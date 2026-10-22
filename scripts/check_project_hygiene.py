#!/usr/bin/env python3
"""Verify that V7's Git baseline contains only source and reviewable evidence."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_IGNORE_RULES = {
    "**/.venv/",
    ".p8-runtime/",
    ".generated/",
    "node_modules/",
    "test-results/",
    "playwright-report/",
}
FORBIDDEN_PATH_PARTS = {
    ".generated",
    ".p8-runtime",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "playwright-report",
    "test-results",
}
FORBIDDEN_FILENAMES = {".env"}
REQUIRED_ACCEPTANCE_SOURCES = {
    "experiments/p1/run.py",
    "runtimes/cwl/pyproject.toml",
    "scripts/check_p1.sh",
    "scripts/check_p4.sh",
    "scripts/check_p5.sh",
    "scripts/check_p7.sh",
}


def _tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def check_paths(paths: list[str]) -> list[str]:
    """Return deterministic Git-tracking violations for supplied relative paths."""
    violations: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        if path.name in FORBIDDEN_FILENAMES or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            violations.append(f"{raw_path}: TRACKED_SECRET_CONFIGURATION")
            continue
        blocked = next((part for part in path.parts if part in FORBIDDEN_PATH_PARTS), None)
        if blocked is not None:
            violations.append(f"{raw_path}: TRACKED_EPHEMERAL_PATH:{blocked}")
    return sorted(violations)


def check(root: Path = ROOT, tracked_paths: list[str] | None = None) -> list[str]:
    """Validate required ignore rules and the repository's current index."""
    ignore_path = root / ".gitignore"
    rules = set(ignore_path.read_text(encoding="utf-8").splitlines())
    violations = [
        f".gitignore: REQUIRED_IGNORE_RULE_MISSING:{rule}"
        for rule in sorted(REQUIRED_IGNORE_RULES - rules)
    ]
    violations.extend(
        f"{relative}: REQUIRED_ACCEPTANCE_SOURCE_MISSING"
        for relative in sorted(REQUIRED_ACCEPTANCE_SOURCES)
        if not (root / relative).is_file() or (root / relative).is_symlink()
    )
    paths = _tracked_paths(root) if tracked_paths is None else tracked_paths
    return sorted([*violations, *check_paths(paths)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    violations = check(args.root)
    if violations:
        print("PROJECT_HYGIENE_CHECK_FAILED")
        print("\n".join(violations))
        raise SystemExit(1)
    print("PROJECT_HYGIENE_CHECK_OK")


if __name__ == "__main__":
    main()
