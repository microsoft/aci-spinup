from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import shlex
import sys
from typing import Any

from .arm import nat_gateway_name_for_vnet, nat_public_ip_name_for_vnet
from .azure import AzureCLI
from .errors import AciSpinupError


RESERVED_SUBNET_NAMES = {
    "azurebastionsubnet",
    "azurefirewallmanagementsubnet",
    "azurefirewallsubnet",
    "gatewaysubnet",
    "routeserversubnet",
}
REPAIR_RACE_WARNING = (
    "Subnet NAT attachment is not transactional. Azure CLI exposes no "
    "conditional ETag for subnet update, so a NAT can still be attached "
    "after the final read and before the update."
)


@dataclass(frozen=True)
class SubnetState:
    name: str
    nat_gateway_id: str | None
    default_outbound_access: bool | None = None


@dataclass(frozen=True)
class VNetState:
    resource_id: str
    location: str
    subnets: tuple[SubnetState, ...]


@dataclass(frozen=True)
class RepairConfig:
    resource_group: str
    vnet_name: str
    subnet_names: tuple[str, ...]
    all_subnets: bool
    allow_reserved_subnets: bool
    location: str | None
    nat_gateway_name: str
    public_ip_name: str


@dataclass(frozen=True)
class RepairPlan:
    config: RepairConfig
    location: str
    selected_subnets: tuple[str, ...]
    update_subnets: tuple[str, ...]
    preserved_nat_gateways: tuple[tuple[str, str], ...]
    unchanged_subnets: tuple[SubnetState, ...]
    skipped_reserved_subnets: tuple[str, ...]
    vnet_resource_id: str | None

    @property
    def needs_generated_nat(self) -> bool:
        preserved = {
            name.casefold() for name, _ in self.preserved_nat_gateways
        }
        return any(
            name.casefold() not in preserved for name in self.update_subnets
        )


class RepairError(AciSpinupError):
    """Raised when a VNet cannot be repaired safely."""


def add_repair_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--resource-group", required=True, help="VNet resource group"
    )
    parser.add_argument("--vnet", required=True, help="Virtual network name")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--subnet",
        action="append",
        help="Named subnet. Repeat or comma-separate for multiple subnets",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="Select every non-reserved subnet in the VNet",
    )
    parser.add_argument(
        "--allow-reserved-subnets",
        action="store_true",
        help="Permit explicitly named Azure reserved subnets",
    )
    parser.add_argument(
        "--location",
        help="VNet region. Required for a lookup-free named-subnet dry run",
    )
    parser.add_argument(
        "--nat-name",
        help="NAT gateway name (default derived from the VNet name)",
    )
    parser.add_argument(
        "--public-ip-name",
        help="NAT public IP name (default derived from the VNet name)",
    )
    parser.add_argument(
        "--subscription", help="Azure subscription name or ID"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the lookup and mutation plan without changing Azure",
    )
    parser.add_argument(
        "--output",
        choices=("human", "json"),
        default="human",
        help="Output format (default: human)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print Azure commands to stderr",
    )


def parse_subnet_names(values: list[str] | None) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for raw_name in value.split(","):
            name = raw_name.strip()
            if not name:
                continue
            normalized = name.casefold()
            if normalized not in seen:
                seen.add(normalized)
                names.append(name)
    return tuple(names)


def config_from_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> RepairConfig:
    for option, value in (
        ("--resource-group", args.resource_group),
        ("--vnet", args.vnet),
        ("--location", args.location),
        ("--nat-name", args.nat_name),
        ("--public-ip-name", args.public_ip_name),
        ("--subscription", args.subscription),
    ):
        if value is not None and not str(value).strip():
            parser.error(f"{option} cannot be empty")
    subnet_names = parse_subnet_names(args.subnet)
    if not args.all and not subnet_names:
        parser.error("--subnet must name at least one subnet")
    if args.all and args.allow_reserved_subnets:
        parser.error(
            "--allow-reserved-subnets applies only to explicitly named --subnet values"
        )
    reserved = [
        name
        for name in subnet_names
        if name.casefold() in RESERVED_SUBNET_NAMES
    ]
    if reserved and not args.allow_reserved_subnets:
        parser.error(
            "reserved subnet(s) require --allow-reserved-subnets: "
            + ", ".join(reserved)
        )
    return RepairConfig(
        resource_group=args.resource_group,
        vnet_name=args.vnet,
        subnet_names=subnet_names,
        all_subnets=args.all,
        allow_reserved_subnets=args.allow_reserved_subnets,
        location=args.location,
        nat_gateway_name=(
            args.nat_name or nat_gateway_name_for_vnet(args.vnet)
        ),
        public_ip_name=(
            args.public_ip_name or nat_public_ip_name_for_vnet(args.vnet)
        ),
    )


