import contextlib
import io
import unittest

from aci_spinup.azure import AzureCLIError
from scripts.disable_default_outbound import (
    InventoryError,
    Subnet,
    run,
    subnets_requiring_update,
)


class FakeCLI:
    def __init__(self, inventory, *, fail_update=None, verification=False):
        self.inventory = inventory
        self.fail_update = fail_update
        self.verification = verification
        self.run_calls = []
        self.json_calls = []

    def command(self, arguments):
        return ["az", *arguments, "--subscription", "Azure Research Subs"]

    def run(self, arguments):
        self.run_calls.append(arguments)
        if self.fail_update and self.fail_update in arguments:
            raise AzureCLIError(["az", *arguments], 1, "denied")
        return ""

    def run_json(self, arguments):
        self.json_calls.append(arguments)
        if arguments[:3] == ["network", "vnet", "list"]:
            return self.inventory
        return self.verification


class DisableDefaultOutboundTests(unittest.TestCase):
    def inventory(self):
        return [
            {
                "resourceGroup": "rg-b",
                "name": "vnet-b",
                "subnets": [
                    {"name": "unset"},
                    {"name": "enabled", "defaultOutboundAccess": True},
                    {"name": "private", "defaultOutboundAccess": False},
                ],
            },
            {
                "resourceGroup": "rg-a",
                "name": "vnet-a",
                "subnets": [
                    {"name": "default", "defaultOutboundAccess": False}
                ],
            },
        ]

    def test_selects_true_and_unset_but_not_false(self):
        self.assertEqual(
            [
                Subnet("rg-b", "vnet-b", "enabled"),
                Subnet("rg-b", "vnet-b", "unset"),
            ],
            subnets_requiring_update(self.inventory()),
        )

    def test_rejects_malformed_inventory(self):
        with self.assertRaisesRegex(InventoryError, "JSON list"):
            subnets_requiring_update({})

    def test_rejects_malformed_setting_before_applying(self):
        cli = FakeCLI(
            [
                {
                    "resourceGroup": "rg",
                    "name": "vnet",
                    "subnets": [
                        {
                            "name": "default",
                            "defaultOutboundAccess": "false",
                        }
                    ],
                }
            ]
        )
        with self.assertRaisesRegex(
            InventoryError, "invalid defaultOutboundAccess"
        ):
            run(cli, apply=True)
        self.assertEqual([], cli.run_calls)

    def test_dry_run_makes_no_updates(self):
        cli = FakeCLI(self.inventory())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = run(cli, apply=False)
        self.assertEqual(0, result)
        self.assertEqual([], cli.run_calls)
        self.assertEqual(1, len(cli.json_calls))
        self.assertIn("Would update 2 subnet(s)", output.getvalue())
        self.assertIn("--default-outbound false", output.getvalue())

    def test_apply_updates_and_verifies_every_candidate(self):
        cli = FakeCLI(self.inventory(), verification=False)
        result = run(cli, apply=True)
        self.assertEqual(0, result)
        self.assertEqual(
            [
                [
                    "network",
                    "vnet",
                    "subnet",
                    "update",
                    "--resource-group",
                    "rg-b",
                    "--vnet-name",
                    "vnet-b",
                    "--name",
                    "enabled",
                    "--default-outbound",
                    "false",
                    "--only-show-errors",
                    "--output",
                    "none",
                ],
                [
                    "network",
                    "vnet",
                    "subnet",
                    "update",
                    "--resource-group",
                    "rg-b",
                    "--vnet-name",
                    "vnet-b",
                    "--name",
                    "unset",
                    "--default-outbound",
                    "false",
                    "--only-show-errors",
                    "--output",
                    "none",
                ],
            ],
            cli.run_calls,
        )
        self.assertEqual(
            [
                ["network", "vnet", "list", "--only-show-errors"],
                [
                    "network",
                    "vnet",
                    "subnet",
                    "show",
                    "--resource-group",
                    "rg-b",
                    "--vnet-name",
                    "vnet-b",
                    "--name",
                    "enabled",
                    "--query",
                    "defaultOutboundAccess",
                    "--only-show-errors",
                ],
                [
                    "network",
                    "vnet",
                    "subnet",
                    "show",
                    "--resource-group",
                    "rg-b",
                    "--vnet-name",
                    "vnet-b",
                    "--name",
                    "unset",
                    "--query",
                    "defaultOutboundAccess",
                    "--only-show-errors",
                ],
            ],
            cli.json_calls,
        )

    def test_apply_reports_failures_and_continues(self):
        cli = FakeCLI(
            self.inventory(),
            fail_update="enabled",
            verification=False,
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = run(cli, apply=True)
        self.assertEqual(1, result)
        self.assertEqual(2, len(cli.run_calls))
        self.assertIn("1 of 2 subnet update(s) failed", stderr.getvalue())

    def test_apply_fails_when_read_back_is_not_false(self):
        cli = FakeCLI(self.inventory(), verification=True)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = run(cli, apply=True)
        self.assertEqual(1, result)
        self.assertEqual(2, len(cli.run_calls))
        self.assertIn(
            "rg-b/vnet-b/enabled still reports "
            "defaultOutboundAccess=True",
            stderr.getvalue(),
        )
        self.assertIn(
            "rg-b/vnet-b/unset still reports "
            "defaultOutboundAccess=True",
            stderr.getvalue(),
        )
        self.assertIn("2 of 2 subnet update(s) failed", stderr.getvalue())

    def test_no_candidates_is_idempotent(self):
        cli = FakeCLI(
            [
                {
                    "resourceGroup": "rg",
                    "name": "vnet",
                    "subnets": [
                        {
                            "name": "default",
                            "defaultOutboundAccess": False,
                        }
                    ],
                }
            ]
        )
        self.assertEqual(0, run(cli, apply=True))
        self.assertEqual([], cli.run_calls)


if __name__ == "__main__":
    unittest.main()
