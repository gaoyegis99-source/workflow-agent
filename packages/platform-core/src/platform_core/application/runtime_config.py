"""Deployment loading for the frozen P6 Agent runtime profile."""

from __future__ import annotations

import json
from pathlib import Path

from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import require, valid_identifier

FIELDS = {
    "schema_version",
    "profile_id",
    "adapter",
    "framework",
    "framework_version",
    "checkpoint_kind",
    "control_mode",
    "max_model_calls_per_advance",
    "profile_digest",
}
EMBEDDING_FIELD = "embedding_catalog_digest"


def validate_runtime_profile(value: dict) -> dict:
    require(
        set(value) == FIELDS or set(value) == FIELDS | {EMBEDDING_FIELD},
        "INVALID_BINDING",
        "Invalid Agent runtime profile",
        422,
    )
    require(
        value["schema_version"] == 1
        and valid_identifier(value["profile_id"])
        and value["adapter"] == "LANGGRAPH"
        and value["framework"] == "langgraph"
        and isinstance(value["framework_version"], str)
        and bool(value["framework_version"])
        and value["checkpoint_kind"] == "POSTGRES"
        and value["control_mode"] == "YIELD_TO_KERNEL"
        and value["max_model_calls_per_advance"] == 1,
        "INVALID_BINDING",
        "Unsupported Agent runtime profile",
        422,
    )
    unsigned = {
        key: item for key, item in value.items() if key not in {"profile_digest", EMBEDDING_FIELD}
    }
    require(
        value["profile_digest"] == canonical_digest(unsigned),
        "INVALID_BINDING",
        "Agent runtime profile digest mismatch",
        422,
    )
    if EMBEDDING_FIELD in value:
        require(
            isinstance(value[EMBEDDING_FIELD], str)
            and len(value[EMBEDDING_FIELD]) == 71
            and value[EMBEDDING_FIELD].startswith("sha256:"),
            "INVALID_BINDING",
            "Embedding catalog digest is invalid",
            422,
        )
    return value


def load_runtime_profile(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Agent runtime profile cannot be read") from error
    require(isinstance(value, dict), "INVALID_BINDING", "Invalid Agent runtime profile", 422)
    return validate_runtime_profile(value)
