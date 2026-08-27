from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import shlex
import sys
import uuid
from typing import Any

from .arm import (
    AzureFileMountSpec,
    DeployConfig,
    DeploymentTopology,
    Port,
    build_deployment_topology,
)
from .azure import AzureCLI
from .errors import DeploymentError, UnsafeDeletionError
from .inventory import (
    collect_top_level_resource_ids,
    top_level_inventory_arguments,
    unexpected_resource_ids,
)

LEGACY_CCE_WARNING = (
    "Confidential ACI is using the legacy allow-all development CCE policy. "
    "Supply --cce-policy-file with a generated policy for non-development use."
)
DELETION_RACE_WARNING = (
    "Resource-group deletion is not transactional. A resource can still be "
    "added after the second inventory pass and before Azure accepts deletion."
)
BEST_EFFORT_DELETION_WARNING = (
    "Deletion safety is best-effort and compares top-level ARM resources "
    "only. Nested resources, extension resources, and data-plane contents "
    "are not inspected and will be deleted with an expected parent."
)


@dataclass(frozen=True)
class DeployRequest:
    topology: DeploymentTopology
    delete: bool
    dry_run: bool
    verbose: bool
    output: str
    output_template: Path | None
    subscription: str | None
    use_existing_resource_group: bool
    ssh_private_key_path: str | None


def default_deployment_name() -> str:
    return f"test-{secrets.token_hex(2)}"


def add_deploy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", help="Container image used by every node")
    resource_group = parser.add_mutually_exclusive_group(required=True)
    resource_group.add_argument(
        "--resource-group", help="Use this exact resource group name"
    )
    resource_group.add_argument(
        "--resource-group-prefix",
        help="Use the resource group <prefix>-<deployment-name>",
    )
    parser.add_argument(
        "--ssh-key",
        help="SSH public key file. Required unless --delete is set",
    )
    parser.add_argument(
        "--name",
        default=default_deployment_name(),
        help=(
            "Deployment name: lowercase alphanumeric segments separated by "
            "single hyphens, at most 53 characters"
        ),
    )
    parser.add_argument(
        "--region",
        default="northeurope",
        help="Azure region (default: northeurope)",
    )
    parser.add_argument(
        "--cpus",
        type=int,
        default=4,
        help="vCPU per node (Standard max 31; Confidential max 31)",
    )
    parser.add_argument(
        "--ram",
        type=int,
        default=16,
        help="GB per node (Standard max 240; Confidential max 180)",
    )
    parser.add_argument(
        "--tcp-ports",
        default="22",
        help="Comma-separated TCP ports (default: 22)",
    )
    parser.add_argument(
        "--udp-ports", help="Comma-separated UDP ports"
    )
    parser.add_argument(
        "--num-containers",
        type=int,
        default=1,
        help="Number of ACI nodes (default: 1)",
    )
    parser.add_argument(
        "--sku",
        choices=("confidential", "standard"),
        default="confidential",
        help="ACI SKU (default: confidential)",
    )
    parser.add_argument(
        "--install",
        dest="install_mode",
        choices=("azure-linux-3", "ubuntu", "none"),
        default="azure-linux-3",
        help="Install SSH dependencies (default: azure-linux-3)",
    )
    parser.add_argument(
        "--azure-file-mount",
        action="append",
        default=[],
        metavar="share=NAME,path=/PATH",
        help=(
            "Azure Files mount. Repeat once per node, or pass once for all nodes"
        ),
    )
    parser.add_argument(
        "--azure-file-share-prefix",
        action="store_true",
        help="Derive one numbered share per node from share=NAME",
    )
    parser.add_argument(
        "--azure-file-account-sku",
        default="Standard_LRS",
        help="Storage account SKU (default: Standard_LRS)",
    )
    parser.add_argument(
        "--azure-file-account-name",
        help=(
            "Globally unique storage account name override for Azure Files"
        ),
    )
    parser.add_argument(
        "--cce-policy-file",
        type=Path,
        help=(
            "File containing a generated base64 CCE policy for confidential ACI"
        ),
    )
    parser.add_argument(
        "--use-existing-resource-group",
        action="store_true",
        help="Do not create or permit deletion of the exact resource group",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the whole resource group after inventory checks",
    )
    parser.add_argument(
        "--subscription", help="Azure subscription name or ID"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the template and Azure command plan without mutations",
    )
    parser.add_argument(
        "--output",
        choices=("human", "json"),
        default="human",
        help="Output format (default: human)",
    )
    parser.add_argument(
        "--output-template",
        type=Path,
        help="Write the rendered ARM template to this path",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print Azure commands to stderr",
    )


