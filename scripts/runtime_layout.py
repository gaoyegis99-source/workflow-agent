"""Resolve relocatable local runtime roots without coupling releases to a workspace."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def runtime_root(variable: str, legacy_name: str) -> Path:
    configured = os.environ.get(variable)
    if not configured:
        return ROOT / legacy_name
    path = Path(configured).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


P8_RUNTIME_ROOT = runtime_root("V7_P8_RUNTIME_ROOT", ".p8-runtime")
P3_RUNTIME_ROOT = runtime_root("V7_P3_RUNTIME_ROOT", ".p3-runtime")
