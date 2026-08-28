# aci-spinup

A collection of scripts that make it easier to deploy compliant Azure
Container Instance resources. Deployments use private ACI groups, compliant
NAT outbound access, and NSG rules for `CorpNetPublic`.

After installing the package, run a minimal deployment with:

```console
python3 -m pip install .
aci-spinup deploy \
  --resource-group-prefix dev \
  --image ghcr.io/example/workload:latest \
  --ssh-key ~/.ssh/id_ed25519.pub
```

To run it directly from the repository without installing:

```console
PYTHONPATH=src python3 -m aci_spinup deploy \
  --resource-group-prefix dev \
  --image ghcr.io/example/workload:latest \
  --ssh-key ~/.ssh/id_ed25519.pub
```

## Full example

```console
aci-spinup deploy \
  --subscription "Azure Research Subs" \
  --resource-group dev-cluster \
  --name cluster \
  --region northeurope \
  --image ghcr.io/example/workload:latest \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --sku standard \
  --cpus 16 \
  --ram 64 \
  --num-containers 3 \
  --install ubuntu \
  --tcp-ports 22,443,8080 \
  --udp-ports 5353 \
  --azure-file-mount share=workspace,path=/mnt/workspace \
  --azure-file-share-prefix \
  --azure-file-account-name uniqueaccountname123 \
  --output-template deployment.json \
  --output json \
  --verbose
```

Run `aci-spinup --help` or `aci-spinup <command> --help` for the full command
surface.
