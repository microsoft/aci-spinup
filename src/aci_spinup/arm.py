from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from .bootstrap import INSTALL_MODES, ssh_bootstrap_command


NETWORK_API_VERSION = "2024-05-01"
ACI_API_VERSION = "2023-05-01"
STORAGE_API_VERSION = "2026-04-01"

PUBLIC_IP_TYPE = "Microsoft.Network/publicIPAddresses"
NAT_GATEWAY_TYPE = "Microsoft.Network/natGateways"
NETWORK_SECURITY_GROUP_TYPE = "Microsoft.Network/networkSecurityGroups"
VIRTUAL_NETWORK_TYPE = "Microsoft.Network/virtualNetworks"
SUBNET_TYPE = "Microsoft.Network/virtualNetworks/subnets"
CONTAINER_GROUP_TYPE = "Microsoft.ContainerInstance/containerGroups"
STORAGE_ACCOUNT_TYPE = "Microsoft.Storage/storageAccounts"
FILE_SHARE_TYPE = "Microsoft.Storage/storageAccounts/fileServices/shares"

# Kept for compatibility with the legacy tool. This policy allows all
# operations and is suitable only for development workloads.
LEGACY_ALLOW_ALL_DEVELOPMENT_CCE_POLICY = (
    "cGFja2FnZSBwb2xpY3kKCmFwaV9zdm4gOj0gIjAuMTAuMCIKZnJhbWV3b3JrX3N2"
    "biA6PSAiMC4xLjAiCgptb3VudF9kZXZpY2UgOj0geyJhbGxvd2VkIjogdHJ1ZX0K"
    "bW91bnRfb3ZlcmxheSA6PSB7ImFsbG93ZWQiOiB0cnVlfQpjcmVhdGVfY29udGFp"
    "bmVyIDo9IHsiYWxsb3dlZCI6IHRydWUsICJhbGxvd19zdGRpb19hY2Nlc3MiOiB0"
    "cnVlfQp1bm1vdW50X2RldmljZSA6PSB7ImFsbG93ZWQiOiB0cnVlfQp1bm1vdW50"
    "X292ZXJsYXkgOj0geyJhbGxvd2VkIjogdHJ1ZX0KZXhlY19pbl9jb250YWluZXIg"
    "Oj0geyJhbGxvd2VkIjogdHJ1ZX0KZXhlY19leHRlcm5hbCA6PSB7ImFsbG93ZWQi"
    "OiB0cnVlLCAiYWxsb3dfc3RkaW9fYWNjZXNzIjogdHJ1ZX0Kc2h1dGRvd25fY29u"
    "dGFpbmVyIDo9IHsiYWxsb3dlZCI6IHRydWV9CnNpZ25hbF9jb250YWluZXJfcHJv"
    "Y2VzcyA6PSB7ImFsbG93ZWQiOiB0cnVlfQpwbGFuOV9tb3VudCA6PSB7ImFsbG93"
    "ZWQiOiB0cnVlfQpwbGFuOV91bm1vdW50IDo9IHsiYWxsb3dlZCI6IHRydWV9Cmdl"
    "dF9wcm9wZXJ0aWVzIDo9IHsiYWxsb3dlZCI6IHRydWV9CmR1bXBfc3RhY2tzIDo9"
    "IHsiYWxsb3dlZCI6IHRydWV9CnJ1bnRpbWVfbG9nZ2luZyA6PSB7ImFsbG93ZWQi"
    "OiB0cnVlfQpsb2FkX2ZyYWdtZW50IDo9IHsiYWxsb3dlZCI6IHRydWV9CnNjcmF0"
    "Y2hfbW91bnQgOj0geyJhbGxvd2VkIjogdHJ1ZX0Kc2NyYXRjaF91bm1vdW50IDo9"
    "IHsiYWxsb3dlZCI6IHRydWV9Cg=="
)


def arm_resource_id(resource_type: str, *names: str) -> str:
    quoted = ", ".join(f"'{name}'" for name in names)
    return f"[resourceId('{resource_type}', {quoted})]"


