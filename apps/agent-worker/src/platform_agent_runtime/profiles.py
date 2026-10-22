"""Exact allowlisted runtime profiles selected by the P6.0 ADR."""

from __future__ import annotations

import hashlib
import json
import re
from importlib.metadata import version
from pathlib import Path

from platform_contracts.agent_runtime import AgentRuntimeProfile

PLAIN_PROFILE_ID = "plain-loop-v1"
LANGGRAPH_PROFILE_ID = "langgraph-minimal-v1"
SELECTED_PROFILE_ID = LANGGRAPH_PROFILE_ID


def _digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _make(profile_id: str, *, adapter: str, checkpoint_kind: str) -> dict:
    framework = "none" if adapter == "PLAIN_LOOP" else "langgraph"
    framework_version = "builtin" if adapter == "PLAIN_LOOP" else version("langgraph")
    unsigned = {
        "schema_version": 1,
        "profile_id": profile_id,
        "adapter": adapter,
        "framework": framework,
        "framework_version": framework_version,
        "checkpoint_kind": checkpoint_kind,
        "control_mode": "YIELD_TO_KERNEL",
        "max_model_calls_per_advance": 1,
    }
    return unsigned | {"profile_digest": _digest(unsigned)}


_PROFILES = {
    PLAIN_PROFILE_ID: _make(
        PLAIN_PROFILE_ID,
        adapter="PLAIN_LOOP",
        checkpoint_kind="MEMORY",
    ),
    LANGGRAPH_PROFILE_ID: _make(
        LANGGRAPH_PROFILE_ID,
        adapter="LANGGRAPH",
        checkpoint_kind="POSTGRES",
    ),
}


def profile(profile_id: str = SELECTED_PROFILE_ID) -> dict:
    if profile_id not in _PROFILES:
        raise ValueError("Unknown agent runtime profile")
    return dict(_PROFILES[profile_id])


def validate(value: dict) -> dict:
    deployment_fields = {
        key: value.get(key)
        for key in (
            "tool_catalog_id",
            "tool_catalog_digest",
            "operator_catalog_digest",
            "embedding_catalog_digest",
            "deployment_profile_digest",
        )
        if key in value
    }
    base = {key: item for key, item in value.items() if key not in deployment_fields}
    parsed = AgentRuntimeProfile.model_validate(base).model_dump(mode="json")
    expected = _PROFILES.get(parsed["profile_id"])
    if expected is None or parsed != expected:
        raise ValueError("Agent runtime profile differs from its allowlisted definition")
    if deployment_fields:
        fields = set(deployment_fields)
        if fields == {"embedding_catalog_digest"}:
            if (
                re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    deployment_fields["embedding_catalog_digest"] or "",
                )
                is None
            ):
                raise ValueError("Agent embedding catalogue binding differs")
            return dict(expected) | deployment_fields
        legacy = {
            "tool_catalog_id",
            "tool_catalog_digest",
            "operator_catalog_digest",
            "deployment_profile_digest",
        }
        current = legacy | {"embedding_catalog_digest"}
        if fields != legacy and fields != current:
            raise ValueError("Agent deployment catalogue binding is incomplete")
        unsigned = dict(expected) | {
            key: deployment_fields[key]
            for key in (
                "tool_catalog_id",
                "tool_catalog_digest",
                "operator_catalog_digest",
            )
        }
        if "embedding_catalog_digest" in deployment_fields:
            if (
                re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    deployment_fields["embedding_catalog_digest"] or "",
                )
                is None
            ):
                raise ValueError("Agent embedding catalogue binding differs")
            unsigned["embedding_catalog_digest"] = deployment_fields["embedding_catalog_digest"]
        if deployment_fields["tool_catalog_id"] not in {
            "p7-action-tools-v2",
            "p7-science-tools-v2",
        } or deployment_fields["deployment_profile_digest"] != _digest(unsigned):
            raise ValueError("Agent deployment catalogue binding differs")
        return unsigned | {
            "deployment_profile_digest": deployment_fields["deployment_profile_digest"]
        }
    return dict(expected)


def diagnostic(value: dict) -> dict:
    checked = validate(value)
    return {
        "profile_id": checked["profile_id"],
        "profile_digest": checked["profile_digest"],
        "adapter": checked["adapter"],
        "framework": checked["framework"],
        "framework_version": checked["framework_version"],
        "checkpoint_kind": checked["checkpoint_kind"],
    }


def load_deployment_profile(path: str | Path) -> dict:
    """Load the exact allowlisted profile used by both API and Agent Worker."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Agent runtime profile cannot be read") from error
    if not isinstance(value, dict):
        raise ValueError("Agent runtime profile must be an object")
    return validate(value)
