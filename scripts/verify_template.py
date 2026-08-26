#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aci_spinup.arm import (  # noqa: E402
    AzureFileMountSpec,
    DeployConfig,
    Port,
    build_deployment_topology,
)


FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "canonical-template.json"


def canonical_template_json() -> str:
    config = DeployConfig(
        name="canonical",
        resource_group="aci-canonical",
        location="northeurope",
        image="example.invalid/aci-spinup/canonical:1",
        ssh_public_key="ssh-ed25519 CANONICAL fixture@example.invalid",
        ports=(Port("TCP", 22), Port("TCP", 443), Port("UDP", 53)),
        cpus=8,
        ram_gb=32,
        node_count=2,
        sku="confidential",
        install_mode="ubuntu",
        azure_file_mounts=(
            AzureFileMountSpec("workspace", "/mnt/workspace"),
        ),
        azure_file_share_prefix=True,
        azure_file_account_sku="Premium_LRS",
        azure_file_account_name="acicanonicalfiles01",
        cce_policy="Y2Fub25pY2FsLWN1c3RvbS1jY2UtcG9saWN5",
    )
    return build_deployment_topology(config).template.to_json()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the deterministic canonical ARM template fixture."
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Replace the fixture with the current builder output",
    )
    args = parser.parse_args(argv)
    rendered = canonical_template_json()
    if args.update:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(rendered, encoding="utf-8")
        print(f"Updated {FIXTURE_PATH.relative_to(REPOSITORY_ROOT)}")
        return 0
    try:
        fixture = FIXTURE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(
            f"Missing {FIXTURE_PATH.relative_to(REPOSITORY_ROOT)}. "
            "Run with --update.",
            file=sys.stderr,
        )
        return 1
    if fixture != rendered:
        print(
            "Canonical ARM template changed. "
            f"fixture sha256={digest(fixture)} "
            f"rendered sha256={digest(rendered)}. "
            "Review the change, then run scripts/verify_template.py --update.",
            file=sys.stderr,
        )
        return 1
    print(
        "Canonical ARM template matches "
        f"{FIXTURE_PATH.relative_to(REPOSITORY_ROOT)} "
        f"(sha256={digest(rendered)})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
