from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "maintenance" / "check_workspace_dependencies.py"
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
        "source: /home/mtd/liweigis/workflow/groundwater-v6/source/value\n",
        encoding="utf-8",
    )

    assert MODULE.check(tmp_path) == [
        "apps/runtime.yaml: WORKSPACE_SIBLING_DEPENDENCY:groundwater-v6"
    ]


def test_historical_verification_references_are_not_runtime_dependencies(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "docs" / "verification" / "prior-run.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        '{"source": "/home/mtd/liweigis/workflow/groundwater-v6/source"}',
        encoding="utf-8",
    )

    assert MODULE.check(tmp_path) == []


def test_active_configuration_cannot_mount_the_host_docker_socket(tmp_path: Path) -> None:
    compose = tmp_path / "deploy" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text(
        "services:\n  worker:\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n",
        encoding="utf-8",
    )

    assert MODULE.check(tmp_path) == ["deploy/compose.yaml: HOST_DOCKER_SOCKET_SOURCE"]


def test_active_python_cannot_import_v6_by_project_name(tmp_path: Path) -> None:
    source = tmp_path / "apps" / "bad.py"
    source.parent.mkdir(parents=True)
    source.write_text("from groundwater_v6.client import Service\n", encoding="utf-8")
    assert MODULE.check(tmp_path) == ["apps/bad.py: CROSS_PROJECT_IMPORT:groundwater-v6"]


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


def test_project_layout_cannot_declare_shared_or_host_runtime(tmp_path: Path) -> None:
    project = tmp_path / "groundwater-v7"
    source = project / "source"
    source.mkdir(parents=True)
    (project / "bin").mkdir()
    (project / "project-layout.json").write_text(
        json.dumps(
            {
                "project": "groundwater-v7",
                "source": "source",
                "runtime": {
                    "root": "runtime",
                    "work": "runtime/work",
                    "active": "runtime/active",
                    "archive": "runtime/archive",
                    "docker": "runtime/docker",
                    "p8": "runtime/p8",
                    "docker_socket": "/var/run/docker.sock",
                },
                "shared_runtime_paths": ["../groundwater-v6/runtime"],
                "shared_docker_resources": [],
            }
        ),
        encoding="utf-8",
    )

    assert MODULE.check(source) == [
        "../project-layout.json: DOCKER_SOCKET_OUTSIDE_PROJECT",
        "../project-layout.json: SHARED_RESOURCE_DECLARED:shared_runtime_paths",
    ]


def test_project_bin_cannot_reference_v6_runtime(tmp_path: Path) -> None:
    source = tmp_path / "groundwater-v7" / "source"
    source.mkdir(parents=True)
    command = source.parent / "bin" / "bad"
    command.parent.mkdir()
    command.write_text(
        "state=/home/mtd/liweigis/workflow/groundwater-v6/runtime/state\n",
        encoding="utf-8",
    )
    assert MODULE._project_bin_violations(source) == [
        "../bin/bad: WORKSPACE_SIBLING_DEPENDENCY:groundwater-v6"
    ]
