"""Standard-library verification used by privileged deployment evidence import."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_digest(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_pinned_release(path: str | Path, expected_digest: str) -> dict:
    if not isinstance(expected_digest, str) or not expected_digest.startswith("sha256:"):
        raise ValueError("A pinned release digest is required")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Release lock cannot be read") from error
    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2, 3}:
        raise ValueError("Unsupported release lock")
    supplied = value.get("release_digest")
    actual = _digest({key: item for key, item in value.items() if key != "release_digest"})
    if supplied != actual or supplied != expected_digest:
        raise ValueError("Release lock digest differs from the pinned release")
    return value


def verify_operator_catalog(lock: dict, path: str | Path, catalog: dict) -> None:
    record = lock.get("operator_catalog")
    if not isinstance(record, dict):
        raise ValueError("Release lock has no operator catalog")
    if record.get("digest") != catalog.get("catalog_digest"):
        raise ValueError("Operator catalog digest differs from the release lock")
    if record.get("file_sha256") != _file_digest(path):
        raise ValueError("Operator catalog file differs from the release lock")