@dataclass(frozen=True)
class Port:
    protocol: str
    number: int

    def __post_init__(self) -> None:
        protocol = self.protocol.upper()
        if protocol not in {"TCP", "UDP"}:
            raise ValueError(f"unsupported port protocol: {self.protocol}")
        if not 1 <= self.number <= 65535:
            raise ValueError(f"port must be between 1 and 65535: {self.number}")
        object.__setattr__(self, "protocol", protocol)

    @property
    def arm_protocol(self) -> str:
        return self.protocol.capitalize()

    @property
    def slug(self) -> str:
        return f"{self.protocol.lower()}-{self.number}"

    def to_aci_dict(self) -> dict[str, Any]:
        return {"protocol": self.protocol, "port": self.number}


@dataclass(frozen=True)
class AzureFileMountSpec:
    share_name: str
    mount_path: str


@dataclass(frozen=True)
class DeployConfig:
    name: str
    resource_group: str
    location: str
    image: str
    ssh_public_key: str
    ports: tuple[Port, ...] = (Port("TCP", 22),)
    cpus: int = 4
    ram_gb: int = 16
    node_count: int = 1
    sku: str = "confidential"
    install_mode: str = "azure-linux-3"
    azure_file_mounts: tuple[AzureFileMountSpec, ...] = ()
    azure_file_share_prefix: bool = False
    azure_file_account_sku: str = "Standard_LRS"
    azure_file_account_name: str | None = None
    cce_policy: str | None = None


@dataclass(frozen=True)
class SecurityRule:
    name: str
    priority: int
    port: Port

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "properties": {
                "priority": self.priority,
                "direction": "Inbound",
                "access": "Allow",
                "protocol": self.port.arm_protocol,
                "sourcePortRange": "*",
                "destinationPortRange": str(self.port.number),
                # CorpNetPublic is the approved service tag for inbound access.
                "sourceAddressPrefix": "CorpNetPublic",
                "destinationAddressPrefix": "*",
            },
        }


@dataclass(frozen=True)
class PublicIPAddress:
    name: str
    location: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": PUBLIC_IP_TYPE,
            "apiVersion": NETWORK_API_VERSION,
            "name": self.name,
            "location": self.location,
            "sku": {"name": "Standard"},
            "properties": {
                "publicIPAddressVersion": "IPv4",
                "publicIPAllocationMethod": "Static",
                "idleTimeoutInMinutes": 4,
                # First-party non-production public IPs require this tag.
                "ipTags": [{"ipTagType": "FirstPartyUsage", "tag": "/NonProd"}],
            },
        }


@dataclass(frozen=True)
class NatGateway:
    name: str
    location: str
    public_ip_name: str

    def to_dict(self) -> dict[str, Any]:
        public_ip_id = arm_resource_id(PUBLIC_IP_TYPE, self.public_ip_name)
        return {
            "type": NAT_GATEWAY_TYPE,
            "apiVersion": NETWORK_API_VERSION,
            "name": self.name,
            "location": self.location,
            "sku": {"name": "Standard"},
            "properties": {
                "publicIpAddresses": [{"id": public_ip_id}],
                "idleTimeoutInMinutes": 4,
            },
            "dependsOn": [public_ip_id],
        }


@dataclass(frozen=True)
class NetworkSecurityGroup:
    name: str
    location: str
    rules: tuple[SecurityRule, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": NETWORK_SECURITY_GROUP_TYPE,
            "apiVersion": NETWORK_API_VERSION,
            "name": self.name,
            "location": self.location,
            "properties": {
                "securityRules": [rule.to_dict() for rule in self.rules],
            },
        }


@dataclass(frozen=True)
class Subnet:
    name: str
    address_prefix: str
    nat_gateway_name: str
    nsg_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "properties": {
                "addressPrefix": self.address_prefix,
                # NAT, rather than implicit platform egress, owns outbound traffic.
                "defaultOutboundAccess": False,
                "natGateway": {
                    "id": arm_resource_id(NAT_GATEWAY_TYPE, self.nat_gateway_name)
                },
                "networkSecurityGroup": {
                    "id": arm_resource_id(
                        NETWORK_SECURITY_GROUP_TYPE, self.nsg_name
                    )
                },
                "delegations": [
                    {
                        "name": "aci-delegation",
                        "properties": {
                            "serviceName": (
                                "Microsoft.ContainerInstance/containerGroups"
                            )
                        },
                    }
                ],
            },
        }


