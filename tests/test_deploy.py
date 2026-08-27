import unittest
from dataclasses import replace

from aci_spinup.arm import (
    AzureFileMountSpec,
    DeployConfig,
    build_deployment_topology,
)
from aci_spinup.azure import AzureCLI
from aci_spinup.deploy import (
    DeployRequest,
    deployment_dry_run,
    deletion_dry_run,
    ensure_deployment_resource_group,
    execute_deployment,
)


def deploy_request(**overrides):
    config_values = {
        "name": "demo",
        "resource_group": "rg-demo",
        "location": "northeurope",
        "image": "image",
        "ssh_public_key": "ssh-ed25519 TEST",
    }
    config_values.update(overrides)
    topology = build_deployment_topology(
        DeployConfig(**config_values)
    )
    return DeployRequest(
        topology=topology,
        delete=False,
        dry_run=False,
        verbose=False,
        output="json",
        output_template=None,
        subscription=None,
        use_existing_resource_group=False,
        ssh_private_key_path=None,
    )


class FakeGroupCLI:
    def __init__(self, exists, payload=None):
        self.exists = exists
        self.payload = payload
        self.run_calls = []
        self.json_calls = []

    def run_tsv(self, arguments):
        return self.exists

    def run_json(self, arguments):
        self.json_calls.append(arguments)
        return self.payload

    def run(self, arguments):
        self.run_calls.append(arguments)
        return ""


class FakeDeploymentCLI(FakeGroupCLI):
    def __init__(self):
        super().__init__("")
        self.tsv_calls = []

    def current_subscription_id(self):
        return "00000000-0000-0000-0000-000000000000"

    def run_tsv(self, arguments):
        self.tsv_calls.append(arguments)
        if arguments[:2] == ["container", "show"]:
            return "10.0.0.9"
        raise AssertionError(f"unexpected TSV command: {arguments}")


class ResourceGroupTests(unittest.TestCase):
    def test_group_is_created_or_reused_without_tags(self):
        cli = FakeGroupCLI("false")
        action = ensure_deployment_resource_group(deploy_request(), cli)
        self.assertEqual("created-or-existing", action)
        command = cli.run_calls[0]
        self.assertEqual(["group", "create"], command[:2])
        self.assertNotIn("--tags", command)
        self.assertEqual(["group", "wait"], cli.run_calls[1][:2])

    def test_dry_run_shows_group_creation_and_legacy_cce_warning(self):
        result = deployment_dry_run(deploy_request(), AzureCLI())
        commands = [step.get("command", []) for step in result["steps"]]
        self.assertTrue(
            any(command[1:3] == ["group", "create"] for command in commands)
        )
        self.assertTrue(
            any(command[1:3] == ["group", "wait"] for command in commands)
        )
        self.assertNotIn("requiredResourceGroupTags", result)
        self.assertTrue(
            any(
                "allow-all development" in warning
                for warning in result["warnings"]
            )
        )
        self.assertFalse(
            any("address-pool" in " ".join(command) for command in commands)
        )

    def test_custom_cce_policy_has_no_legacy_warning(self):
        result = deployment_dry_run(
            deploy_request(cce_policy="Y3VzdG9t"),
            AzureCLI(),
        )
        self.assertEqual([], result["warnings"])

    def test_execute_deployment_reads_private_ips_only(self):
        cli = FakeDeploymentCLI()
        result = execute_deployment(deploy_request(), cli)

        self.assertEqual(
            [
                ["group", "create"],
                ["group", "wait"],
                ["deployment", "group"],
            ],
            [command[:2] for command in cli.run_calls],
        )
        self.assertEqual(
            [["container", "show"]],
            [command[:2] for command in cli.tsv_calls],
        )
        self.assertEqual(
            {
                "containerGroup": "demo-1",
                "requestedPrivateIp": "10.0.0.4",
                "privateIp": "10.0.0.9",
            },
            result["nodes"][0],
        )

    def test_dry_run_does_not_treat_subscription_name_as_an_id(self):
        request = replace(
            deploy_request(), subscription="Production Subscription"
        )
        result = deployment_dry_run(request, AzureCLI())
        self.assertTrue(
            all(
                resource_id.startswith(
                    "/subscriptions/<resolved-subscription-id>/"
                )
                for resource_id in result["expectedTopLevelResourceIds"]
            )
        )

    def test_deletion_dry_run_shows_two_top_level_inventory_passes(self):
        result = deletion_dry_run(
            deploy_request(
                azure_file_mounts=(
                    AzureFileMountSpec("workspace", "/mnt/workspace"),
                )
            ),
            AzureCLI(),
        )
        commands = [step["command"] for step in result["steps"]]
        command_text = [" ".join(command) for command in commands]
        self.assertFalse(any("group show" in command for command in command_text))
        self.assertEqual(
            2,
            sum(
                command[1:3] == ["resource", "list"]
                and "--resource-type" not in command
                for command in commands
            ),
        )
        self.assertEqual(3, len(commands))
        for fragment in (
            "vnet peering list",
            "lock list",
            "role assignment list",
            "policy assignment list",
            "policy exemption list",
            "diagnostic-settings list",
            "storage container-rm list",
        ):
            self.assertFalse(
                any(fragment in command for command in command_text),
                fragment,
            )
        expected = "\n".join(result["expectedTopLevelResourceIds"])
        self.assertIn("/storageAccounts/", expected)
        self.assertNotIn("/fileServices/", expected)
        self.assertEqual("top-level ARM resources only", result["inventoryScope"])
        self.assertTrue(
            any("best-effort" in warning for warning in result["warnings"])
        )
        self.assertTrue(
            any("not transactional" in warning for warning in result["warnings"])
        )


if __name__ == "__main__":
    unittest.main()
