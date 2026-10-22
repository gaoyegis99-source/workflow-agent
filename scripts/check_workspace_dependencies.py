#!/usr/bin/env python3
"""Reject runtime/configuration dependencies on sibling workspace projects."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PATH = re.compile(r"/home/mtd/liweigis/workflow/([A-Za-z0-9][A-Za-z0-9_.-]*)")
IGNORED_DIRECTORIES = {
    ".git",
    ".generated",
    ".p8-runtime",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "docs",
    "node_modules",
    "tests",
}
HISTORICAL_PREFIXES = (Path("docs/verification"),)
SCANNED_SUFFIXES = {".json", ".py", ".sh", ".toml", ".yaml", ".yml"}
SCANNED_NAMES = {"Dockerfile", "compose.yaml", "compose.yml"}


def _is_historical(relative: Path) -> bool:
    return any(relative.is_relative_to(prefix) for prefix in HISTORICAL_PREFIXES)


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def check(root: Path = ROOT) -> list[str]:
    """Return deterministic violations without following excluded directories."""
    root = root.resolve()
    violations: list[str] = []
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        relative_directory = current.relative_to(root)
        retained_names: list[str] = []
        for name in names:
            candidate = current / name
            relative = candidate.relative_to(root)
            if name in IGNORED_DIRECTORIES or _is_historical(relative):
                continue
            if candidate.is_symlink():
                if not _is_within(root, candidate):
                    violations.append(f"{relative.as_posix()}: WORKSPACE_SYMLINK_ESCAPE")
                continue
            retained_names.append(name)
        names[:] = retained_names

        if _is_historical(relative_directory):
            names[:] = []
            continue
        for name in files:
            path = current / name
            relative = path.relative_to(root)
            if path.is_symlink():
                if not _is_within(root, path):
                    violations.append(f"{relative.as_posix()}: WORKSPACE_SYMLINK_ESCAPE")
                continue
            if path.suffix not in SCANNED_SUFFIXES and path.name not in SCANNED_NAMES:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            for sibling in sorted(set(WORKSPACE_PATH.findall(content))):
                if sibling != root.name:
                    violations.append(
                        f"{relative.as_posix()}: WORKSPACE_SIBLING_DEPENDENCY:{sibling}"
                    )
    return sorted(violations)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    violations = check(args.root)
    if violations:
        print("WORKSPACE_DEPENDENCY_CHECK_FAILED")
        print("\n".join(violations))
        raise SystemExit(1)
    print("WORKSPACE_DEPENDENCY_CHECK_OK")


if __name__ == "__main__":
    main()