@dataclass(frozen=True)
class VirtualNetwork:
    name: str
    location: str
    subnet: Subnet

    def to_dict(self) -> dict[str, Any]:
        dependencies = [
            arm_resource_id(NAT_GATEWAY_TYPE, self.subnet.nat_gateway_name),
            arm_resource_id(NETWORK_SECURITY_GROUP_TYPE, self.subnet.nsg_name),
        ]
        return {
            "type": VIRTUAL_NETWORK_TYPE,
            "apiVersion": NETWORK_API_VERSION,
            "name": self.name,
            "location": self.location,
            "properties": {
                "addressSpace": {"addressPrefixes": ["10.0.0.0/16"]},
                "subnets": [self.subnet.to_dict()],
            },
            "dependsOn": dependencies,
        }


@dataclass(frozen=True)
class StorageAccount:
    name: str
    location: str
    sku: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": STORAGE_ACCOUNT_TYPE,
            "apiVersion": STORAGE_API_VERSION,
            "name": self.name,
            "location": self.location,
            "sku": {"name": self.sku},
            "kind": storage_account_kind(self.sku),
            "properties": {
                "supportsHttpsTrafficOnly": True,
                "minimumTlsVersion": "TLS1_2",
                "allowBlobPublicAccess": False,
            },
        }


@dataclass(frozen=True)
class FileShare:
    storage_account_name: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        account_id = arm_resource_id(
            STORAGE_ACCOUNT_TYPE, self.storage_account_name
        )
        return {
            "type": FILE_SHARE_TYPE,
            "apiVersion": STORAGE_API_VERSION,
            "name": f"{self.storage_account_name}/default/{self.name}",
            "properties": {},
            "dependsOn": [account_id],
        }


@dataclass(frozen=True)
class ContainerGroup:
    name: str
    container_name: str
    location: str
    image: str
    ssh_public_key: str
    install_mode: str
    ports: tuple[Port, ...]
    cpus: int
    ram_gb: int
    sku: str
    vnet_name: str
    subnet_name: str
    private_ip: str
    cce_policy: str | None = None
    storage_account_name: str | None = None
    share_name: str | None = None
    mount_path: str | None = None
    volume_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        dependencies = [
            arm_resource_id(VIRTUAL_NETWORK_TYPE, self.vnet_name),
        ]
        volumes: list[dict[str, Any]] = []
        volume_mounts: list[dict[str, Any]] = []
        if self.storage_account_name and self.share_name:
            account_id = arm_resource_id(
                STORAGE_ACCOUNT_TYPE, self.storage_account_name
            )
            share_id = arm_resource_id(
                FILE_SHARE_TYPE,
                self.storage_account_name,
                "default",
                self.share_name,
            )
            dependencies.extend([account_id, share_id])
            volumes.append(
                {
                    "name": self.volume_name,
                    "azureFile": {
                        "shareName": self.share_name,
                        "storageAccountName": self.storage_account_name,
                        "storageAccountKey": storage_account_key_expression(
                            self.storage_account_name
                        ),
                    },
                }
            )
            volume_mounts.append(
                {
                    "name": self.volume_name,
                    "mountPath": self.mount_path,
                    "readOnly": False,
                }
            )

        properties: dict[str, Any] = {
            "sku": self.sku.capitalize(),
            "restartPolicy": "Never",
            "osType": "Linux",
            "ipAddress": {
                "ports": [port.to_aci_dict() for port in self.ports],
                "type": "Private",
                "ip": self.private_ip,
            },
            "subnetIds": [
                {
                    "id": arm_resource_id(
                        SUBNET_TYPE, self.vnet_name, self.subnet_name
                    )
                }
            ],
            "volumes": volumes,
            "containers": [
                {
                    "name": self.container_name,
                    "properties": {
                        "image": self.image,
                        "command": ssh_bootstrap_command(self.install_mode),
                        "ports": [
                            port.to_aci_dict() for port in self.ports
                        ],
                        "environmentVariables": [
                            {
                                "name": "SSH_ADMIN_KEY",
                                "value": self.ssh_public_key,
                            }
                        ],
                        "volumeMounts": volume_mounts,
                        "resources": {
                            "requests": {
                                "cpu": self.cpus,
                                "memoryInGB": self.ram_gb,
                            }
                        },
                        "securityContext": {"privileged": True},
                    },
                }
            ],
        }
        if self.sku == "confidential":
            properties["confidentialComputeProperties"] = {
                "ccePolicy": (
                    self.cce_policy
                    or LEGACY_ALLOW_ALL_DEVELOPMENT_CCE_POLICY
                )
            }

        return {
            "type": CONTAINER_GROUP_TYPE,
            "apiVersion": ACI_API_VERSION,
            "name": self.name,
            "location": self.location,
            "identity": {"type": "SystemAssigned"},
            "properties": properties,
            "dependsOn": dependencies,
        }