def effective_resource_group(args: argparse.Namespace) -> str:
    if args.resource_group is not None:
        return args.resource_group
    return f"{args.resource_group_prefix}-{args.name}"


def parse_ports(value: str | None, protocol: str) -> list[Port]:
    ports: list[Port] = []
    for raw in (value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            number = int(raw)
        except ValueError as exc:
            raise ValueError(f"{protocol} port is not an integer: {raw}") from exc
        ports.append(Port(protocol, number))
    return ports


def parse_azure_file_mount(value: str) -> AzureFileMountSpec:
    fields: dict[str, str] = {}
    for raw_field in value.split(","):
        if not raw_field.strip():
            continue
        key, separator, raw_value = raw_field.partition("=")
        key = key.strip()
        if not separator:
            raise ValueError("must contain comma-separated key=value fields")
        if key not in {"share", "path"}:
            raise ValueError(f"contains unsupported field '{key}'")
        if key in fields:
            raise ValueError(f"contains duplicate field '{key}'")
        fields[key] = raw_value.strip()
    if not fields.get("share"):
        raise ValueError("requires share=NAME")
    if not fields.get("path"):
        raise ValueError("requires path=/ABSOLUTE/PATH")
    return AzureFileMountSpec(fields["share"], fields["path"])


def _public_key(path_value: str, parser: argparse.ArgumentParser) -> str:
    path = Path(path_value).expanduser()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        parser.error(f"cannot read --ssh-key {path}: {exc}")
    if not value:
        parser.error(f"--ssh-key {path} is empty")
    if len(value.splitlines()) != 1:
        parser.error(f"--ssh-key {path} must contain exactly one public key")
    fields = value.split()
    if len(fields) < 2:
        parser.error(f"--ssh-key {path} is not an OpenSSH public key")
    key_type, encoded_key = fields[:2]
    if not (
        key_type.startswith(("ssh-", "ecdsa-sha2-", "sk-"))
        or "-cert-v01@openssh.com" in key_type
    ):
        parser.error(
            f"--ssh-key {path} uses unsupported key type {key_type}"
        )
    try:
        key_blob = base64.b64decode(encoded_key, validate=True)
    except (ValueError, binascii.Error) as exc:
        parser.error(f"--ssh-key {path} contains invalid base64: {exc}")
    if len(key_blob) < 4:
        parser.error(f"--ssh-key {path} contains an invalid key blob")
    type_length = int.from_bytes(key_blob[:4], byteorder="big")
    encoded_type = key_blob[4 : 4 + type_length]
    try:
        blob_type = encoded_type.decode("ascii")
    except UnicodeDecodeError:
        parser.error(f"--ssh-key {path} contains an invalid key type")
    if 4 + type_length > len(key_blob) or blob_type != key_type:
        parser.error(
            f"--ssh-key {path} key type does not match its encoded key"
        )
    return value


def _cce_policy(
    path: Path | None, parser: argparse.ArgumentParser
) -> str | None:
    if path is None:
        return None
    expanded_path = path.expanduser()
    try:
        value = "".join(
            expanded_path.read_text(encoding="utf-8").split()
        )
    except (OSError, UnicodeError) as exc:
        parser.error(f"cannot read --cce-policy-file {expanded_path}: {exc}")
    if not value:
        parser.error(f"--cce-policy-file {expanded_path} is empty")
    try:
        base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        parser.error(
            f"--cce-policy-file {expanded_path} must contain base64: {exc}"
        )
    return value


def request_from_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> DeployRequest:
    text_arguments = {
        "--resource-group": args.resource_group,
        "--resource-group-prefix": args.resource_group_prefix,
        "--name": args.name,
        "--region": args.region,
        "--subscription": args.subscription,
        "--azure-file-account-name": args.azure_file_account_name,
    }
    if not args.delete:
        text_arguments["--image"] = args.image
        text_arguments["--ssh-key"] = args.ssh_key
    for option, value in text_arguments.items():
        if value is not None and not str(value).strip():
            parser.error(f"{option} cannot be empty")
    if not args.delete and not args.image:
        parser.error("--image is required unless --delete is set")
    if not args.delete and not args.ssh_key:
        parser.error("--ssh-key is required unless --delete is set")
    if args.delete and args.use_existing_resource_group:
        parser.error("--delete cannot be used with --use-existing-resource-group")
    if args.use_existing_resource_group and not args.resource_group:
        parser.error(
            "--use-existing-resource-group requires --resource-group"
        )
    if args.cce_policy_file and args.sku != "confidential":
        parser.error("--cce-policy-file requires --sku confidential")

    try:
        ports = tuple(
            [
                *parse_ports(args.tcp_ports, "TCP"),
                *parse_ports(args.udp_ports, "UDP"),
            ]
        )
        mounts = tuple(
            parse_azure_file_mount(value)
            for value in args.azure_file_mount
        )
        ssh_public_key = (
            _public_key(args.ssh_key, parser)
            if not args.delete
            else "<ssh-public-key-required-for-deploy>"
        )
        config = DeployConfig(
            name=args.name,
            resource_group=effective_resource_group(args),
            location=args.region,
            image=args.image or "<image-required-for-deploy>",
            ssh_public_key=ssh_public_key,
            ports=ports,
            cpus=args.cpus,
            ram_gb=args.ram,
            node_count=args.num_containers,
            sku=args.sku,
            install_mode=args.install_mode,
            azure_file_mounts=mounts,
            azure_file_share_prefix=args.azure_file_share_prefix,
            azure_file_account_sku=args.azure_file_account_sku,
            azure_file_account_name=args.azure_file_account_name,
            cce_policy=_cce_policy(args.cce_policy_file, parser),
        )
        topology = build_deployment_topology(config)
    except ValueError as exc:
        parser.error(str(exc))

    private_key_path = None
    if args.ssh_key and args.ssh_key.endswith(".pub"):
        candidate = Path(args.ssh_key[: -len(".pub")]).expanduser()
        if candidate.is_file():
            private_key_path = str(candidate)
    return DeployRequest(
        topology=topology,
        delete=args.delete,
        dry_run=args.dry_run,
        verbose=args.verbose,
        output=args.output,
        output_template=args.output_template,
        subscription=args.subscription,
        use_existing_resource_group=args.use_existing_resource_group,
        ssh_private_key_path=private_key_path,
    )


def _group_create_arguments(request: DeployRequest) -> list[str]:
    config = request.topology.config
    return [
        "group",
        "create",
        "--name",
        config.resource_group,
        "--location",
        config.location,
        "--only-show-errors",
        "--output",
        "none",
    ]


def _group_wait_arguments(request: DeployRequest) -> list[str]:
    return [
        "group",
        "wait",
        "--name",
        request.topology.config.resource_group,
        "--created",
        "--only-show-errors",
    ]


def ensure_deployment_resource_group(
    request: DeployRequest, cli: AzureCLI
) -> str:
    cli.run(_group_create_arguments(request))
    cli.run(_group_wait_arguments(request))
    return "created-or-existing"


def deployment_warnings(request: DeployRequest) -> list[str]:
    warnings = []
    if request.topology.uses_legacy_cce_policy:
        warnings.append(LEGACY_CCE_WARNING)
    return warnings


def _deployment_arguments(
    request: DeployRequest, template_path: str
) -> list[str]:
    config = request.topology.config
    return [
        "deployment",
        "group",
        "create",
        "--name",
        f"{config.name}-deployment",
        "--resource-group",
        config.resource_group,
        "--mode",
        "Incremental",
        "--template-file",
        template_path,
        "--only-show-errors",
        "--output",
        "none",
    ]


def _container_ip_arguments(
    request: DeployRequest, container_group_name: str
) -> list[str]:
    return [
        "container",
        "show",
        "--resource-group",
        request.topology.config.resource_group,
        "--name",
        container_group_name,
        "--query",
        "ipAddress.ip",
        "--only-show-errors",
    ]


def _write_template(path: Path, topology: DeploymentTopology) -> None:
    try:
        path.write_text(topology.template.to_json(), encoding="utf-8")
    except OSError as exc:
        raise DeploymentError(f"cannot write ARM template {path}: {exc}") from exc


def _temporary_template_path() -> Path:
    return Path.cwd() / (
        f".aci-spinup-template-{os.getpid()}-{uuid.uuid4().hex}.json"
    )


def _dry_run_subscription_token(subscription: str | None) -> str:
    if subscription is None:
        return "<current-subscription-id>"
    try:
        return str(uuid.UUID(subscription))
    except ValueError:
        return "<resolved-subscription-id>"


def _command_step(command: list[str], purpose: str) -> dict[str, Any]:
    return {"purpose": purpose, "command": command}


def deployment_dry_run(
    request: DeployRequest, cli: AzureCLI
) -> dict[str, Any]:
    template_path = (
        str(request.output_template)
        if request.output_template
        else "<generated-template-file>"
    )
    steps: list[dict[str, Any]] = []
    if not request.use_existing_resource_group:
        steps.extend(
            [
                _command_step(
                    cli.command(_group_create_arguments(request)),
                    "create or reuse the resource group",
                ),
                _command_step(
                    cli.command(_group_wait_arguments(request)),
                    "wait for the resource group",
                ),
            ]
        )
    steps.append(
        _command_step(
            cli.command(_deployment_arguments(request, template_path)),
            "deploy ARM template in Incremental mode",
        )
    )
    for node in request.topology.nodes:
        steps.append(
            _command_step(
                cli.command(
                    [
                        *_container_ip_arguments(
                            request, node.container_group_name
                        ),
                        "--output",
                        "tsv",
                    ]
                ),
                f"read assigned private IP for {node.container_group_name}",
            )
        )
    subscription_token = _dry_run_subscription_token(request.subscription)
    return {
        "command": "deploy",
        "dryRun": True,
        "resourceGroup": request.topology.config.resource_group,
        "useExistingResourceGroup": request.use_existing_resource_group,
        "templatePath": (
            str(request.output_template) if request.output_template else None
        ),
        "template": request.topology.template.to_dict(),
        "expectedTopLevelResourceIds": sorted(
            request.topology.expected_top_level_resource_ids(
                subscription_token
            ),
            key=str.casefold,
        ),
        "steps": steps,
        "warnings": deployment_warnings(request),
    }


def _read_node_ips(
    request: DeployRequest, cli: AzureCLI
) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for node in request.topology.nodes:
        private_ip = cli.run_tsv(
            _container_ip_arguments(request, node.container_group_name)
        )
        if not private_ip:
            raise DeploymentError(
                f"{node.container_group_name} did not report a private IP; "
                "deployment result is incomplete"
            )
        mappings.append(
            {
                "containerGroup": node.container_group_name,
                "requestedPrivateIp": node.requested_private_ip,
                "privateIp": private_ip,
            }
        )
    return mappings


def execute_deployment(
    request: DeployRequest, cli: AzureCLI
) -> dict[str, Any]:
    subscription_id = cli.current_subscription_id()
    resource_group_action = "existing-unmanaged"
    if not request.use_existing_resource_group:
        resource_group_action = ensure_deployment_resource_group(request, cli)

    keep_template = request.output_template is not None
    template_path = request.output_template or _temporary_template_path()
    if not keep_template:
        try:
            _write_template(template_path, request.topology)
        except DeploymentError:
            template_path.unlink(missing_ok=True)
            raise
    try:
        cli.run(_deployment_arguments(request, str(template_path)))
    finally:
        if not keep_template:
            try:
                template_path.unlink()
            except FileNotFoundError:
                pass
    mappings = _read_node_ips(request, cli)
    return {
        "command": "deploy",
        "dryRun": False,
        "status": "deployed",
        "subscriptionId": subscription_id,
        "resourceGroup": request.topology.config.resource_group,
        "templatePath": (
            str(request.output_template) if request.output_template else None
        ),
        "resourceGroupAction": resource_group_action,
        "nodes": mappings,
        "warnings": deployment_warnings(request),
    }


def deletion_dry_run(
    request: DeployRequest, cli: AzureCLI
) -> dict[str, Any]:
    subscription_token = _dry_run_subscription_token(request.subscription)
    expected = sorted(
        request.topology.expected_top_level_resource_ids(
            subscription_token
        ),
        key=str.casefold,
    )
    steps: list[dict[str, Any]] = []
    for pass_number in (1, 2):
        steps.append(
            _command_step(
                cli.command(
                    [
                        *top_level_inventory_arguments(
                            request.topology.config.resource_group
                        ),
                        "--output",
                        "json",
                    ]
                ),
                f"read top-level ARM inventory pass {pass_number}",
            )
        )
    steps.append(
        _command_step(
            cli.command(
                [
                    "group",
                    "delete",
                    "--name",
                    request.topology.config.resource_group,
                    "--yes",
                    "--no-wait",
                    "--only-show-errors",
                ]
            ),
            "delete only after both inventory passes are safe",
        )
    )
    return {
        "command": "deploy-delete",
        "dryRun": True,
        "resourceGroup": request.topology.config.resource_group,
        "templatePath": (
            str(request.output_template) if request.output_template else None
        ),
        "template": request.topology.template.to_dict(),
        "expectedTopLevelResourceIds": expected,
        "steps": steps,
        "safetyCheck": (
            "both actual top-level resource ID inventories must be subsets of "
            "expectedTopLevelResourceIds"
        ),
        "inventoryScope": "top-level ARM resources only",
        "warnings": [
            BEST_EFFORT_DELETION_WARNING,
            DELETION_RACE_WARNING,
        ],
    }


def execute_deletion(
    request: DeployRequest, cli: AzureCLI
) -> dict[str, Any]:
    subscription_id = cli.current_subscription_id()
    expected = request.topology.expected_top_level_resource_ids(
        subscription_id
    )
    actual_first = collect_top_level_resource_ids(
        cli, request.topology.config.resource_group
    )
    unexpected = unexpected_resource_ids(expected, actual_first)
    if unexpected:
        raise UnsafeDeletionError(unexpected)

    actual_second = collect_top_level_resource_ids(
        cli, request.topology.config.resource_group
    )
    unexpected = unexpected_resource_ids(expected, actual_second)
    if unexpected:
        raise UnsafeDeletionError(unexpected)
    cli.run(
        [
            "group",
            "delete",
            "--name",
            request.topology.config.resource_group,
            "--yes",
            "--no-wait",
            "--only-show-errors",
        ]
    )
    return {
        "command": "deploy-delete",
        "dryRun": False,
        "status": "deletion-started",
        "subscriptionId": subscription_id,
        "resourceGroup": request.topology.config.resource_group,
        "expectedTopLevelResourceIds": sorted(expected, key=str.casefold),
        "actualTopLevelResourceIdsFirstPass": sorted(
            actual_first, key=str.casefold
        ),
        "actualTopLevelResourceIds": sorted(
            actual_second, key=str.casefold
        ),
        "unexpectedTopLevelResourceIds": [],
        "inventoryScope": "top-level ARM resources only",
        "warnings": [
            BEST_EFFORT_DELETION_WARNING,
            DELETION_RACE_WARNING,
        ],
    }


def render_human(result: dict[str, Any]) -> None:
    for warning in result.get("warnings", []):
        print(f"WARNING: {warning}")
    if result["dryRun"]:
        print(f"Dry run for resource group {result['resourceGroup']}")
        print("Azure command plan:")
        for step in result["steps"]:
            if "command" in step:
                print(f"  {shlex.join(step['command'])}")
            else:
                for alternative in step["commandAlternatives"]:
                    print(f"  {shlex.join(alternative)}")
        print("ARM template:")
        print(json.dumps(result["template"], indent=2, sort_keys=True))
        print("No Azure mutations were performed.")
        return

    if result["command"] == "deploy-delete":
        print(
            "Deletion started for resource group "
            f"{result['resourceGroup']} after both best-effort top-level "
            "inventory passes."
        )
        return

    print(f"Deployed resource group {result['resourceGroup']}.")
    for node in result["nodes"]:
        if node["privateIp"] != node["requestedPrivateIp"]:
            print(
                f"{node['containerGroup']}: Azure assigned private IP "
                f"{node['privateIp']} instead of "
                f"{node['requestedPrivateIp']}."
            )
        print(f"{node['containerGroup']}: private={node['privateIp']}")


def run_deploy(request: DeployRequest) -> int:
    cli = AzureCLI(request.subscription, verbose=request.verbose)
    if request.output_template:
        _write_template(request.output_template, request.topology)

    if request.delete:
        result = (
            deletion_dry_run(request, cli)
            if request.dry_run
            else execute_deletion(request, cli)
        )
    else:
        result = (
            deployment_dry_run(request, cli)
            if request.dry_run
            else execute_deployment(request, cli)
        )

    if request.output == "json":
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        render_human(result)
        if (
            not result["dryRun"]
            and result["command"] == "deploy"
            and request.ssh_private_key_path
        ):
            for node in result["nodes"]:
                print(
                    f"ssh -i {shlex.quote(request.ssh_private_key_path)} "
                    f"root@{node['privateIp']} -p 22"
                )
    return 0
