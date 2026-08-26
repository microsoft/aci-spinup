import subprocess
import unittest
from unittest.mock import patch

from aci_spinup.azure import (
    AzureCLI,
    AzureCLIError,
)


class AzureCLITests(unittest.TestCase):
    @patch("aci_spinup.azure.subprocess.run")
    def test_subscription_is_applied_and_id_is_returned(self, run):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout='{"id": "sub-id", "name": "test subscription"}',
            stderr="",
        )
        subscription_id = AzureCLI(
            "requested-sub"
        ).current_subscription_id()
        self.assertEqual("sub-id", subscription_id)
        command = run.call_args.args[0]
        self.assertEqual("az", command[0])
        self.assertEqual(
            ["--subscription", "requested-sub"], command[-2:]
        )

    @patch("aci_spinup.azure.subprocess.run")
    def test_error_contains_azure_stderr(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 2, stdout="", stderr="authentication required"
        )
        with self.assertRaisesRegex(
            AzureCLIError, "Azure stderr: authentication required"
        ):
            AzureCLI().run(["group", "list"])

    @patch("aci_spinup.azure.subprocess.run")
    def test_invalid_json_has_a_parse_specific_error(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="not-json", stderr=""
        )
        with self.assertRaisesRegex(
            AzureCLIError, "returned invalid JSON"
        ) as raised:
            AzureCLI().run_json(["group", "list"])
        self.assertNotIn("failed with exit code 0", str(raised.exception))

    @patch("aci_spinup.azure.subprocess.run")
    def test_stdout_only_failure_is_labeled_as_stdout(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 2, stdout="extension failed", stderr=""
        )
        with self.assertRaisesRegex(
            AzureCLIError, "Azure stdout: extension failed"
        ):
            AzureCLI().run(["group", "list"])


if __name__ == "__main__":
    unittest.main()