ArmResource = (
    PublicIPAddress
    | NatGateway
    | NetworkSecurityGroup
    | VirtualNetwork
    | StorageAccount
    | FileShare
    | ContainerGroup
)


@dataclass(frozen=True)
class ArmTemplate:
    resources: tuple[ArmResource, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": (
                "https://schema.management.azure.com/schemas/"
                "2019-04-01/deploymentTemplate.json#"
            ),
            "contentVersion": "1.0.0.0",
            "parameters": {},
            "variables": {},
            "resources": [resource.to_dict() for resource in self.resources],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class NodePlan:
    index: int
    container_group_name: str
    container_name: str
    requested_private_ip: str
    share_name: str | None


@dataclass(frozen=True)
class DeploymentTopology:
    config: DeployConfig
    template: ArmTemplate
    vnet_name: str
    subnet_name: str
    nat_gateway_name: str
    nat_public_ip_name: str
    nsg_name: str
    nodes: tuple[NodePlan, ...]
    storage_account_name: str | None
    share_names: tuple[str, ...]
    uses_legacy_cce_policy: bool

    def expected_top_level_resource_ids(
        self, subscription_id: str
    ) -> set[str]:
        base = (
            f"/subscriptions/{subscription_id}/resourceGroups/"
            f"{self.config.resource_group}/providers"
        )

        def top(provider_type: str, name: str) -> str:
            return f"{base}/{provider_type}/{name}"

        ids = {
            top(
                "Microsoft.Resources/deployments",
                f"{self.config.name}-deployment",
            ),
            top(PUBLIC_IP_TYPE, self.nat_public_ip_name),
            top(NAT_GATEWAY_TYPE, self.nat_gateway_name),
            top(NETWORK_SECURITY_GROUP_TYPE, self.nsg_name),
            top(VIRTUAL_NETWORK_TYPE, self.vnet_name),
        }

        if self.storage_account_name:
            ids.add(
                top(STORAGE_ACCOUNT_TYPE, self.storage_account_name)
            )

        for node in self.nodes:
            ids.add(
                top(CONTAINER_GROUP_TYPE, node.container_group_name)
            )
        return ids


def storage_account_kind(sku: str) -> str:
    return "FileStorage" if sku.startswith("Premium") else "StorageV2"


def storage_account_key_expression(account_name: str) -> str:
    return (
        "[listKeys(resourceId("
        f"'{STORAGE_ACCOUNT_TYPE}', '{account_name}'), "
        f"'{STORAGE_API_VERSION}').keys[0].value]"
    )


def derived_storage_account_name(
    resource_group: str, deployment_name: str
) -> str:
    seed = f"{resource_group}-{deployment_name}".lower()
    sanitized = "".join(character for character in seed if character.isalnum())
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]
    prefix = sanitized[:18]
    return (f"{prefix}{digest}" if prefix else f"aci{digest}")[:24]


def resource_name_prefix_from_vnet_name(vnet_name: str) -> str:
    return vnet_name[: -len("-vnet")] if vnet_name.endswith("-vnet") else vnet_name


def nat_gateway_name_for_vnet(vnet_name: str) -> str:
    return f"{resource_name_prefix_from_vnet_name(vnet_name)}-nat"


def nat_public_ip_name_for_vnet(vnet_name: str) -> str:
    return f"{resource_name_prefix_from_vnet_name(vnet_name)}-nat-ip"


