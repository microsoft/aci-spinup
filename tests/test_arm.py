import json
import unittest

from aci_spinup.arm import (
    ACI_API_VERSION,
    FILE_SHARE_TYPE,
    LEGACY_ALLOW_ALL_DEVELOPMENT_CCE_POLICY,
    STORAGE_ACCOUNT_TYPE,
    STORAGE_API_VERSION,
    AzureFileMountSpec,
    DeployConfig,
    Port,
    build_deployment_topology,
)
from aci_spinup.bootstrap import ssh_bootstrap_script


def resource(template, resource_type, name=None):
    matches = [
        item
        for item in template["resources"]
        if item["type"] == resource_type
        and (name is None or item["name"] == name)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {resource_type} resource named {name}, got {matches}"
        )
    return matches[0]


class ArmTemplateTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "name": "demo",
            "resource_group": "rg-demo",
            "location": "northeurope",
            "image": "example.invalid/image:latest",
            "ssh_public_key": "ssh-ed25519 TEST test@example",
        }
        values.update(overrides)
        return DeployConfig(**values)

    def test_network_compliance_and_identity(self):
        template = build_deployment_topology(self.config()).template.to_dict()
        public_ips = [
            item
            for item in template["resources"]
            if item["type"] == "Microsoft.Network/publicIPAddresses"
        ]
        self.assertTrue(
            all("tags" not in item for item in template["resources"])
        )
        self.assertEqual(1, len(public_ips))
        for public_ip in public_ips:
            self.assertEqual(
                [{"ipTagType": "FirstPartyUsage", "tag": "/NonProd"}],
                public_ip["properties"]["ipTags"],
            )

        vnet = resource(
            template, "Microsoft.Network/virtualNetworks", "demo-vnet"
        )
        subnet = vnet["properties"]["subnets"][0]["properties"]
        self.assertIs(False, subnet["defaultOutboundAccess"])
        self.assertNotIn("defaultoutboundaccess", subnet)
        self.assertIn("natGateway", subnet)

        nsg = resource(
            template,
            "Microsoft.Network/networkSecurityGroups",
            "demo-vnet-nsg",
        )
        self.assertTrue(nsg["properties"]["securityRules"])
        self.assertTrue(
            all(
                rule["properties"]["sourceAddressPrefix"] == "CorpNetPublic"
                for rule in nsg["properties"]["securityRules"]
            )
        )
        group = resource(
            template, "Microsoft.ContainerInstance/containerGroups", "demo-1"
        )
        self.assertEqual(ACI_API_VERSION, group["apiVersion"])
        self.assertEqual({"type": "SystemAssigned"}, group["identity"])
        self.assertEqual("10.0.0.4", group["properties"]["ipAddress"]["ip"])
        self.assertIn("confidentialComputeProperties", group["properties"])
        self.assertEqual(
            LEGACY_ALLOW_ALL_DEVELOPMENT_CCE_POLICY,
            group["properties"]["confidentialComputeProperties"]["ccePolicy"],
        )

    def test_standard_multi_node_topology_omits_confidential_policy(self):
        topology = build_deployment_topology(
            self.config(node_count=3, sku="standard")
        )
        resources = topology.template.to_dict()["resources"]
        groups = [
            item
            for item in resources
            if item["type"]
            == "Microsoft.ContainerInstance/containerGroups"
        ]
        self.assertEqual(
            ["demo-1", "demo-2", "demo-3"],
            [group["name"] for group in groups],
        )
        self.assertEqual(
            ["10.0.0.4", "10.0.0.5", "10.0.0.6"],
            [group["properties"]["ipAddress"]["ip"] for group in groups],
        )
        self.assertTrue(
            all(
                "confidentialComputeProperties" not in group["properties"]
                for group in groups
            )
        )
        self.assertEqual(
            1,
            sum(
                item["type"] == "Microsoft.Network/publicIPAddresses"
                for item in resources
            ),
        )
        self.assertFalse(
            any(
                item["type"] == "Microsoft.Network/loadBalancers"
                for item in resources
            )
        )

    def test_all_ports_are_exposed_by_aci_and_nsg(self):
        topology = build_deployment_topology(
            self.config(
                ports=(
                    Port("TCP", 8080),
                    Port("UDP", 53),
                    Port("UDP", 8080),
                )
            )
        )
        template = topology.template.to_dict()
        nsg = resource(
            template,
            "Microsoft.Network/networkSecurityGroups",
            "demo-vnet-nsg",
        )
        rules = nsg["properties"]["securityRules"]
        self.assertEqual(
            {
                "allow-tcp-22",
                "allow-tcp-8080",
                "allow-udp-53",
                "allow-udp-8080",
            },
            {rule["name"] for rule in rules},
        )
        self.assertEqual(
            {"Tcp", "Udp"},
            {rule["properties"]["protocol"] for rule in rules},
        )
        container = resource(
            template, "Microsoft.ContainerInstance/containerGroups", "demo-1"
        )
        self.assertEqual(
            {
                ("TCP", 22),
                ("TCP", 8080),
                ("UDP", 53),
                ("UDP", 8080),
            },
            {
                (port["protocol"], port["port"])
                for port in container["properties"]["ipAddress"]["ports"]
            },
        )

    def test_effective_ports_are_limited_to_five_including_ssh(self):
        build_deployment_topology(
            self.config(
                ports=(
                    Port("TCP", 22),
                    Port("TCP", 80),
                    Port("TCP", 443),
                    Port("UDP", 53),
                    Port("UDP", 123),
                )
            )
        )
        with self.assertRaisesRegex(ValueError, "at most 5 effective ports"):
            build_deployment_topology(
                self.config(
                    ports=tuple(
                        Port("UDP", number)
                        for number in (53, 123, 500, 4500, 5353)
                    )
                )
            )

    def test_storage_is_arm_managed_and_key_is_expression(self):
        topology = build_deployment_topology(
            self.config(
                node_count=2,
                azure_file_mounts=(
                    AzureFileMountSpec("workspace", "/mnt/workspace"),
                ),
                azure_file_share_prefix=True,
                azure_file_account_sku="Premium_LRS",
            )
        )
        template = topology.template.to_dict()
        account = resource(template, STORAGE_ACCOUNT_TYPE)
        self.assertEqual(STORAGE_API_VERSION, account["apiVersion"])
        self.assertEqual("FileStorage", account["kind"])
        shares = [
            item
            for item in template["resources"]
            if item["type"] == FILE_SHARE_TYPE
        ]
        self.assertEqual(2, len(shares))
        self.assertEqual(
            {
                f"{account['name']}/default/workspace-1",
                f"{account['name']}/default/workspace-2",
            },
            {share["name"] for share in shares},
        )
        account_dependency = (
            f"[resourceId('{STORAGE_ACCOUNT_TYPE}', '{account['name']}')]"
        )
        self.assertTrue(
            all(share["dependsOn"] == [account_dependency] for share in shares)
        )

        group = resource(
            template, "Microsoft.ContainerInstance/containerGroups", "demo-1"
        )
        key = group["properties"]["volumes"][0]["azureFile"][
            "storageAccountKey"
        ]
        self.assertEqual(
            (
                "[listKeys(resourceId('Microsoft.Storage/storageAccounts', "
                f"'{account['name']}'), '{STORAGE_API_VERSION}').keys[0].value]"
            ),
            key,
        )
        self.assertIn(account_dependency, group["dependsOn"])
        self.assertIn("fileServices/shares", " ".join(group["dependsOn"]))
        rendered = json.dumps(template)
        self.assertNotIn("plaintext-storage-key", rendered)

    def test_short_share_prefix_is_valid_after_node_suffix(self):
        topology = build_deployment_topology(
            self.config(
                azure_file_mounts=(
                    AzureFileMountSpec("ws-", "/mnt/workspace"),
                ),
                azure_file_share_prefix=True,
            )
        )
        self.assertEqual(("ws-1",), topology.share_names)

    def test_storage_account_name_override_is_used_and_validated(self):
        topology = build_deployment_topology(
            self.config(
                azure_file_mounts=(
                    AzureFileMountSpec("workspace", "/mnt/workspace"),
                ),
                azure_file_account_name="globallyunique123",
            )
        )
        self.assertEqual(
            "globallyunique123", topology.storage_account_name
        )
        with self.assertRaisesRegex(ValueError, "3-24 lowercase"):
            build_deployment_topology(
                self.config(
                    azure_file_mounts=(
                        AzureFileMountSpec("workspace", "/mnt/workspace"),
                    ),
                    azure_file_account_name="Invalid_Name",
                )
            )

    def test_custom_cce_policy_is_emitted(self):
        topology = build_deployment_topology(
            self.config(cce_policy="Y3VzdG9tLXBvbGljeQ==")
        )
        group = resource(
            topology.template.to_dict(),
            "Microsoft.ContainerInstance/containerGroups",
            "demo-1",
        )
        self.assertEqual(
            "Y3VzdG9tLXBvbGljeQ==",
            group["properties"]["confidentialComputeProperties"]["ccePolicy"],
        )
        self.assertFalse(topology.uses_legacy_cce_policy)
        with self.assertRaisesRegex(ValueError, "valid base64"):
            build_deployment_topology(
                self.config(cce_policy="not-base64!")
            )

    def test_vnet_attached_sku_compute_limits(self):
        for sku, cpu_limit, ram_limit in (
            ("standard", 31, 240),
            ("confidential", 31, 180),
        ):
            build_deployment_topology(
                self.config(
                    sku=sku,
                    cpus=cpu_limit,
                    ram_gb=ram_limit,
                )
            )
            with self.assertRaisesRegex(ValueError, "vCPU per node"):
                build_deployment_topology(
                    self.config(sku=sku, cpus=cpu_limit + 1)
                )
            with self.assertRaisesRegex(ValueError, "GB per node"):
                build_deployment_topology(
                    self.config(sku=sku, ram_gb=ram_limit + 1)
                )

    def test_deployment_name_must_be_aci_compatible(self):
        for invalid_name in (
            "Uppercase",
            "two--hyphens",
            "trailing-",
        ):
            with self.subTest(invalid_name=invalid_name):
                with self.assertRaisesRegex(
                    ValueError, "deployment name must contain lowercase"
                ):
                    build_deployment_topology(
                        self.config(name=invalid_name)
                    )

    def test_deployment_name_keeps_all_generated_names_in_limits(self):
        build_deployment_topology(
            self.config(name="a" * 53, node_count=251)
        )
        with self.assertRaisesRegex(
            ValueError, "generated ARM deployment.*exceeds 64"
        ):
            build_deployment_topology(
                self.config(name="a" * 54, node_count=251)
            )

    def test_expected_top_level_manifest_excludes_nested_resources(self):
        topology = build_deployment_topology(
            self.config(
                azure_file_mounts=(
                    AzureFileMountSpec("workspace", "/mnt/workspace"),
                )
            )
        )
        ids = topology.expected_top_level_resource_ids(
            "00000000-0000-0000-0000-000000000000"
        )
        joined = "\n".join(sorted(ids))
        self.assertIn("/virtualNetworks/demo-vnet", joined)
        self.assertIn("/storageAccounts/", joined)
        self.assertNotIn("/loadBalancers/", joined)
        self.assertNotIn("/publicIPAddresses/demo-1", joined)
        self.assertNotIn("/subnets/", joined)
        self.assertNotIn("/securityRules/", joined)
        self.assertNotIn("/backendAddressPools/", joined)
        self.assertNotIn("/fileServices/", joined)


class BootstrapTests(unittest.TestCase):
    def test_azure_linux_3_uses_tdnf(self):
        script = ssh_bootstrap_script("azure-linux-3")
        self.assertIn("tdnf install -y openssh-server", script)
        self.assertNotIn("apt-get install", script)

    def test_ubuntu_uses_apt(self):
        script = ssh_bootstrap_script("ubuntu")
        self.assertIn("apt-get install -y", script)
        self.assertNotIn("tdnf install", script)

    def test_none_skips_package_install_but_configures_ssh(self):
        script = ssh_bootstrap_script("none")
        self.assertNotIn("apt-get", script)
        self.assertNotIn("tdnf", script)
        self.assertIn("authorized_keys", script)
        self.assertIn("ssh-keygen -A", script)
        self.assertIn("PermitRootLogin=yes", script)
        self.assertIn("/usr/sbin/sshd -D", script)

    def test_bootstrap_fails_clearly_when_image_user_is_not_root(self):
        script = ssh_bootstrap_script("none")
        self.assertIn('"$(id -u)" -ne 0', script)
        self.assertIn("requires the container image to run as root", script)


if __name__ == "__main__":
    unittest.main()
