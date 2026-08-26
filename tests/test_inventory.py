import unittest
from unittest.mock import patch

from aci_spinup.arm import DeployConfig, build_deployment_topology
from aci_spinup.deploy import (
    DeployRequest,
    execute_deletion,
)
from aci_spinup.errors import (
    InventoryError,
    UnsafeDeletionError,
)
from aci_spinup.inventory import (
    collect_top_level_resource_ids,
    normalize_resource_id,
    unexpected_resource_ids,
)


class ResourceIdTests(unittest.TestCase):
    def test_normalization_is_case_insensitive_and_trims_trailing_slash(self):
        self.assertEqual(
            "/subscriptions/sub/resourcegroups/rg/providers/type/name",
            normalize_resource_id(
                "/Subscriptions/SUB/ResourceGroups/RG/providers/Type/Name/"
            ),
        )

    def test_missing_expected_resources_are_allowed(self):
        expected = {
            "/subscriptions/sub/resourceGroups/rg/providers/T/a",
            "/subscriptions/sub/resourceGroups/rg/providers/T/b",
        }
        actual = {
            "/SUBSCRIPTIONS/SUB/resourcegroups/RG/providers/t/A/",
        }
        self.assertEqual([], unexpected_resource_ids(expected, actual))

    def test_unexpected_resources_are_reported(self):
        expected = {
            "/subscriptions/sub/resourceGroups/rg/providers/T/a",
        }
        extra = "/subscriptions/sub/resourceGroups/rg/providers/T/other"
        self.assertEqual(
            [extra], unexpected_resource_ids(expected, [extra])
        )


class FakeInventoryCLI:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def run_json(self, arguments):
        self.calls.append(arguments)
        return self.payload


class TopLevelInventoryTests(unittest.TestCase):
    def test_uses_only_resource_list_and_ignores_nested_ids(self):
        base = "/subscriptions/sub/resourceGroups/rg/providers"
        top_level = (
            f"{base}/Microsoft.Network/virtualNetworks/vnet"
        )
        cli = FakeInventoryCLI(
            [
                {"id": top_level},
                {"id": f"{top_level}/subnets/default"},
            ]
        )
        self.assertEqual(
            {top_level},
            collect_top_level_resource_ids(cli, "rg"),
        )
        self.assertEqual(
            [
                [
                    "resource",
                    "list",
                    "--resource-group",
                    "rg",
                    "--only-show-errors",
                ]
            ],
            cli.calls,
        )

    def test_page_token_fails_closed(self):
        cli = FakeInventoryCLI(
            {"value": [], "nextLink": "https://example.invalid/next"}
        )
        with self.assertRaisesRegex(InventoryError, "page token"):
            collect_top_level_resource_ids(cli, "rg")

    def test_invalid_json_shape_fails_closed(self):
        cli = FakeInventoryCLI({"value": []})
        with self.assertRaisesRegex(InventoryError, "JSON list"):
            collect_top_level_resource_ids(cli, "rg")

    def test_malformed_provider_path_fails_closed(self):
        cli = FakeInventoryCLI(
            [
                {
                    "id": (
                        "/subscriptions/sub/resourceGroups/rg/providers/"
                        "Microsoft.Example/widgets"
                    )
                }
            ]
        )
        with self.assertRaisesRegex(InventoryError, "invalid resource ID"):
            collect_top_level_resource_ids(cli, "rg")


class FakeDeleteCLI:
    def __init__(self):
        self.commands = []

    def current_subscription_id(self):
        return "sub"

    def run(self, arguments):
        self.commands.append(arguments)
        return ""


class DeletionSubsetTests(unittest.TestCase):
    def request(self):
        topology = build_deployment_topology(
            DeployConfig(
                name="demo",
                resource_group="rg",
                location="northeurope",
                image="image",
                ssh_public_key="ssh-ed25519 TEST",
            )
        )
        return DeployRequest(
            topology=topology,
            delete=True,
            dry_run=False,
            verbose=False,
            output="json",
            output_template=None,
            subscription=None,
            use_existing_resource_group=False,
            ssh_private_key_path=None,
        )

    @patch("aci_spinup.deploy.collect_top_level_resource_ids")
    def test_missing_resources_still_allow_group_deletion(self, collect):
        request = self.request()
        one_expected_id = next(
            iter(
                request.topology.expected_top_level_resource_ids("sub")
            )
        )
        collect.return_value = {one_expected_id}
        cli = FakeDeleteCLI()
        result = execute_deletion(request, cli)
        self.assertEqual("deletion-started", result["status"])
        self.assertEqual(2, collect.call_count)
        self.assertEqual("group", cli.commands[0][0])
        self.assertIn("--no-wait", cli.commands[0])

    @patch("aci_spinup.deploy.collect_top_level_resource_ids")
    def test_unexpected_resource_blocks_group_deletion(self, collect):
        collect.return_value = {
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.Example/widgets/unexpected"
        }
        cli = FakeDeleteCLI()
        with self.assertRaises(UnsafeDeletionError):
            execute_deletion(self.request(), cli)
        self.assertEqual(1, collect.call_count)
        self.assertEqual([], cli.commands)

    @patch("aci_spinup.deploy.collect_top_level_resource_ids")
    def test_second_inventory_pass_can_block_deletion(self, collect):
        request = self.request()
        one_expected_id = next(
            iter(
                request.topology.expected_top_level_resource_ids("sub")
            )
        )
        collect.side_effect = [
            {one_expected_id},
            {
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Example/widgets/late"
            },
        ]
        cli = FakeDeleteCLI()
        with self.assertRaises(UnsafeDeletionError):
            execute_deletion(request, cli)
        self.assertEqual(2, collect.call_count)
        self.assertEqual([], cli.commands)

if __name__ == "__main__":
    unittest.main()
