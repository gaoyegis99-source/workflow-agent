#!/usr/bin/env python3
"""Verify that V7's Git baseline contains only current maintained source."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_IGNORE_RULES = {
    "/apps/platform-api/src/platform_api/studio_static/",
    "/archive/",
    "/docs/verification/",
    "/evidence/",
    "**/.venv/",
    ".generated/",
    "node_modules/",
    "test-results/",
    "playwright-report/",
}
REQUIRED_RETRIEVAL_IGNORE_RULES = {
    "apps/platform-api/src/platform_api/studio_static/",
    "archive/",
    "docs/verification/",
    "evidence/",
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
FORBIDDEN_TRACKED_PREFIXES = {
    PurePosixPath("apps/platform-api/src/platform_api/studio_static"),
    PurePosixPath("archive"),
    PurePosixPath("docs/verification"),
    PurePosixPath("evidence"),
}
REQUIRED_ACCEPTANCE_SOURCES = {
    "experiments/p1/run.py",
    "runtimes/cwl/pyproject.toml",
    "scripts/checks/check_p1.sh",
    "scripts/checks/check_p4.sh",
    "scripts/checks/check_p5.sh",
    "scripts/checks/check_p7.sh",
}
FORBIDDEN_SOURCE_RUNTIME_DIRECTORIES = {
    ".p2-runtime",
    ".p3-runtime",
    ".p4-runtime",
    ".p5-runtime",
    ".p6-runtime",
    ".p7-runtime",
    ".p8-runtime",
}


def _tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    paths = [path for path in result.stdout.decode("utf-8").split("\0") if path]
    return [path for path in paths if (root / path).exists() or (root / path).is_symlink()]


def check_paths(paths: list[str]) -> list[str]:
    """Return deterministic Git-tracking violations for supplied relative paths."""
    violations: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        blocked_prefix = next(
            (
                prefix
                for prefix in FORBIDDEN_TRACKED_PREFIXES
                if path == prefix or path.is_relative_to(prefix)
            ),
            None,
        )
        if blocked_prefix is not None:
            violations.append(
                f"{raw_path}: TRACKED_RETRIEVAL_POLLUTION:{blocked_prefix.as_posix()}"
            )
            continue
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
    retrieval_ignore_path = root / ".ignore"
    retrieval_rules = (
        set(retrieval_ignore_path.read_text(encoding="utf-8").splitlines())
        if retrieval_ignore_path.is_file()
        else set()
    )
    violations.extend(
        f".ignore: REQUIRED_RETRIEVAL_IGNORE_RULE_MISSING:{rule}"
        for rule in sorted(REQUIRED_RETRIEVAL_IGNORE_RULES - retrieval_rules)
    )
    violations.extend(
        f"{relative}: REQUIRED_ACCEPTANCE_SOURCE_MISSING"
        for relative in sorted(REQUIRED_ACCEPTANCE_SOURCES)
        if not (root / relative).is_file() or (root / relative).is_symlink()
    )
    violations.extend(
        f"{relative}: SOURCE_RUNTIME_DIRECTORY_PRESENT"
        for relative in sorted(FORBIDDEN_SOURCE_RUNTIME_DIRECTORIES)
        if (root / relative).exists()
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