def _required_text(payload: Any, key: str, label: str) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), str):
        raise RepairError(f"{label} did not contain '{key}'")
    value = payload[key].strip()
    if not value:
        raise RepairError(f"{label} contained an empty '{key}'")
    return value


def read_vnet(cli: AzureCLI, config: RepairConfig) -> VNetState:
    payload = cli.run_json(
        [
            "network",
            "vnet",
            "show",
            "--resource-group",
            config.resource_group,
            "--name",
            config.vnet_name,
            "--only-show-errors",
        ]
    )
    resource_id = _required_text(payload, "id", "virtual network response")
    location = _required_text(
        payload, "location", "virtual network response"
    )
    raw_subnets = payload.get("subnets") if isinstance(payload, dict) else None
    if not isinstance(raw_subnets, list):
        raise RepairError("virtual network response did not contain subnets")
    subnets = []
    for raw_subnet in raw_subnets:
        name = _required_text(raw_subnet, "name", "subnet response")
        raw_nat = (
            raw_subnet.get("natGateway")
            if isinstance(raw_subnet, dict)
            else None
        )
        nat_id = None
        if raw_nat:
            nat_id = _required_text(
                raw_nat, "id", f"NAT association for subnet {name}"
            )
        default_outbound = _resource_property(
            raw_subnet, "defaultOutboundAccess"
        )
        if default_outbound is not None and not isinstance(
            default_outbound, bool
        ):
            raise RepairError(
                f"subnet {name} returned invalid defaultOutboundAccess"
            )
        subnets.append(SubnetState(name, nat_id, default_outbound))
    return VNetState(resource_id, location, tuple(subnets))


def expected_nat_gateway_id(vnet: VNetState, nat_name: str) -> str:
    marker = "/providers/microsoft.network/virtualnetworks/"
    normalized = vnet.resource_id.casefold()
    index = normalized.find(marker)
    if index < 0:
        raise RepairError(
            f"VNet returned an unexpected resource ID: {vnet.resource_id}"
        )
    return (
        f"{vnet.resource_id[:index]}"
        f"/providers/Microsoft.Network/natGateways/{nat_name}"
    )


