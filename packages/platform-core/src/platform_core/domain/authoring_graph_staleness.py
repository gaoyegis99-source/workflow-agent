"""Frozen authoring-environment staleness checks."""

from __future__ import annotations


def environment_stale_reasons(frozen: dict, current: dict) -> list[str]:
    names = (
        "catalog_digest",
        "semantic_contract_digest",
        "compiler_digest",
        "validator_image_digest",
    )
    return [name.upper() + "_CHANGED" for name in names if frozen.get(name) != current.get(name)]
