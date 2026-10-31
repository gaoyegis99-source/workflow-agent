"""Pure digest helpers shared by active direct-deployment components."""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_digest(path: str | Path) -> str:
    """Return the stable SHA-256 URI for one regular artifact file."""

    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