def build_repair_plan(
    config: RepairConfig, vnet: VNetState | None
) -> RepairPlan:
    reserved = [
        name
        for name in config.subnet_names
        if name.casefold() in RESERVED_SUBNET_NAMES
    ]
    if reserved and not config.allow_reserved_subnets:
        raise RepairError(
            "reserved subnet(s) require explicit permission: "
            + ", ".join(reserved)
        )
    if vnet is None:
        if config.all_subnets:
            raise RepairError("--all requires a VNet lookup")
        if config.location is None:
            raise RepairError(
                "--location is required without a VNet lookup"
            )
        if len(config.subnet_names) > 800:
            raise RepairError("one NAT gateway supports at most 800 subnets")
        return RepairPlan(
            config=config,
            location=config.location,
            selected_subnets=config.subnet_names,
            update_subnets=config.subnet_names,
            preserved_nat_gateways=(),
            unchanged_subnets=(),
            skipped_reserved_subnets=(),
            vnet_resource_id=None,
        )

    if config.location and config.location.casefold() != vnet.location.casefold():
        raise RepairError(
            f"--location {config.location} does not match VNet location "
            f"{vnet.location}"
        )
    known = {subnet.name.casefold(): subnet for subnet in vnet.subnets}
    skipped: list[str] = []
    if config.all_subnets:
        selected_states = []
        for subnet in vnet.subnets:
            if subnet.name.casefold() in RESERVED_SUBNET_NAMES:
                skipped.append(subnet.name)
            else:
                selected_states.append(subnet)
    else:
        missing = [
            name
            for name in config.subnet_names
            if name.casefold() not in known
        ]
        if missing:
            raise RepairError(
                f"subnet(s) not found in VNet {config.vnet_name}: "
                + ", ".join(missing)
            )
        selected_states = [
            known[name.casefold()] for name in config.subnet_names
        ]
    if not selected_states:
        raise RepairError("no non-reserved target subnets were selected")

    unchanged = tuple(
        subnet
        for subnet in selected_states
        if subnet.nat_gateway_id
        and subnet.default_outbound_access is False
    )
    updates = tuple(
        subnet.name
        for subnet in selected_states
        if not subnet.nat_gateway_id
        or subnet.default_outbound_access is not False
    )
    preserved_nat_gateways = tuple(
        (subnet.name, subnet.nat_gateway_id)
        for subnet in selected_states
        if subnet.name in updates and subnet.nat_gateway_id
    )
    expected_nat_id = expected_nat_gateway_id(
        vnet, config.nat_gateway_name
    ).casefold()
    already_on_generated_nat = sum(
        1
        for subnet in vnet.subnets
        if subnet.nat_gateway_id
        and subnet.nat_gateway_id.rstrip("/").casefold() == expected_nat_id
    )
    new_associations = sum(
        1 for subnet in selected_states if not subnet.nat_gateway_id
    )
    if already_on_generated_nat + new_associations > 800:
        raise RepairError("one NAT gateway supports at most 800 subnets")

    return RepairPlan(
        config=config,
        location=vnet.location,
        selected_subnets=tuple(subnet.name for subnet in selected_states),
        update_subnets=updates,
        preserved_nat_gateways=preserved_nat_gateways,
        unchanged_subnets=unchanged,
        skipped_reserved_subnets=tuple(skipped),
        vnet_resource_id=vnet.resource_id,
    )


def public_ip_create_arguments(plan: RepairPlan) -> list[str]:
    return [
        "network",
        "public-ip",
        "create",
        "--resource-group",
        plan.config.resource_group,
        "--name",
        plan.config.public_ip_name,
        "--location",
        plan.location,
        "--sku",
        "Standard",
        "--allocation-method",
        "Static",
        "--version",
        "IPv4",
        "--idle-timeout",
        "4",
        "--ip-tags",
        "FirstPartyUsage=/NonProd",
        "--only-show-errors",
        "--output",
        "none",
    ]


def nat_gateway_create_arguments(plan: RepairPlan) -> list[str]:
    return [
        "network",
        "nat",
        "gateway",
        "create",
        "--resource-group",
        plan.config.resource_group,
        "--name",
        plan.config.nat_gateway_name,
        "--location",
        plan.location,
        "--public-ip-addresses",
        plan.config.public_ip_name,
        "--idle-timeout",
        "4",
        "--sku",
        "Standard",
        "--only-show-errors",
        "--output",
        "none",
    ]


def public_ip_wait_arguments(plan: RepairPlan) -> list[str]:
    return [
        "network",
        "public-ip",
        "wait",
        "--resource-group",
        plan.config.resource_group,
        "--name",
        plan.config.public_ip_name,
        "--created",
        "--only-show-errors",
    ]


def nat_gateway_wait_arguments(plan: RepairPlan) -> list[str]:
    return [
        "network",
        "nat",
        "gateway",
        "wait",
        "--resource-group",
        plan.config.resource_group,
        "--name",
        plan.config.nat_gateway_name,
        "--created",
        "--only-show-errors",
    ]


def _preserved_nat_gateway(
    plan: RepairPlan, subnet_name: str
) -> str | None:
    normalized = subnet_name.casefold()
    return next(
        (
            nat_id
            for name, nat_id in plan.preserved_nat_gateways
            if name.casefold() == normalized
        ),
        None,
    )


def subnet_update_arguments(
    plan: RepairPlan,
    subnet_name: str,
    nat_gateway: str | None = None,
) -> list[str]:
    arguments = [
        "network",
        "vnet",
        "subnet",
        "update",
        "--resource-group",
        plan.config.resource_group,
        "--vnet-name",
        plan.config.vnet_name,
        "--name",
        subnet_name,
        "--default-outbound",
        "false",
    ]
    if nat_gateway is not None:
        arguments.extend(["--nat-gateway", nat_gateway])
    arguments.extend(["--only-show-errors", "--output", "none"])
    return arguments


