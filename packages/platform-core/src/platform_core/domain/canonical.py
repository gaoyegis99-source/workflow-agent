"""Canonical JSON identities shared by pure Platform domain models."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_hex(value: Any) -> str:
    """Return the bare SHA-256 hex form used by existing idempotency fields."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


def digest_uri(value: Any) -> str:
    """Return the ``sha256:`` URI form used by immutable content identities."""

    return "sha256:" + digest_hex(value)
