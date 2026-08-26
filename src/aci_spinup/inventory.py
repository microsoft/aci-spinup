from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .azure import AzureCLI
from .errors import InventoryError


def normalize_resource_id(resource_id: str) -> str:
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ValueError("resource ID must be a non-empty string")
    return resource_id.strip().rstrip("/").casefold()


def unexpected_resource_ids(
    expected_resource_ids: Iterable[str],
    actual_resource_ids: Iterable[str],
) -> list[str]:
    expected = {
        normalize_resource_id(resource_id)
        for resource_id in expected_resource_ids
    }
    actual_by_normalized: dict[str, str] = {}
    for resource_id in actual_resource_ids:
        actual_by_normalized.setdefault(
            normalize_resource_id(resource_id),
            resource_id.strip().rstrip("/"),
        )
    return sorted(
        (
            original
            for normalized, original in actual_by_normalized.items()
            if normalized not in expected
        ),
        key=str.casefold,
    )


def top_level_inventory_arguments(resource_group: str) -> list[str]:
    return [
        "resource",
        "list",
        "--resource-group",
        resource_group,
        "--only-show-errors",
    ]


def _is_top_level_resource_id(resource_id: str) -> bool:
    parts = resource_id.strip("/").split("/")
    if (
        len(parts) < 8
        or parts[0].casefold() != "subscriptions"
        or not parts[1]
        or parts[2].casefold() != "resourcegroups"
        or not parts[3]
        or parts[4].casefold() != "providers"
    ):
        raise InventoryError(
            f"Azure returned an invalid resource ID: {resource_id}"
        )
    provider_path = parts[5:]
    if (
        len(provider_path) < 3
        or any(not part for part in provider_path)
        or (len(provider_path) - 1) % 2
    ):
        raise InventoryError(
            f"Azure returned an invalid resource ID: {resource_id}"
        )
    return len(provider_path) == 3


def collect_top_level_resources(
    cli: AzureCLI, resource_group: str
) -> list[dict[str, Any]]:
    payload: Any = cli.run_json(
        top_level_inventory_arguments(resource_group)
    )
    if isinstance(payload, dict) and (
        payload.get("nextLink") or payload.get("nextToken")
    ):
        raise InventoryError(
            "top-level resource inventory returned an unconsumed page token"
        )
    if not isinstance(payload, list):
        raise InventoryError(
            "top-level resource inventory did not return a JSON list"
        )

    resources: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise InventoryError(
                "top-level resource inventory item is not an object"
            )
        resource_id = item.get("id")
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise InventoryError(
                "top-level resource inventory item is missing 'id'"
            )
        resource_id = resource_id.strip().rstrip("/")
        if _is_top_level_resource_id(resource_id):
            resources.append(item)
    return resources


def collect_top_level_resource_ids(
    cli: AzureCLI, resource_group: str
) -> set[str]:
    return {
        str(item["id"]).strip().rstrip("/")
        for item in collect_top_level_resources(cli, resource_group)
    }