def public_ip_show_arguments(plan: RepairPlan) -> list[str]:
    return [
        "network",
        "public-ip",
        "show",
        "--resource-group",
        plan.config.resource_group,
        "--name",
        plan.config.public_ip_name,
        "--query",
        "ipAddress",
        "--only-show-errors",
    ]


def subnet_show_arguments(plan: RepairPlan, subnet_name: str) -> list[str]:
    return [
        "network",
        "vnet",
        "subnet",
        "show",
        "--resource-group",
        plan.config.resource_group,
        "--vnet-name",
        plan.config.vnet_name,
        "--name",
        subnet_name,
        "--only-show-errors",
    ]


def _resource_property(resource: dict[str, Any], key: str) -> Any:
    if key in resource:
        return resource[key]
    properties = resource.get("properties")
    return properties.get(key) if isinstance(properties, dict) else None

def read_subnet(
    cli: AzureCLI, plan: RepairPlan, subnet_name: str
) -> SubnetState:
    payload = cli.run_json(subnet_show_arguments(plan, subnet_name))
    name = _required_text(payload, "name", "subnet recheck")
    raw_nat = _resource_property(payload, "natGateway")
    nat_id = (
        _required_text(raw_nat, "id", f"NAT association for subnet {name}")
        if raw_nat
        else None
    )
    default_outbound = _resource_property(
        payload, "defaultOutboundAccess"
    )
    if default_outbound is not None and not isinstance(
        default_outbound, bool
    ):
        raise RepairError(
            f"subnet {name} returned invalid defaultOutboundAccess"
        )
    return SubnetState(name, nat_id, default_outbound)


def repair_mutation_commands(
    cli: AzureCLI, plan: RepairPlan
) -> list[list[str]]:
    if not plan.update_subnets:
        return []
    arguments = []
    if plan.needs_generated_nat:
        arguments.extend(
            [
                public_ip_create_arguments(plan),
                public_ip_wait_arguments(plan),
                nat_gateway_create_arguments(plan),
                nat_gateway_wait_arguments(plan),
            ]
        )
    for subnet_name in plan.update_subnets:
        arguments.extend(
            [
                subnet_show_arguments(plan, subnet_name),
                subnet_update_arguments(
                    plan,
                    subnet_name,
                    (
                        None
                        if _preserved_nat_gateway(plan, subnet_name)
                        else plan.config.nat_gateway_name
                    ),
                ),
            ]
        )
    if plan.needs_generated_nat:
        arguments.append([*public_ip_show_arguments(plan), "--output", "tsv"])
    return [cli.command(command) for command in arguments]


def ensure_repair_resources(cli: AzureCLI, plan: RepairPlan) -> None:
    cli.run(public_ip_create_arguments(plan))
    cli.run(public_ip_wait_arguments(plan))
    cli.run(nat_gateway_create_arguments(plan))
    cli.run(nat_gateway_wait_arguments(plan))


def execute_repair_mutations(
    cli: AzureCLI,
    plan: RepairPlan,
) -> tuple[tuple[str, ...], tuple[SubnetState, ...], str | None]:
    updated: list[str] = []
    late_associations: list[SubnetState] = []
    resources_prepared = False
    generated_resources_used = False
    for subnet_name in plan.update_subnets:
        subnet = read_subnet(cli, plan, subnet_name)
        if subnet.nat_gateway_id:
            if subnet.default_outbound_access is False:
                late_associations.append(subnet)
                continue
            cli.run(
                subnet_update_arguments(plan, subnet.name)
            )
            updated.append(subnet.name)
            continue
        if not resources_prepared:
            ensure_repair_resources(cli, plan)
            resources_prepared = True
            generated_resources_used = True
            subnet = read_subnet(cli, plan, subnet_name)
            if subnet.nat_gateway_id:
                if subnet.default_outbound_access is False:
                    late_associations.append(subnet)
                    continue
                cli.run(
                    subnet_update_arguments(plan, subnet.name)
                )
                updated.append(subnet.name)
                continue
        cli.run(
            subnet_update_arguments(
                plan, subnet.name, plan.config.nat_gateway_name
            )
        )
        updated.append(subnet.name)

    public_ip = (
        cli.run_tsv(public_ip_show_arguments(plan))
        if updated and generated_resources_used
        else None
    )
    return tuple(updated), tuple(late_associations), public_ip


