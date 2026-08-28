#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import shlex
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aci_spinup.azure import AzureCLI, AzureCLIError  # noqa: E402
from aci_spinup.errors import AciSpinupError  # noqa: E402


DEFAULT_SUBSCRIPTION = "Azure Research Subs"


class InventoryError(AciSpinupError):
    pass


@dataclass(frozen=True, order=True)
class Subnet:
    resource_group: str
    vnet: str
    name: str


def subnets_requiring_update(payload: Any) -> list[Subnet]:
    if not isinstance(payload, list):
        raise InventoryError("Azure VNet inventory did not return a JSON list")

    candidates: list[Subnet] = []
    for vnet in payload:
        if not isinstance(vnet, dict):
            raise InventoryError("Azure VNet inventory contains a non-object")
        resource_group = vnet.get("resourceGroup")
        vnet_name = vnet.get("name")
        subnets = vnet.get("subnets")
        if not isinstance(resource_group, str) or not resource_group:
            raise InventoryError("Azure VNet inventory has no resource group")
        if not isinstance(vnet_name, str) or not vnet_name:
            raise InventoryError("Azure VNet inventory has no VNet name")
        if not isinstance(subnets, list):
            raise InventoryError(
                f"{resource_group}/{vnet_name} has invalid subnet inventory"
            )
        for subnet in subnets:
            if not isinstance(subnet, dict):
                raise InventoryError(
                    f"{resource_group}/{vnet_name} contains a non-object subnet"
                )
            subnet_name = subnet.get("name")
            if not isinstance(subnet_name, str) or not subnet_name:
                raise InventoryError(
                    f"{resource_group}/{vnet_name} has an unnamed subnet"
                )
            default_outbound = subnet.get("defaultOutboundAccess")
            if default_outbound is False:
                continue
            if default_outbound is not True and default_outbound is not None:
                raise InventoryError(
                    f"{resource_group}/{vnet_name}/{subnet_name} has invalid "
                    "defaultOutboundAccess"
                )
            if default_outbound is True or default_outbound is None:
                candidates.append(
                    Subnet(resource_group, vnet_name, subnet_name)
                )
    return sorted(candidates, key=lambda item: tuple(
        value.casefold()
        for value in (item.resource_group, item.vnet, item.name)
    ))


def update_arguments(subnet: Subnet) -> list[str]:
    return [
        "network",
        "vnet",
        "subnet",
        "update",
        "--resource-group",
        subnet.resource_group,
        "--vnet-name",
        subnet.vnet,
        "--name",
        subnet.name,
        "--default-outbound",
        "false",
        "--only-show-errors",
        "--output",
        "none",
    ]


def verify_arguments(subnet: Subnet) -> list[str]:
    return [
        "network",
        "vnet",
        "subnet",
        "show",
        "--resource-group",
        subnet.resource_group,
        "--vnet-name",
        subnet.vnet,
        "--name",
        subnet.name,
        "--query",
        "defaultOutboundAccess",
        "--only-show-errors",
    ]


def run(cli: AzureCLI, *, apply: bool) -> int:
    inventory = cli.run_json(
        ["network", "vnet", "list", "--only-show-errors"]
    )
    candidates = subnets_requiring_update(inventory)
    if not candidates:
        print("All subnets already have defaultOutboundAccess=false.")
        return 0

    if not apply:
        print(f"Would update {len(candidates)} subnet(s):")
        for subnet in candidates:
            print(f"  {shlex.join(cli.command(update_arguments(subnet)))}")
        print("No Azure mutations were performed. Pass --apply to continue.")
        return 0

    failures: list[str] = []
    for subnet in candidates:
        label = f"{subnet.resource_group}/{subnet.vnet}/{subnet.name}"
        try:
            cli.run(update_arguments(subnet))
            actual = cli.run_json(verify_arguments(subnet))
            if actual is not False:
                raise InventoryError(
                    f"{label} still reports defaultOutboundAccess={actual!r}"
                )
        except (AzureCLIError, InventoryError) as exc:
            failures.append(f"{label}: {exc}")
            print(f"FAILED: {failures[-1]}", file=sys.stderr)
        else:
            print(f"Updated {label}")

    if failures:
        print(
            f"{len(failures)} of {len(candidates)} subnet update(s) failed.",
            file=sys.stderr,
        )
        return 1
    print(f"Updated {len(candidates)} subnet(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Set defaultOutboundAccess=false on every subnet in a subscription."
        )
    )
    parser.add_argument(
        "--subscription",
        default=DEFAULT_SUBSCRIPTION,
        help=f"Azure subscription (default: {DEFAULT_SUBSCRIPTION})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates; without this flag the script only prints a plan",
    )
    args = parser.parse_args(argv)
    try:
        return run(AzureCLI(args.subscription), apply=args.apply)
    except AciSpinupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
