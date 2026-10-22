"""Strict, independently versioned configuration for optional embedding services."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from platform_core.domain.agent_runtime import canonical_digest

SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
BASE_FIELDS = {"schema_version", "catalog_id", "state", "catalog_digest"}
ENABLED_FIELDS = {
    "origin",
    "model_id",
    "dimensions",
    "quantization",
    "artifact_digest",
    "credential_file",
    "ca_file",
    "request_timeout_seconds",
    "p95_limit_seconds",
}


def validate_embedding_catalog(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Embedding catalog must be an object")
    state = value.get("state")
    allowed = BASE_FIELDS | ({"reason"} if state == "DISABLED" else ENABLED_FIELDS)
    if set(value) != allowed:
        raise ValueError("Embedding catalog fields differ")
    if (
        value.get("schema_version") != 1
        or value.get("catalog_id") != "p8-embedding-v1"
        or state not in {"DISABLED", "ENABLED"}
        or SHA256.fullmatch(str(value.get("catalog_digest", ""))) is None
    ):
        raise ValueError("Embedding catalog identity is invalid")
    unsigned = {key: item for key, item in value.items() if key != "catalog_digest"}
    if value["catalog_digest"] != canonical_digest(unsigned):
        raise ValueError("Embedding catalog digest differs")
    if state == "DISABLED":
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("Disabled embedding catalog requires a reason")
        return dict(value)

    parsed = urlsplit(str(value.get("origin", "")))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enabled embedding origin must be a credential-free HTTPS origin")
    if not (
        isinstance(value.get("model_id"), str)
        and value["model_id"]
        and isinstance(value.get("dimensions"), int)
        and 1 <= value["dimensions"] <= 65_536
        and isinstance(value.get("quantization"), str)
        and value["quantization"]
        and SHA256.fullmatch(str(value.get("artifact_digest", ""))) is not None
        and all(
            isinstance(value.get(field), str) and Path(value[field]).is_absolute()
            for field in ("credential_file", "ca_file")
        )
        and value.get("request_timeout_seconds") == 15
        and value.get("p95_limit_seconds") == 10
    ):
        raise ValueError("Enabled embedding catalog violates the production gate")
    return dict(value)


def load_embedding_catalog(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("Embedding catalog cannot be read") from error
    return validate_embedding_catalog(value)
