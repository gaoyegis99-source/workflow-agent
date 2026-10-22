"""Pure draft authoring rules."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from platform_core.domain.control import require, valid_identifier


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def blank_workflow() -> dict:
    return {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {},
        "outputs": {},
        "steps": {},
    }


def validate_definition_create(request: dict) -> None:
    require(
        request.get("expected_revision") == 0,
        "REVISION_CONFLICT",
        "Expected revision must be zero",
        409,
    )
    require(
        valid_identifier(request.get("stable_key", "")),
        "MALFORMED_REQUEST",
        "Invalid stable workflow key",
        422,
    )
    _bounded_json(request.get("layout", {}), "layout")


def validate_revision_create(request: dict) -> None:
    require(
        isinstance(request.get("expected_revision"), int) and request["expected_revision"] > 0,
        "MALFORMED_REQUEST",
        "A positive expected revision is required",
        422,
    )
    _bounded_json(request.get("layout", {}), "layout")
    if request.get("source") is not None:
        # Imported lazily to keep the small digest/bounds module acyclic.
        from platform_core.domain.authoring_graph import validate_source_envelope

        validate_source_envelope(request["source"])


def _bounded_json(value: Any, name: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        require(False, "MALFORMED_REQUEST", f"{name} must be finite JSON", 422)
        return
    require(len(encoded.encode()) <= 1024 * 1024, "MALFORMED_REQUEST", f"{name} is too large", 422)