def effective_ports(ports: tuple[Port, ...]) -> tuple[Port, ...]:
    deduplicated: list[Port] = []
    seen: set[tuple[str, int]] = set()
    for port in ports:
        key = (port.protocol, port.number)
        if key not in seen:
            seen.add(key)
            deduplicated.append(port)
    if ("TCP", 22) not in seen:
        deduplicated.insert(0, Port("TCP", 22))
    if len(deduplicated) > 5:
        raise ValueError(
            "ACI supports at most 5 effective ports, including injected TCP/22"
        )
    return tuple(deduplicated)


def derived_share_name(prefix: str, node_index: int) -> str:
    return f"{prefix.rstrip('-')}-{node_index + 1}"


def validate_share_name(name: str) -> None:
    if not re.fullmatch(
        r"(?=.{3,63}\Z)[a-z0-9]+(?:-[a-z0-9]+)*", name
    ):
        raise ValueError(
            "Azure file share names must be 3-63 lowercase letters, "
            "numbers, or hyphens; each hyphen must be surrounded by a "
            "letter or number"
        )


def validate_storage_account_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9]{3,24}", name):
        raise ValueError(
            "Azure storage account names must be 3-24 lowercase letters "
            "or numbers"
        )


def validate_deployment_name(name: str, node_count: int) -> None:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError(
            "deployment name must contain lowercase letters or numbers "
            "separated by single hyphens, and must begin and end with a "
            "letter or number"
        )

    last_node_number = node_count
    last_node_index = node_count - 1
    container_group_name = f"{name}-{last_node_number}"
    generated_names = (
        ("ARM deployment", f"{name}-deployment", 64),
        ("container group", container_group_name, 63),
        ("container", f"{name}-{last_node_index}-0", 63),
        ("virtual network", f"{name}-vnet", 64),
        ("NAT gateway", f"{name}-nat", 80),
        ("NAT public IP", f"{name}-nat-ip", 80),
        ("network security group", f"{name}-vnet-nsg", 80),
    )
    for label, generated_name, maximum in generated_names:
        if len(generated_name) > maximum:
            raise ValueError(
                f"deployment name is too long: generated {label} name "
                f"'{generated_name}' exceeds {maximum} characters"
            )


def validate_deploy_config(config: DeployConfig) -> None:
    if config.node_count < 1 or config.node_count > 251:
        raise ValueError("node count must be between 1 and 251 for the /24 subnet")
    validate_deployment_name(config.name, config.node_count)
    if config.cpus < 1:
        raise ValueError("CPUs must be at least 1")
    if config.ram_gb < 1:
        raise ValueError("RAM must be at least 1 GB")
    if config.sku not in {"standard", "confidential"}:
        raise ValueError(f"unsupported ACI SKU: {config.sku}")
    cpu_limit, ram_limit = {
        "standard": (31, 240),
        "confidential": (31, 180),
    }[config.sku]
    if config.cpus > cpu_limit:
        raise ValueError(
            f"VNet-attached {config.sku.capitalize()} ACI supports at most "
            f"{cpu_limit} vCPU per node"
        )
    if config.ram_gb > ram_limit:
        raise ValueError(
            f"VNet-attached {config.sku.capitalize()} ACI supports at most "
            f"{ram_limit} GB per node"
        )
    if config.cce_policy is not None:
        if config.sku != "confidential":
            raise ValueError("a custom CCE policy requires confidential ACI")
        if not config.cce_policy.strip():
            raise ValueError("custom CCE policy cannot be empty")
        try:
            base64.b64decode(config.cce_policy, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                "custom CCE policy must be valid base64"
            ) from exc
    if config.install_mode not in INSTALL_MODES:
        raise ValueError(f"unsupported install mode: {config.install_mode}")
    if len(config.azure_file_mounts) > 1 and (
        len(config.azure_file_mounts) != config.node_count
    ):
        raise ValueError(
            "multiple Azure Files mounts must match the number of nodes"
        )
    if config.azure_file_share_prefix and not config.azure_file_mounts:
        raise ValueError(
            "Azure Files share prefix mode requires an Azure Files mount"
        )
    if config.azure_file_account_name:
        if not config.azure_file_mounts:
            raise ValueError(
                "an Azure Files account name requires an Azure Files mount"
            )
        validate_storage_account_name(config.azure_file_account_name)
    for mount in config.azure_file_mounts:
        if not mount.mount_path.startswith("/"):
            raise ValueError("Azure Files mount paths must be absolute")
        if not config.azure_file_share_prefix:
            validate_share_name(mount.share_name)
    effective_ports(config.ports)


