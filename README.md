# aci-spinup

`aci-spinup` deploys one or more Azure Container Instances behind per-node
Standard public load balancers. The generated network uses a NAT gateway,
`defaultOutboundAccess=false`, and `CorpNetPublic` ingress rules.

## Install

You need Python 3.11 or later and the Azure CLI. Sign in before a real run.

```console
az login
python3 -m pip install .
aci-spinup --help
```

The `aci-spinup` entrypoint has two commands:

- `aci-spinup deploy`
- `aci-spinup repair-subnet-outbound`

The package also installs `deploy-aci` as a compatibility alias for
`aci-spinup deploy`.

Pass `--subscription NAME_OR_ID` to either command to select a subscription.

## Deploy

Deploy one confidential node into a resource group named `dev-demo`:

```console
aci-spinup deploy \
  --name demo \
  --resource-group-prefix dev \
  --image ghcr.io/example/workload:latest \
  --ssh-key ~/.ssh/id_ed25519.pub
```

Deployment names contain lowercase letters and numbers separated by single
hyphens. They must begin and end with a letter or number. The maximum is 53
characters, which keeps every generated ACI, deployment, load balancer,
public IP, VNet, NAT gateway, and NSG name within its provider limit at the
largest supported node suffix.

Use `--resource-group RG` for an exact name. Without
`--use-existing-resource-group`, `aci-spinup` runs `az group create`, which
creates the group or reuses it if it already exists. Pass
`--use-existing-resource-group` to skip that command. The flag also disables
whole-group deletion.

The script adds no ownership or deployment tags. The only generated tag is the
`FirstPartyUsage=/NonProd` public-IP tag required for compliance.

Each node receives a requested private address starting at `10.0.0.4`.
Azure can assign another address. The load balancer depends on the container
group and uses ARM `reference()` to register the address Azure assigned during
the same deployment.

### Ports and container OS

Pass comma-separated ports with `--tcp-ports` and `--udp-ports`. The generated
load balancer has one inbound rule for each requested port. TCP port 22 remains
open for SSH. UDP rules use the TCP port 22 health probe because Azure Load
Balancer does not support UDP health probes. ACI permits at most five effective
ports. The limit includes TCP port 22 when `aci-spinup` adds it.

All generated container groups attach to a VNet. Standard nodes permit at most
31 vCPU and 240 GB. Confidential nodes permit at most 31 vCPU and 180 GB.

`--install` controls package installation in the container:

- `azure-linux-3` uses `tdnf`. This mode is the default.
- `ubuntu` uses `apt-get`.
- `none` skips package installation.

All three modes write the public key, generate SSH host keys, enable root
public-key login, and start `sshd`. With `none`, the image must already contain
`ssh-keygen` and `/usr/sbin/sshd`. The image must run its configured command as
root; bootstrap exits with an explicit error otherwise. `--ssh-key` accepts one
valid OpenSSH public-key record. Human output prints an `ssh -i` helper only
when the public-key path ends in `.pub` and the corresponding private-key file
exists locally.

### Confidential policy

For legacy compatibility, confidential deployments default to an allow-all
development CCE policy. Human and JSON output warn when this policy is in use.
Do not use the default policy as a production trust boundary.

To use a generated policy, write its base64 value to a file and pass
`--cce-policy-file PATH`. `aci-spinup` validates and embeds the value but does
not run `confcom`, Docker, or another policy generator.

### Multiple nodes and Azure Files

```console
aci-spinup deploy \
  --name cluster \
  --resource-group-prefix dev \
  --image ghcr.io/example/workload:latest \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --sku standard \
  --num-containers 3 \
  --tcp-ports 22,443,8080 \
  --udp-ports 5353 \
  --azure-file-share-prefix \
  --azure-file-mount share=workspace,path=/mnt/workspace
```

One mount applies to every node. With `--azure-file-share-prefix`, the example
creates `workspace-1`, `workspace-2`, and `workspace-3`. Without that flag,
all nodes use the named share. You can instead repeat `--azure-file-mount`
once per node.

