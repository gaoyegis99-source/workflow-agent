"""Opaque, scope-bound cursor helpers shared by trusted HTTP services."""

from __future__ import annotations

import base64
import binascii
import json
import re


def encode_cursor(scope: str, keys: tuple[str, ...]) -> str:
    if (
        not scope
        or not keys
        or any(not isinstance(value, str) or not value or len(value) > 128 for value in keys)
    ):
        raise ValueError("Invalid cursor components")
    payload = json.dumps(
        [1, scope, list(keys)],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, scope: str, key_count: int) -> tuple[str, ...] | None:
    if cursor is None:
        return None
    try:
        if re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(payload)
        if (
            not isinstance(value, list)
            or len(value) != 3
            or value[0] != 1
            or value[1] != scope
            or not isinstance(value[2], list)
            or len(value[2]) != key_count
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in value[2])
        ):
            raise ValueError
        return tuple(value[2])
    except (UnicodeError, ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise ValueError("Invalid list cursor") from None
