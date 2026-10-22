from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_workspace_dependencies.py"
SPEC = importlib.util.spec_from_file_location("check_workspace_dependencies", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_current_v7_source_has_no_sibling_workspace_dependency() -> None:
    assert MODULE.check(ROOT) == []


def test_runtime_configuration_cannot_reference_a_sibling_workspace(tmp_path: Path) -> None:
    config = tmp_path / "apps" / "runtime.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "source: /home/mtd/liweigis/workflow/groundwater-platform_v6/value\n",
        encoding="utf-8",
    )

    assert MODULE.check(tmp_path) == [
        "apps/runtime.yaml: WORKSPACE_SIBLING_DEPENDENCY:groundwater-platform_v6"
    ]


def test_historical_verification_references_are_not_runtime_dependencies(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "docs" / "verification" / "prior-run.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        '{"source": "/home/mtd/liweigis/workflow/groundwater-platform_v6"}',
        encoding="utf-8",
    )

    assert MODULE.check(tmp_path) == []


def test_source_symlink_cannot_escape_project_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    linked = tmp_path / "apps" / "linked.py"
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    assert MODULE.check(tmp_path) == ["apps/linked.py: WORKSPACE_SYMLINK_ESCAPE"]
