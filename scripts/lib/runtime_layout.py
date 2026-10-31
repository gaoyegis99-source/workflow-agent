"""Resolve project-owned runtime roots from the V7 layout contract."""

from __future__ import annotations

from pathlib import Path

from scripts.maintenance.project_environment import layout, paths

ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = ROOT.parent
LAYOUT_PATH = PROJECT_ROOT / "project-layout.json"


def _layout_payload() -> dict[str, object]:
    return layout()


def runtime_root(variable: str, contract_key: str, fallback: str) -> Path:
    """Use the same checked paths as every Shell maintenance entrypoint."""
    resolved = paths()
    if variable not in resolved:
        raise ValueError("RUNTIME_VARIABLE_UNKNOWN")
    return Path(resolved[variable])


RUNTIME_ROOT = runtime_root("V7_RUNTIME_ROOT", "root", "runtime")
WORK_ROOT = runtime_root("V7_RUNTIME_WORK_ROOT", "work", "runtime/work")
ACTIVE_ROOT = runtime_root("V7_RUNTIME_ACTIVE_ROOT", "active", "runtime/active")
ARCHIVE_ROOT = runtime_root("V7_RUNTIME_ARCHIVE_ROOT", "archive", "runtime/archive")
TOOLCHAIN_ROOT = runtime_root("V7_RUNTIME_TOOLCHAIN_ROOT", "toolchain", "runtime/toolchain")
DOCKER_ROOT = runtime_root("V7_DOCKER_DATA_ROOT", "docker", "runtime/docker")

P1_RUNTIME_ROOT = runtime_root("V7_P1_RUNTIME_ROOT", "p1", "runtime/work/p1")
P2_RUNTIME_ROOT = runtime_root("V7_P2_RUNTIME_ROOT", "p2", "runtime/work/p2")
P3_RUNTIME_ROOT = runtime_root("V7_P3_RUNTIME_ROOT", "p3", "runtime/work/p3")
P4_RUNTIME_ROOT = runtime_root("V7_P4_RUNTIME_ROOT", "p4", "runtime/work/p4")
P5_RUNTIME_ROOT = runtime_root("V7_P5_RUNTIME_ROOT", "p5", "runtime/work/p5")
P6_RUNTIME_ROOT = runtime_root("V7_P6_RUNTIME_ROOT", "p6", "runtime/work/p6")
P7_RUNTIME_ROOT = runtime_root("V7_P7_RUNTIME_ROOT", "p7", "runtime/work/p7")
# P8 stays at its existing project-local path.  It is a project-owned direct
# Compose runtime; callers may not redirect it to another project.
P8_RUNTIME_ROOT = runtime_root("V7_P8_RUNTIME_ROOT", "p8", "runtime/p8")
