#!/usr/bin/env python3
"""Reject runtime/configuration dependencies on sibling workspace projects."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_PATH = re.compile(
    r"/home/mtd/liweigis/workflow/((?:groundwater-v[67])|(?:(?:projects/(?:platform/groundwater/(?:worktrees/)?|research/)|archive/(?:platform/groundwater/|services/data-asset/|workflows/))[A-Za-z0-9][A-Za-z0-9_.-]*))"
)
HOST_DOCKER_SOCKET_SOURCE = re.compile(
    r"(?m)^\s*-\s*/var/run/docker\.sock:|"
    r"DOCKER_HOST\s*=\s*[\"']unix:///var/run/docker\.sock"
)
CROSS_PROJECT_IMPORT = re.compile(
    r"(?m)^\s*(?:from|import)\s+(?:groundwater_v6|groundwater_platform_v6)(?:\.|\s|$)"
)
IGNORED_DIRECTORIES = {
    ".git",
    ".generated",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "docs",
    "node_modules",
    "tests",
}
SCANNED_SUFFIXES = {".json", ".py", ".sh", ".toml", ".yaml", ".yml"}
SCANNED_NAMES = {"Dockerfile", "compose.yaml", "compose.yml"}


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _registered_project(root: Path) -> str | None:
    for candidate in (root, *root.parents):
        if (candidate / "project-layout.json").is_file():
            for workspace in candidate.parents:
                if (workspace / "workspace-layout.json").is_file():
                    return candidate.relative_to(workspace).as_posix()
        if (candidate / "workspace-layout.json").is_file():
            return root.relative_to(candidate).as_posix()
    return None


def _layout_violations(root: Path) -> list[str]:
    """Check the project-level half of the runtime-isolation contract.

    Source scans intentionally omit the project root.  The layout is where a
    hidden shared Docker socket or shared writable runtime would otherwise be
    declared, so validate it separately.  Temporary unit-test trees that are
    not a ``source/`` checkout stay useful for focused content tests.
    """

    if root.name != "source":
        return []
    project_root = root.parent
    path = project_root / "project-layout.json"
    if path.is_symlink() or not path.is_file():
        return ["../project-layout.json: PROJECT_LAYOUT_MISSING_OR_SYMLINK"]
    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["../project-layout.json: PROJECT_LAYOUT_INVALID"]
    runtime = layout.get("runtime") if isinstance(layout, dict) else None
    if (
        layout.get("project") != "groundwater-v7"
        or layout.get("source") != "source"
        or not isinstance(runtime, dict)
    ):
        return ["../project-layout.json: PROJECT_LAYOUT_INVALID"]
    expected_socket = f"/run/user/{os.getuid()}/groundwater-v7-docker/docker.sock"
    expected_paths = {
        "root": "runtime",
        "work": "runtime/work",
        "active": "runtime/active",
        "archive": "runtime/archive",
        "docker": "runtime/docker",
        "p8": "runtime/p8",
    }
    violations: list[str] = []
    for key, value in expected_paths.items():
        if runtime.get(key) != value:
            violations.append(f"../project-layout.json: RUNTIME_PATH_INVALID:{key}")
    if runtime.get("docker_socket") != expected_socket:
        violations.append("../project-layout.json: DOCKER_SOCKET_OUTSIDE_PROJECT")
    for key in ("shared_runtime_paths", "shared_docker_resources"):
        if layout.get(key) != []:
            violations.append(f"../project-layout.json: SHARED_RESOURCE_DECLARED:{key}")
    return violations


def _project_bin_violations(root: Path) -> list[str]:
    """Keep stable project-level launchers inside the same boundary contract."""
    if root.name != "source":
        return []
    bin_root = root.parent / "bin"
    if bin_root.is_symlink() or not bin_root.is_dir():
        return ["../bin: PROJECT_BIN_MISSING_OR_SYMLINK"]
    violations: list[str] = []
    current_project = _registered_project(root)
    for directory, names, files in os.walk(bin_root, followlinks=False):
        current = Path(directory)
        retained_names: list[str] = []
        for name in names:
            path = current / name
            relative = f"../bin/{path.relative_to(bin_root).as_posix()}"
            if path.is_symlink():
                violations.append(f"{relative}: WORKSPACE_SYMLINK_ESCAPE")
                continue
            retained_names.append(name)
        names[:] = retained_names
        for name in files:
            path = current / name
            relative = f"../bin/{path.relative_to(bin_root).as_posix()}"
            if path.is_symlink() or not path.is_file():
                violations.append(f"{relative}: WORKSPACE_SYMLINK_ESCAPE")
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if HOST_DOCKER_SOCKET_SOURCE.search(content):
                violations.append(f"{relative}: HOST_DOCKER_SOCKET_SOURCE")
            if CROSS_PROJECT_IMPORT.search(content):
                violations.append(f"{relative}: CROSS_PROJECT_IMPORT:groundwater-v6")
            for sibling in sorted(set(WORKSPACE_PATH.findall(content))):
                if sibling != current_project:
                    violations.append(f"{relative}: WORKSPACE_SIBLING_DEPENDENCY:{sibling}")
    return violations


def check(root: Path = ROOT) -> list[str]:
    """Return deterministic violations without following excluded directories."""
    root = root.resolve()
    current_project = _registered_project(root)
    violations: list[str] = []
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        retained_names: list[str] = []
        for name in names:
            candidate = current / name
            relative = candidate.relative_to(root)
            if name in IGNORED_DIRECTORIES:
                continue
            if candidate.is_symlink():
                if not _is_within(root, candidate):
                    violations.append(f"{relative.as_posix()}: WORKSPACE_SYMLINK_ESCAPE")
                continue
            retained_names.append(name)
        names[:] = retained_names

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
            if HOST_DOCKER_SOCKET_SOURCE.search(content):
                violations.append(f"{relative.as_posix()}: HOST_DOCKER_SOCKET_SOURCE")
            if path.suffix == ".py" and CROSS_PROJECT_IMPORT.search(content):
                violations.append(f"{relative.as_posix()}: CROSS_PROJECT_IMPORT:groundwater-v6")
            for sibling in sorted(set(WORKSPACE_PATH.findall(content))):
                if sibling != current_project:
                    violations.append(
                        f"{relative.as_posix()}: WORKSPACE_SIBLING_DEPENDENCY:{sibling}"
                    )
    return sorted([*violations, *_layout_violations(root), *_project_bin_violations(root)])


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
