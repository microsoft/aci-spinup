from __future__ import annotations

import json
import shlex
import subprocess
import sys
from typing import Any

from .errors import AciSpinupError


class AzureCLIError(AciSpinupError):
    def __init__(
        self,
        command: list[str],
        returncode: int | None,
        detail: str,
        *,
        status: str | None = None,
        detail_label: str = "Azure CLI output",
    ):
        self.command = command
        self.returncode = returncode
        self.detail = detail
        command_status = status or (
            "could not be started"
            if returncode is None
            else f"failed with exit code {returncode}"
        )
        rendered_detail = detail.strip() or "Azure CLI returned no error text"
        super().__init__(
            f"Azure CLI command {command_status}: {shlex.join(command)}\n"
            f"{detail_label}: {rendered_detail}"
        )


class AzureCLI:
    def __init__(
        self, subscription: str | None = None, *, verbose: bool = False
    ):
        self.subscription = subscription
        self.verbose = verbose

    def command(self, arguments: list[str]) -> list[str]:
        command = ["az", *arguments]
        if self.subscription:
            command.extend(["--subscription", self.subscription])
        return command

    def run(self, arguments: list[str]) -> str:
        command = self.command(arguments)
        if self.verbose:
            print(f"Running: {shlex.join(command)}", file=sys.stderr)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise AzureCLIError(command, None, str(exc)) from exc
        if result.returncode != 0:
            detail = result.stderr or result.stdout
            detail_label = (
                "Azure stderr" if result.stderr else "Azure stdout"
            )
            raise AzureCLIError(
                command,
                result.returncode,
                detail,
                detail_label=detail_label,
            )
        return result.stdout

    def run_json(self, arguments: list[str]) -> Any:
        output = self.run([*arguments, "--output", "json"])
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise AzureCLIError(
                self.command([*arguments, "--output", "json"]),
                0,
                str(exc),
                status="returned invalid JSON",
                detail_label="JSON parser error",
            ) from exc

    def run_tsv(self, arguments: list[str]) -> str:
        return self.run([*arguments, "--output", "tsv"]).strip()

    def current_subscription_id(self) -> str:
        payload = self.run_json(["account", "show", "--only-show-errors"])
        if not isinstance(payload, dict) or not payload.get("id"):
            raise AzureCLIError(
                self.command(
                    [
                        "account",
                        "show",
                        "--only-show-errors",
                        "--output",
                        "json",
                    ]
                ),
                0,
                "response did not contain a subscription ID",
                status="returned invalid account data",
                detail_label="Validation error",
            )
        return str(payload["id"])