def _repair_result(
    plan: RepairPlan,
    *,
    dry_run: bool,
    lookup_performed: bool,
    commands: list[list[str]],
    public_ip: str | None,
    subscription_id: str | None,
    updated_subnets: tuple[str, ...],
    late_associations: tuple[SubnetState, ...],
) -> dict[str, Any]:
    return {
        "command": "repair-subnet-outbound",
        "dryRun": dry_run,
        "lookupPerformed": lookup_performed,
        "subscriptionId": subscription_id,
        "resourceGroup": plan.config.resource_group,
        "vnet": plan.config.vnet_name,
        "location": plan.location,
        "natGateway": plan.config.nat_gateway_name,
        "publicIpResource": plan.config.public_ip_name,
        "publicIp": public_ip,
        "selectedSubnets": list(plan.selected_subnets),
        "plannedUpdateSubnets": list(plan.update_subnets),
        "updatedSubnets": list(updated_subnets),
        "unchangedSubnets": [
            {
                "name": subnet.name,
                "natGatewayId": subnet.nat_gateway_id,
            }
            for subnet in plan.unchanged_subnets
        ],
        "lateAssociatedSubnets": [
            {
                "name": subnet.name,
                "natGatewayId": subnet.nat_gateway_id,
            }
            for subnet in late_associations
        ],
        "skippedReservedSubnets": list(plan.skipped_reserved_subnets),
        "commands": commands,
        "warnings": [REPAIR_RACE_WARNING] if plan.update_subnets else [],
    }


def render_human(result: dict[str, Any]) -> None:
    for warning in result.get("warnings", []):
        print(f"WARNING: {warning}")
    if result["skippedReservedSubnets"]:
        print(
            "Skipped reserved subnets: "
            + ", ".join(result["skippedReservedSubnets"])
        )
    for subnet in result["unchangedSubnets"]:
        print(
            f"Unchanged {subnet['name']}: already associated with "
            f"{subnet['natGatewayId']}"
        )
    for subnet in result["lateAssociatedSubnets"]:
        print(
            f"Skipped {subnet['name']}: a NAT was attached before update "
            f"({subnet['natGatewayId']})"
        )
    if result["dryRun"] and result["plannedUpdateSubnets"]:
        print(
            "Subnets to update: "
            + ", ".join(result["plannedUpdateSubnets"])
        )
    elif result["updatedSubnets"]:
        print("Updated subnets: " + ", ".join(result["updatedSubnets"]))
    else:
        print("No selected subnets need repair.")
    if result["dryRun"]:
        for command in result["commands"]:
            print(f"  {shlex.join(command)}")
        print("No Azure mutations were performed.")
    elif result["publicIp"] is not None:
        print(f"Outbound public IP: {result['publicIp'] or '(pending)'}")


def run_repair(
    config: RepairConfig,
    *,
    dry_run: bool,
    output: str,
    subscription: str | None,
    verbose: bool,
) -> int:
    cli = AzureCLI(subscription, verbose=verbose)
    needs_lookup = (
        not dry_run or config.all_subnets or config.location is None
    )
    subscription_id = (
        cli.current_subscription_id() if needs_lookup else None
    )
    vnet = read_vnet(cli, config) if needs_lookup else None
    plan = build_repair_plan(config, vnet)
    commands = repair_mutation_commands(cli, plan)
    public_ip = None
    updated_subnets = plan.update_subnets if dry_run else ()
    late_associations: tuple[SubnetState, ...] = ()
    if not dry_run and plan.update_subnets:
        (
            updated_subnets,
            late_associations,
            public_ip,
        ) = execute_repair_mutations(
            cli,
            plan,
        )

    result = _repair_result(
        plan,
        dry_run=dry_run,
        lookup_performed=needs_lookup,
        commands=commands,
        public_ip=public_ip,
        subscription_id=subscription_id,
        updated_subnets=updated_subnets,
        late_associations=late_associations,
    )
    if output == "json":
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        render_human(result)
    return 0
