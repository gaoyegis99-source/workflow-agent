"""Data invariants. This module deliberately knows no storage or HTTP libraries."""

import hashlib
import re

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 128


def identifier(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
    ):
        raise ValueError("Invalid identity")
    return value


def checksum(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()