The ARM template creates one storage account and all file shares. It obtains
the account key at deployment time with an ARM `listKeys` expression. The
rendered template contains no storage account key.

The default storage account name remains deterministic for compatibility.
Storage account names are global in Azure. If that name is already taken,
pass a unique 3-24 character lowercase name with
`--azure-file-account-name NAME`. Use the same override when you reconstruct
the topology for deletion.

### Inspect a deployment plan

Dry-run mode performs no Azure lookup when all required values are local:

```console
aci-spinup deploy \
  --name demo \
  --resource-group-prefix dev \
  --image ghcr.io/example/workload:latest \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --dry-run \
  --output-template demo.json
```

Use `--output json` for machine-readable output. ARM deployments use
Incremental mode. Shrinking a previous topology is unsupported. A later
deployment does not remove resources left by a larger deployment. The
expanded template can contain at most 800 top-level ARM resources.

## Delete a resource group

Rebuild the deployed topology and request deletion:

```console
aci-spinup deploy \
  --name demo \
  --resource-group-prefix dev \
  --delete
```

Pass the original node, port, storage name, and storage share options if the
deployment did not use defaults.

Deletion safety is best-effort. `aci-spinup` runs
`az resource list --resource-group RG` twice and deletes the group only when
both top-level resource ID sets are subsets of the generated top-level
topology. Missing expected resources are allowed. Unexpected top-level
resources stop deletion and are listed. Any command, JSON, or pagination
failure stops deletion.

The check does not inspect nested ARM resources, extension resources, or
data-plane contents. Examples include VNet peerings, load balancer child
resources, locks, role assignments, policy assignments, diagnostic settings,
blobs, files, queue messages, and table entities. Azure deletes those with an
expected parent during whole-resource-group deletion.

`--dry-run` prints the top-level expected manifest and both inventory commands
without querying Azure. Whole-group deletion is unavailable with
`--use-existing-resource-group`.

The check and deletion are not one Azure transaction. A resource can still be
added after the second inventory and before Azure accepts group deletion. The
second pass reduces this race but cannot remove it.

## Repair subnet outbound access

Attach one generated NAT gateway to named subnets that have no NAT:

```console
aci-spinup repair-subnet-outbound \
  --resource-group network-rg \
  --vnet workload-vnet \
  --subnet app,workers
```

Use repeated `--subnet` values or use `--all`. The `--all` mode excludes
`GatewaySubnet`, `AzureFirewallSubnet`, `AzureFirewallManagementSubnet`,
`AzureBastionSubnet`, and `RouteServerSubnet`. To name one of those subnets,
also pass `--allow-reserved-subnets`.

The command preserves every existing NAT association. If an associated subnet
still has `defaultOutboundAccess` enabled or unset, the command disables it
without changing the NAT field. A subnet already using a NAT with
`defaultOutboundAccess=false` is unchanged and reported.
If a selected subnet has no NAT, the command creates or reuses the configured
Standard public IP and NAT gateway, then attaches that NAT while setting
`defaultOutboundAccess=false`. The public IP receives only the required
`FirstPartyUsage=/NonProd` IP tag.

Immediately before each subnet update, `aci-spinup` reads the subnet again. If
another process attached a NAT and already disabled default outbound access,
the command skips and reports that subnet. Otherwise, it leaves the NAT field
alone while disabling default outbound access. Azure CLI offers no conditional
ETag for this update, so a small race remains between the final read and the
update.

One generated NAT gateway supports up to 800 subnets. For a lookup-free dry
run, pass named subnets and `--location`:

```console
aci-spinup repair-subnet-outbound \
  --resource-group network-rg \
  --vnet workload-vnet \
  --subnet app \
  --location northeurope \
  --dry-run \
  --output json
```

## Verify the builder

Run the unit tests and compare the builder with the committed canonical
template:

```console
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/verify_template.py
```

If an intentional builder change affects the fixture, inspect the generated
template and then run `python3 scripts/verify_template.py --update`.
