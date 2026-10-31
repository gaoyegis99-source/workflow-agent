#!/usr/bin/env python3
"""Validate the maintained V7 source and project-runtime layout contract.

This check is deliberately read-only. It distinguishes the source repository
from the project-owned runtime directory and keeps P8 at its project-local
direct-Compose location.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SOURCE_ROOT.parent
LAYOUT_PATH = PROJECT_ROOT / "project-layout.json"

SCRIPT_AREAS = frozenset(
    {
        "build",
        "checks",
        "configure",
        "evaluation",
        "lib",
        "maintenance",
        "operations",
        "release",
        "verification",
    }
)
DEPLOY_AREAS = frozenset(
    {"agent", "execution", "knowledge", "operators", "platform", "production", "spatial"}
)
TEST_AREAS = frozenset(
    {
        "agent",
        "architecture",
        "execution",
        "knowledge",
        "operators",
        "platform",
        "release",
        "spatial",
    }
)
CAPABILITY_AREAS = frozenset(
    {"agent", "authoring", "execution", "knowledge", "operators", "platform", "spatial"}
)
LEGACY_SOURCE_RUNTIMES = frozenset(
    {
        ".p1-runtime",
        ".p2-runtime",
        ".p3-runtime",
        ".p4-runtime",
        ".p5-runtime",
        ".p6-runtime",
        ".p7-runtime",
        ".p8-runtime",
    }
)
TEXT_SUFFIXES = frozenset({".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"})
LEGACY_REFERENCE_ALLOWLIST = frozenset(
    {
        Path(".gitignore"),
        Path("scripts/maintenance/cleanup_policy.py"),
        Path("scripts/maintenance/check_internal_layout.py"),
        Path("scripts/maintenance/check_project_hygiene.py"),
        Path("scripts/release/build_p8_release.sh"),
        Path("tests/architecture/test_project_hygiene.py"),
    }
)
RETIRED_MAINTENANCE_COMMANDS = frozenset(
    {
        Path("scripts/maintenance/cleanup_runtime.py"),
        Path("scripts/maintenance/relocate_reproducible_runtime.py"),
    }
)
LEGACY_REFERENCE_PATTERNS = {
    "FLAT_SCRIPT_PATH": re.compile(
        r"(?<![A-Za-z0-9_.-])scripts/"
        r"(?!(?:build|checks|configure|evaluation|lib|maintenance|operations|release|verification)"
        r"(?![A-Za-z0-9_.-])|__init__\.py)"
        r"[A-Za-z0-9_.-]+"
    ),
    "LEGACY_DEPLOY_PATH": re.compile(r"(?<![A-Za-z0-9_.-])deploy/p\d[A-Za-z0-9_.-]*(?:[/.])"),
    "LEGACY_TEST_PATH": re.compile(r"(?<![A-Za-z0-9_.-])tests/p\d[A-Za-z0-9_.-]*/"),
    "LEGACY_DOC_PATH": re.compile(r"(?<![A-Za-z0-9_.-])docs/p\d[A-Za-z0-9_.-]*\.md"),
    "LEGACY_SOURCE_RUNTIME_PATH": re.compile(r"(?<![A-Za-z0-9_.-])\.p[1-8]-runtime(?:/|\b)"),
}
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\n]+)\)")


def child_names(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {child.name for child in directory.iterdir() if child.is_dir()}


def require_directories(errors: list[str], parent: Path, names: frozenset[str]) -> None:
    missing = sorted(names - child_names(parent))
    if missing:
        relative = parent.relative_to(SOURCE_ROOT)
        errors.append(f"missing directories beneath {relative}: {', '.join(missing)}")


def validate_layout_contract(errors: list[str]) -> dict[str, object]:
    if not LAYOUT_PATH.is_file():
        errors.append(f"missing project layout contract: {LAYOUT_PATH}")
        return {}
    try:
        payload = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        errors.append(f"invalid project layout contract: {error}")
        return {}
    if payload.get("schema_version") != 2:
        errors.append("project layout schema_version must be 2")
    if payload.get("source") != "source":
        errors.append("project layout source must be source")
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("project layout runtime must be an object")
        return payload
    expected = {
        "root": "runtime",
        "work": "runtime/work",
        "active": "runtime/active",
        "archive": "runtime/archive",
        "toolchain": "runtime/toolchain",
        "docker": "runtime/docker",
        "p1": "runtime/work/p1",
        "p2": "runtime/work/p2",
        "p3": "runtime/work/p3",
        "p4": "runtime/work/p4",
        "p5": "runtime/work/p5",
        "p6": "runtime/work/p6",
        "p7": "runtime/work/p7",
        "p8": "runtime/p8",
    }
    for key, value in expected.items():
        if runtime.get(key) != value:
            errors.append(f"project layout runtime.{key} must be {value}")
    if "runtime_status" in payload:
        errors.append("project layout must not carry historical runtime_status observations")
    return payload


def validate_project_runtime(errors: list[str]) -> None:
    for relative in (
        "runtime/work",
        "runtime/active",
        "runtime/archive",
        "runtime/toolchain",
        "runtime/docker",
        "runtime/p8",
    ):
        if not (PROJECT_ROOT / relative).is_dir():
            errors.append(f"missing project runtime directory: {relative}")
    legacy = [PROJECT_ROOT / f"runtime/{stage}" for stage in ("p2", "p3", "p4", "p5", "p6", "p7")]
    for path in legacy:
        if path.exists():
            relative = path.relative_to(PROJECT_ROOT)
            errors.append(f"legacy runtime root must be archived or removed: {relative}")


def validate_source_tree(errors: list[str]) -> None:
    required_source = frozenset(
        {
            "apps",
            "packages",
            "runtimes",
            "deploy",
            "scripts",
            "tests",
            "docs",
        }
    )
    require_directories(errors, SOURCE_ROOT, required_source)
    require_directories(errors, SOURCE_ROOT / "scripts", SCRIPT_AREAS)
    require_directories(errors, SOURCE_ROOT / "deploy", DEPLOY_AREAS)
    require_directories(errors, SOURCE_ROOT / "tests", TEST_AREAS)
    require_directories(errors, SOURCE_ROOT / "docs" / "capabilities", CAPABILITY_AREAS)
    require_directories(
        errors,
        SOURCE_ROOT / "docs",
        frozenset({"maintenance", "operations"}),
    )

    for name in sorted(LEGACY_SOURCE_RUNTIMES):
        if (SOURCE_ROOT / name).exists():
            errors.append(f"legacy generated runtime remains in source: {name}")
    for path in SOURCE_ROOT.glob("scripts/*"):
        if path.is_file() and path.name != "__init__.py":
            errors.append(f"flat maintenance command remains at scripts root: {path.name}")
    for relative in sorted(RETIRED_MAINTENANCE_COMMANDS):
        path = SOURCE_ROOT / relative
        if path.exists() or path.is_symlink():
            errors.append(f"retired maintenance command remains: {relative}")


def validate_symlinks(errors: list[str]) -> None:
    skipped = {".git", ".venv", "node_modules", "__pycache__"}
    for path in SOURCE_ROOT.rglob("*"):
        if any(part in skipped for part in path.relative_to(SOURCE_ROOT).parts):
            continue
        if not path.is_symlink():
            continue
        try:
            path.resolve().relative_to(SOURCE_ROOT)
        except ValueError:
            errors.append(f"source symlink escapes source root: {path.relative_to(SOURCE_ROOT)}")


def _iter_active_text_files() -> list[Path]:
    skipped = {".git", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "node_modules"}
    files: list[Path] = []
    for directory, names, filenames in os.walk(SOURCE_ROOT, followlinks=False):
        current = Path(directory)
        names[:] = [name for name in names if name not in skipped]
        for filename in filenames:
            path = current / filename
            if path.suffix in TEXT_SUFFIXES:
                files.append(path)
    return sorted(files)


def _markdown_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    target = target.split("#", maxsplit=1)[0]
    if not target or target.startswith(("/", "http://", "https://", "mailto:")) or "${" in target:
        return None
    return target


def validate_active_references(errors: list[str]) -> None:
    """Reject stale paths and dead local Markdown links in maintained source."""
    for path in _iter_active_text_files():
        relative = path.relative_to(SOURCE_ROOT)
        if relative in LEGACY_REFERENCE_ALLOWLIST:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in LEGACY_REFERENCE_PATTERNS.items():
            match = pattern.search(content)
            if match:
                errors.append(f"{relative}: {label}:{match.group()}")
    markdown_paths = [SOURCE_ROOT / "README.md"]
    markdown_paths.extend((SOURCE_ROOT / "docs").rglob("*.md"))
    for path in sorted(set(markdown_paths)):
        if not path.is_file():
            continue
        relative = path.relative_to(SOURCE_ROOT)
        content = path.read_text(encoding="utf-8", errors="replace")
        for raw_target in MARKDOWN_LINK.findall(content):
            target = _markdown_target(raw_target)
            if target is None:
                continue
            if not (path.parent / target).resolve().exists():
                errors.append(f"{relative}: BROKEN_LOCAL_MARKDOWN_LINK:{target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("source", "runtime", "all"), default="all")
    scope = parser.parse_args().scope
    errors: list[str] = []
    validate_layout_contract(errors)
    if scope in {"runtime", "all"}:
        validate_project_runtime(errors)
    validate_source_tree(errors)
    validate_symlinks(errors)
    validate_active_references(errors)
    if errors:
        raise SystemExit("INTERNAL_LAYOUT_CHECK_FAILED\n- " + "\n- ".join(errors))
    print("INTERNAL_LAYOUT_CHECK_OK")


if __name__ == "__main__":
    main()
