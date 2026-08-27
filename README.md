# aci-spinup

Python scripts for compliant Azure Container Instance deployments.

`aci-spinup deploy` creates private ACI groups in a delegated subnet with:

- a NAT gateway and a `FirstPartyUsage=/NonProd` outbound public IP
- `defaultOutboundAccess=false`
- an NSG that permits configured ports from `CorpNetPublic`
- SSH on TCP port 22

The deployment has no public ingress resource. Connect from a network that can
route to the private subnet.

## Install

You need Python 3.11 or later, the Azure CLI, and an Azure login.

```console
az login
python3 -m pip install .
aci-spinup --help
```

The package installs these entrypoints:

- `aci-spinup deploy`
- `aci-spinup repair-subnet-outbound`
- `deploy-aci`, an alias for `aci-spinup deploy`

## Deploy ACI

```console
aci-spinup deploy \
  --name demo \
  --resource-group-prefix dev \
  --image ghcr.io/example/workload:latest \
  --ssh-key ~/.ssh/id_ed25519.pub
```

Deployments default to confidential ACI with the legacy allow-all development
CCE policy. Pass `--cce-policy-file PATH` to use a generated policy, or pass
`--sku standard` for standard ACI.

Use `--num-containers`, `--tcp-ports`, `--udp-ports`, and
`--azure-file-mount` to change the topology. Run `aci-spinup deploy --help` for
all options and limits.

To inspect the commands and ARM template without changing Azure, add
`--dry-run`. Add `--output-template PATH` to keep the generated template.

## Delete a deployment

Recreate the original command and add `--delete`:

```console
aci-spinup deploy \
  --name demo \
  --resource-group-prefix dev \
  --delete
```

Deletion works only for resource groups managed as a whole. It is unavailable
with `--use-existing-resource-group`.

The command compares the group's top-level ARM resources with the generated
topology twice. Unexpected resources stop deletion. The check does not inspect
nested resources, extension resources, or data-plane contents.

## Repair subnet outbound access

```console
aci-spinup repair-subnet-outbound \
  --resource-group network-rg \
  --vnet workload-vnet \
  --subnet app,workers
```

For each selected subnet, the command sets
`defaultOutboundAccess=false`. It preserves an existing NAT gateway. If no NAT
gateway is attached, it creates or reuses the configured compliant public IP
and NAT gateway, then attaches the NAT gateway.

Use `--all` instead of `--subnet` to select non-reserved subnets. Run
`aci-spinup repair-subnet-outbound --help` for all options.

## Check changes

```console
PYTHONPATH=src python3 -m unittest discover -s tests
python3 scripts/verify_template.py
```