def build_deployment_topology(config: DeployConfig) -> DeploymentTopology:
    validate_deploy_config(config)
    ports = effective_ports(config.ports)
    vnet_name = f"{config.name}-vnet"
    subnet_name = "default"
    nat_name = nat_gateway_name_for_vnet(vnet_name)
    nat_ip_name = nat_public_ip_name_for_vnet(vnet_name)
    nsg_name = f"{vnet_name}-nsg"
    rules = tuple(
        SecurityRule(
            name=f"allow-{port.slug}",
            priority=100 + index * 10,
            port=port,
        )
        for index, port in enumerate(ports)
    )

    resources: list[ArmResource] = [
        PublicIPAddress(nat_ip_name, config.location),
        NatGateway(nat_name, config.location, nat_ip_name),
        NetworkSecurityGroup(nsg_name, config.location, rules),
        VirtualNetwork(
            vnet_name,
            config.location,
            Subnet(subnet_name, "10.0.0.0/24", nat_name, nsg_name),
        ),
    ]

    storage_account_name: str | None = None
    share_names: list[str] = []
    node_mounts: list[tuple[str, str] | None] = [None] * config.node_count
    if config.azure_file_mounts:
        storage_account_name = (
            config.azure_file_account_name
            or derived_storage_account_name(
                config.resource_group, config.name
            )
        )
        resources.append(
            StorageAccount(
                storage_account_name,
                config.location,
                config.azure_file_account_sku,
            )
        )
        seen_shares: set[str] = set()
        for node_index in range(config.node_count):
            source_mount = (
                config.azure_file_mounts[0]
                if len(config.azure_file_mounts) == 1
                else config.azure_file_mounts[node_index]
            )
            share_name = (
                derived_share_name(source_mount.share_name, node_index)
                if config.azure_file_share_prefix
                else source_mount.share_name
            )
            validate_share_name(share_name)
            node_mounts[node_index] = (share_name, source_mount.mount_path)
            if share_name not in seen_shares:
                seen_shares.add(share_name)
                share_names.append(share_name)
                resources.append(FileShare(storage_account_name, share_name))

    nodes: list[NodePlan] = []
    for node_index in range(config.node_count):
        container_group_name = f"{config.name}-{node_index + 1}"
        container_name = f"{config.name}-{node_index}-0"
        private_ip = f"10.0.0.{node_index + 4}"
        mount = node_mounts[node_index]
        share_name = mount[0] if mount else None
        mount_path = mount[1] if mount else None
        resources.append(
            ContainerGroup(
                name=container_group_name,
                container_name=container_name,
                location=config.location,
                image=config.image,
                ssh_public_key=config.ssh_public_key,
                install_mode=config.install_mode,
                ports=ports,
                cpus=config.cpus,
                ram_gb=config.ram_gb,
                sku=config.sku,
                vnet_name=vnet_name,
                subnet_name=subnet_name,
                private_ip=private_ip,
                cce_policy=config.cce_policy,
                storage_account_name=storage_account_name,
                share_name=share_name,
                mount_path=mount_path,
                volume_name=(
                    f"azurefiles{node_index + 1}" if mount else None
                ),
            )
        )
        nodes.append(
            NodePlan(
                index=node_index,
                container_group_name=container_group_name,
                container_name=container_name,
                requested_private_ip=private_ip,
                share_name=share_name,
            )
        )

    return DeploymentTopology(
        config=config,
        template=ArmTemplate(tuple(resources)),
        vnet_name=vnet_name,
        subnet_name=subnet_name,
        nat_gateway_name=nat_name,
        nat_public_ip_name=nat_ip_name,
        nsg_name=nsg_name,
        nodes=tuple(nodes),
        storage_account_name=storage_account_name,
        share_names=tuple(share_names),
        uses_legacy_cce_policy=(
            config.sku == "confidential" and config.cce_policy is None
        ),
    )
