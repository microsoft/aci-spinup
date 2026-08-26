import unittest
from unittest.mock import patch

from aci_spinup.azure import AzureCLI
from aci_spinup.repair import (
    RepairConfig,
    RepairError,
    SubnetState,
    VNetState,
    build_repair_plan,
    execute_repair_mutations,
    nat_gateway_create_arguments,
    parse_subnet_names,
    public_ip_create_arguments,
    repair_mutation_commands,
    subnet_update_arguments,
)


class RepairPlanningTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "resource_group": "network-rg",
            "vnet_name": "demo-vnet",
            "subnet_names": ("workload-a",),
            "all_subnets": False,
            "allow_reserved_subnets": False,
            "location": "northeurope",
            "nat_gateway_name": "demo-nat",
            "public_ip_name": "demo-nat-ip",
        }
        values.update(overrides)
        return RepairConfig(**values)

    def vnet(self):
        base = (
            "/subscriptions/sub/resourceGroups/network-rg/providers/"
            "Microsoft.Network"
        )
        return VNetState(
            resource_id=f"{base}/virtualNetworks/demo-vnet",
            location="northeurope",
            subnets=(
                SubnetState("workload-a", None),
                SubnetState(
                    "workload-b",
                    f"{base}/natGateways/preexisting",
                    False,
                ),
                SubnetState("GatewaySubnet", None),
                SubnetState("AzureBastionSubnet", None),
                SubnetState("AzureFirewallManagementSubnet", None),
            ),
        )

    def test_repeated_and_comma_separated_names_are_deduplicated(self):
        self.assertEqual(
            ("one", "two", "THREE"),
            parse_subnet_names(["one,two", "ONE", "THREE"]),
        )

    def test_all_skips_reserved_and_leaves_associated_nat_untouched(self):
        plan = build_repair_plan(
            self.config(
                subnet_names=(),
                all_subnets=True,
                location=None,
            ),
            self.vnet(),
        )
        self.assertEqual(("workload-a", "workload-b"), plan.selected_subnets)
        self.assertEqual(("workload-a",), plan.update_subnets)
        self.assertEqual(("workload-b",), tuple(
            subnet.name for subnet in plan.unchanged_subnets
        ))
        self.assertEqual(
            (
                "GatewaySubnet",
                "AzureBastionSubnet",
                "AzureFirewallManagementSubnet",
            ),
            plan.skipped_reserved_subnets,
        )

    def test_lookup_free_named_dry_run_plans_one_nat_for_all_subnets(self):
        plan = build_repair_plan(
            self.config(subnet_names=("workload-a", "workload-b")),
            None,
        )
        commands = repair_mutation_commands(AzureCLI("sub"), plan)
        self.assertEqual(9, len(commands))
        public_ip_create = public_ip_create_arguments(plan)
        self.assertEqual(
            ["network", "public-ip", "create"],
            public_ip_create[:3],
        )
        self.assertIn(
            "FirstPartyUsage=/NonProd",
            public_ip_create,
        )
        self.assertNotIn("--tags", public_ip_create)
        nat_create = nat_gateway_create_arguments(plan)
        self.assertEqual(
            ["network", "nat", "gateway", "create"],
            nat_create[:4],
        )
        self.assertIn("demo-nat-ip", nat_create)
        self.assertNotIn("--tags", nat_create)
        command_text = [" ".join(command) for command in commands]
        self.assertTrue(
            any("network public-ip wait" in command for command in command_text)
        )
        self.assertTrue(
            any("network nat gateway wait" in command for command in command_text)
        )
        for subnet in plan.update_subnets:
            update = subnet_update_arguments(
                plan, subnet, plan.config.nat_gateway_name
            )
            self.assertIn("--default-outbound", update)
            self.assertIn("false", update)
            self.assertIn("demo-nat", update)

    def test_explicit_missing_subnet_fails(self):
        with self.assertRaisesRegex(RepairError, "not found"):
            build_repair_plan(
                self.config(subnet_names=("missing",)), self.vnet()
            )

    def test_all_reserved_vnet_fails(self):
        state = VNetState(
            resource_id=self.vnet().resource_id,
            location="northeurope",
            subnets=(SubnetState("GatewaySubnet", None),),
        )
        with self.assertRaisesRegex(RepairError, "no non-reserved"):
            build_repair_plan(
                self.config(
                    subnet_names=(), all_subnets=True, location=None
                ),
                state,
            )

    def test_late_nat_association_skips_subnet_update(self):
        plan = build_repair_plan(self.config(), self.vnet())

        class FakeCLI:
            def __init__(self):
                self.run_calls = []

            def run_json(self, arguments):
                return {
                    "name": "workload-a",
                    "natGateway": {"id": "late-nat-id"},
                    "defaultOutboundAccess": False,
                }

            def run(self, arguments):
                self.run_calls.append(arguments)
                return ""

            def run_tsv(self, arguments):
                raise AssertionError("public IP must not be read")

        cli = FakeCLI()
        updated, late, public_ip = execute_repair_mutations(
            cli, plan
        )
        self.assertEqual((), updated)
        self.assertEqual(("workload-a",), tuple(item.name for item in late))
        self.assertEqual("late-nat-id", late[0].nat_gateway_id)
        self.assertIsNone(public_ip)
        self.assertEqual([], cli.run_calls)

    def test_existing_nat_is_preserved_when_disabling_default_outbound(self):
        base = (
            "/subscriptions/sub/resourceGroups/network-rg/providers/"
            "Microsoft.Network"
        )
        existing_nat = f"{base}/natGateways/preexisting"
        state = VNetState(
            resource_id=f"{base}/virtualNetworks/demo-vnet",
            location="northeurope",
            subnets=(SubnetState("workload-a", existing_nat, True),),
        )
        plan = build_repair_plan(self.config(), state)
        self.assertEqual(("workload-a",), plan.update_subnets)
        self.assertFalse(plan.needs_generated_nat)

        class FakeCLI:
            def __init__(self):
                self.run_calls = []

            def run_json(self, arguments):
                return {
                    "name": "workload-a",
                    "natGateway": {"id": existing_nat},
                    "defaultOutboundAccess": True,
                }

            def run(self, arguments):
                self.run_calls.append(arguments)
                return ""

            def run_tsv(self, arguments):
                raise AssertionError("no generated public IP is used")

        cli = FakeCLI()
        updated, late, public_ip = execute_repair_mutations(
            cli, plan
        )
        self.assertEqual(("workload-a",), updated)
        self.assertEqual((), late)
        self.assertIsNone(public_ip)
        command = cli.run_calls[0]
        self.assertNotIn("--nat-gateway", command)
        self.assertEqual(
            "false", command[command.index("--default-outbound") + 1]
        )

    def test_missing_nat_resources_are_created_before_subnet_update(self):
        plan = build_repair_plan(self.config(), self.vnet())

        class FakeCLI:
            def __init__(self):
                self.run_calls = []

            def run_json(self, arguments):
                return {"name": "workload-a", "natGateway": None}

            def run(self, arguments):
                self.run_calls.append(arguments)
                return ""

            def run_tsv(self, arguments):
                return "203.0.113.10"

        cli = FakeCLI()
        updated, late, public_ip = execute_repair_mutations(
            cli, plan
        )
        self.assertEqual(("workload-a",), updated)
        self.assertEqual((), late)
        self.assertEqual("203.0.113.10", public_ip)
        self.assertEqual(
            ["network", "public-ip", "create"],
            cli.run_calls[0][:3],
        )
        self.assertTrue(
            any(command[:4] == ["network", "vnet", "subnet", "update"]
                for command in cli.run_calls)
        )


if __name__ == "__main__":
    unittest.main()
