import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from aci_spinup.cli import build_parser, main
from aci_spinup.repair import config_from_args


class CLIParsingTests(unittest.TestCase):
    def parse_error(self, parser, arguments):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(arguments)
        self.assertEqual(2, raised.exception.code)

    def test_deploy_defaults_and_removed_flags(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "deploy",
                "--resource-group",
                "rg",
                "--image",
                "image",
                "--ssh-key",
                "key.pub",
            ]
        )
        self.assertEqual("azure-linux-3", args.install_mode)
        self.assertEqual("22", args.tcp_ports)
        self.assertEqual("confidential", args.sku)
        for removed in (
            "--azure-auth",
            "--access-mode",
            "--create-nat",
            "--create-vnet",
            "--vnet-subnet",
        ):
            self.parse_error(
                build_parser(),
                [
                    "deploy",
                    "--resource-group",
                    "rg",
                    removed,
                    "value",
                ],
            )

    def test_repair_requires_exactly_one_selection_mode(self):
        base = [
            "repair-subnet-outbound",
            "--resource-group",
            "rg",
            "--vnet",
            "vnet",
        ]
        self.parse_error(build_parser(), base)
        self.parse_error(
            build_parser(), [*base, "--all", "--subnet", "default"]
        )
        named = build_parser().parse_args([*base, "--subnet", "one,two"])
        self.assertEqual(["one,two"], named.subnet)
        all_subnets = build_parser().parse_args([*base, "--all"])
        self.assertTrue(all_subnets.all)

    def test_reserved_named_subnet_requires_explicit_override(self):
        parser = build_parser()
        arguments = [
            "repair-subnet-outbound",
            "--resource-group",
            "rg",
            "--vnet",
            "vnet",
            "--subnet",
            "GatewaySubnet",
        ]
        args = parser.parse_args(arguments)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                config_from_args(args.command_parser, args)
        args = parser.parse_args(
            [*arguments, "--allow-reserved-subnets"]
        )
        config = config_from_args(args.command_parser, args)
        self.assertEqual(("GatewaySubnet",), config.subnet_names)

    def test_empty_azure_identifiers_are_rejected(self):
        with self.assertRaises(SystemExit):
            main(
                [
                    "deploy",
                    "--resource-group",
                    "",
                    "--delete",
                    "--dry-run",
                ]
            )
        with self.assertRaises(SystemExit):
            main(
                [
                    "repair-subnet-outbound",
                    "--resource-group",
                    "rg",
                    "--vnet",
                    "",
                    "--subnet",
                    "app",
                    "--location",
                    "northeurope",
                    "--dry-run",
                ]
            )

    def test_non_key_ssh_file_is_rejected(self):
        with self.assertRaises(SystemExit):
            main(
                [
                    "deploy",
                    "--resource-group",
                    "rg",
                    "--image",
                    "image",
                    "--ssh-key",
                    str(Path(__file__).parent.parent / "README.md"),
                    "--dry-run",
                ]
            )

    @patch("aci_spinup.azure.subprocess.run")
    def test_deploy_dry_run_does_not_call_azure(self, run):
        key_path = (
            Path(__file__).parent / "fixtures" / "test-key.pub"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "deploy",
                    "--resource-group-prefix",
                    "test",
                    "--name",
                    "dry",
                    "--image",
                    "example.invalid/image:1",
                    "--ssh-key",
                    str(key_path),
                    "--dry-run",
                    "--output",
                    "json",
                ]
            )
        self.assertEqual(0, result)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["dryRun"])
        self.assertEqual("test-dry", payload["resourceGroup"])
        run.assert_not_called()

    @patch("aci_spinup.azure.subprocess.run")
    def test_named_repair_dry_run_with_location_does_not_call_azure(self, run):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "repair-subnet-outbound",
                    "--resource-group",
                    "rg",
                    "--vnet",
                    "vnet",
                    "--subnet",
                    "app",
                    "--location",
                    "northeurope",
                    "--dry-run",
                    "--output",
                    "json",
                ]
            )
        self.assertEqual(0, result)
        self.assertFalse(json.loads(output.getvalue())["lookupPerformed"])
        run.assert_not_called()

    @patch("aci_spinup.azure.subprocess.run")
    def test_custom_cce_policy_file_and_storage_name_feed_template(self, run):
        fixtures = Path(__file__).parent / "fixtures"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "deploy",
                    "--resource-group-prefix",
                    "test",
                    "--name",
                    "custom",
                    "--image",
                    "example.invalid/image:1",
                    "--ssh-key",
                    str(fixtures / "test-key.pub"),
                    "--azure-file-mount",
                    "share=workspace,path=/mnt/workspace",
                    "--azure-file-account-name",
                    "globallyunique123",
                    "--cce-policy-file",
                    str(fixtures / "test-cce-policy.txt"),
                    "--dry-run",
                    "--output",
                    "json",
                ]
            )
        self.assertEqual(0, result)
        payload = json.loads(output.getvalue())
        resources = payload["template"]["resources"]
        account = next(
            item
            for item in resources
            if item["type"] == "Microsoft.Storage/storageAccounts"
        )
        group = next(
            item
            for item in resources
            if item["type"]
            == "Microsoft.ContainerInstance/containerGroups"
        )
        self.assertEqual("globallyunique123", account["name"])
        self.assertEqual(
            "Y3VzdG9tLWRldmVsb3BtZW50LXBvbGljeQ==",
            group["properties"]["confidentialComputeProperties"]["ccePolicy"],
        )
        self.assertFalse(
            any(
                "allow-all development" in warning
                for warning in payload["warnings"]
            )
        )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
