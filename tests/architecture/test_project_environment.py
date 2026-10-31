from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.maintenance import project_environment as environment

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "scripts/maintenance/bootstrap-assets"


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    content = (ASSETS / "project-layout.json").read_text().replace("__UID__", str(os.getuid()))
    (tmp_path / "project-layout.json").write_text(content)
    monkeypatch.setattr(environment, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(environment, "SOURCE_ROOT", source)
    for key in tuple(os.environ):
        if key.startswith(("V6_", "V7_")) or key in {
            "GROUNDWATER_GENERATED_ROOT",
            "UV_PROJECT_ENVIRONMENT",
        }:
            monkeypatch.delenv(key)
    return tmp_path


def test_owned_paths_reject_sibling_traversal_and_symlink(project):
    allowed = project / "runtime/work"
    with pytest.raises(environment.EnvironmentRefusal):
        environment.owned_path(project.parent / "sibling/runtime/work", allowed)
    with pytest.raises(environment.EnvironmentRefusal):
        environment.owned_path(allowed / "../active", allowed)
    allowed.mkdir(parents=True)
    (allowed / "linked").symlink_to(project.parent)
    with pytest.raises(environment.EnvironmentRefusal):
        environment.owned_path(allowed / "linked/cache", allowed)


def test_testing_environment_scrubs_credentials_after_saving_valid_paths(project):
    prefix = "V6" if environment.PROJECT.endswith("v6") else "V7"
    key = "GROUNDWATER_GENERATED_ROOT" if prefix == "V6" else "V7_P4_RUNTIME_ROOT"
    result = environment.child_environment(
        testing=True,
        inherited={
            "PATH": os.defpath,
            key: str(project / "runtime/work/fixture"),
            f"{prefix}_OWNER_TOKEN": "dummy-never-log",
            f"{prefix}_DATABASE_URL": "dummy",
            "DOCKER_HOST": "unix:///foreign.sock",
            "DOCKER_CONTEXT": "foreign",
            "DOCKER_CERT_PATH": "/foreign",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_TLS": "1",
            "PYTHONPATH": "/foreign/source",
            "PYTHONHOME": "/foreign/python",
            "VIRTUAL_ENV": "/foreign/venv",
            "UV_PROJECT": "/foreign/source",
            "UV_ACTIVE": "1",
        },
    )
    assert result[key] == str(project / "runtime/work/fixture")
    assert f"{prefix}_OWNER_TOKEN" not in result
    assert f"{prefix}_DATABASE_URL" not in result
    assert (
        result["DOCKER_HOST"]
        == f"unix:///run/user/{os.getuid()}/{environment.PROJECT}-docker/docker.sock"
    )
    assert not {"DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"}.intersection(result)
    assert result["PYTHONDONTWRITEBYTECODE"] == "1"
    assert result["PYTHONPATH"] == str(project / "source")
    assert result["VIRTUAL_ENV"] == result["UV_PROJECT_ENVIRONMENT"]
    assert not {"DOCKER_TLS", "PYTHONHOME", "UV_PROJECT", "UV_ACTIVE"}.intersection(result)


@pytest.mark.parametrize(
    "arguments", [["--context", "foreign"], ["--host=unix:///foreign"], ["-Hforeign"]]
)
def test_docker_cli_overrides_are_rejected(arguments):
    with pytest.raises(environment.EnvironmentRefusal, match="OVERRIDE_REJECTED"):
        environment.docker_arguments(arguments)


def test_foreign_socket_in_layout_is_rejected(project):
    file = project / "project-layout.json"
    value = json.loads(file.read_text())
    value["runtime"]["docker_socket"] = "/run/user/1000/other-docker/docker.sock"
    file.write_text(json.dumps(value))
    with pytest.raises(environment.EnvironmentRefusal, match="OUTSIDE_PROJECT"):
        environment.socket_path()


def test_docker_context_short_flag_is_rejected():
    for arguments in (["-c", "foreign"], ["-cforeign"], ["-c=foreign"]):
        with pytest.raises(environment.EnvironmentRefusal, match="OVERRIDE_REJECTED"):
            environment.docker_arguments(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--rm", "image", "/bin/sh", "-c", "true"],
        ["run", "--cpus", "1", "image"],
        ["run", "--cap-drop", "ALL", "image"],
    ],
)
def test_docker_context_parser_allows_subcommand_and_container_arguments(arguments):
    assert environment.docker_arguments(arguments) == ["/usr/bin/docker", *arguments]


def test_bootstrap_uses_only_tracked_assets_and_refuses_overwrite(tmp_path):
    maintenance = tmp_path / "source" / "scripts" / "maintenance"
    maintenance.mkdir(parents=True)
    for name in ("bootstrap_project.py", "project_environment.py"):
        shutil.copy2(ROOT / "scripts" / "maintenance" / name, maintenance / name)
    shutil.copytree(ASSETS, maintenance / "bootstrap-assets")
    script = maintenance / "bootstrap_project.py"

    def run(*args):
        return subprocess.run(
            [sys.executable, "-B", str(script), *args], capture_output=True, text=True
        )

    assert json.loads(run().stdout)["status"] == "PLANNED"
    assert not (tmp_path / "bin").exists()
    assert run("--execute").returncode == 0
    assert run("--execute").returncode == 0
    assert run("--check").returncode == 0
    assert not (tmp_path / "runtime").exists()
    layout = json.loads((tmp_path / "project-layout.json").read_text())
    assert "worktrees" not in layout
    assert "observed_active_release" not in layout.get("runtime_status", {})
    launcher = tmp_path / "bin/ops"
    launcher.write_text("custom entrypoint")
    assert run("--execute").returncode == 1
    assert launcher.read_text() == "custom entrypoint"


def test_direct_docker_launcher_rejects_target_override_without_socket(tmp_path):
    maintenance = tmp_path / "source" / "scripts" / "maintenance"
    maintenance.mkdir(parents=True)
    shutil.copy2(
        ROOT / "scripts/maintenance/project_environment.py", maintenance / "project_environment.py"
    )
    launcher = tmp_path / "bin/docker"
    launcher.parent.mkdir()
    shutil.copy2(ASSETS / "bin/docker", launcher)
    result = subprocess.run(
        ["bash", str(launcher), "--host", "unix:///foreign"], capture_output=True, text=True
    )
    assert result.returncode == 75
    assert "OVERRIDE_REJECTED" in result.stderr


SCRUBBED_SCRIPTS = [
    "check_p3.sh",
    "check_p4_scheduler_r5.sh",
    "check_p4_scheduler_r42.sh",
    "check_p4.sh",
    "check_p4_scheduler_r41.sh",
    "check_p4_scheduler_r3.sh",
    "check_p4_performance.sh",
    "check_p5.sh",
    "check_p4_scheduler_r44.sh",
    "check_p42.sh",
    "check_p422.sh",
    "check_p4_scheduler_r44_roles.sh",
    "check_p4_scheduler_r43.sh",
    "check_p4_scheduler_diagnostics.sh",
]


@pytest.mark.parametrize("name", SCRUBBED_SCRIPTS)
def test_all_test_shell_initializers_keep_paths_but_remove_inherited_credentials(tmp_path, name):
    source = tmp_path / "source"
    for relative in ("scripts/lib/runtime-layout.sh", "scripts/maintenance/project_environment.py"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (tmp_path / "project-layout.json").write_text(
        (ASSETS / "project-layout.json").read_text().replace("__UID__", str(os.getuid()))
    )
    original = (ROOT / "scripts/checks" / name).read_text()
    assert 'if [[ "$name" == V7_*' not in original
    lines = original.splitlines()
    stop = next(
        index
        for index, line in enumerate(lines)
        if 'source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"' in line
    )
    assert "--testing" in lines[stop]
    script = source / "scripts/checks" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "\n".join(lines[: stop + 1])
        + '\n: "${V7_P4_RUNTIME_ROOT:?}"\n'
        + 'test -z "${V7_OWNER_TOKEN:-}"\n'
        + 'test -z "${V7_DATABASE_URL:-}"\n'
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("V6_", "V7_"))
        and key not in {"UV_PROJECT_ENVIRONMENT", "GROUNDWATER_GENERATED_ROOT"}
    }
    env.update(V7_OWNER_TOKEN="fixture-secret", V7_DATABASE_URL="fixture-only")
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_p2_rejects_unsupported_phase_before_any_execution():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/checks/check_p2.sh"), "--phase", "identity"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


def test_runtime_layout_does_not_consume_the_calling_scripts_arguments():
    command = (
        "set -- --phase upgrade; source scripts/lib/runtime-layout.sh; "
        'test "$1" = --phase; test "$2" = upgrade; test -n "$V7_P8_RUNTIME_ROOT"'
    )
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_public_runtime_override_rejects_a_sibling_project(project, monkeypatch):
    if environment.PROJECT.endswith("v7"):
        from scripts.lib.runtime_layout import runtime_root

        monkeypatch.setenv("V7_P3_RUNTIME_ROOT", str(project.parent / "sibling/runtime/work"))
        with pytest.raises(environment.EnvironmentRefusal, match="OUTSIDE"):
            runtime_root("V7_P3_RUNTIME_ROOT", "p3", "runtime/work/p3")
    else:
        monkeypatch.setenv(
            "GROUNDWATER_GENERATED_ROOT", str(project.parent / "sibling/runtime/work")
        )
        with pytest.raises(environment.EnvironmentRefusal, match="OUTSIDE"):
            environment.paths()


def test_layout_source_check_does_not_require_live_runtime():
    from scripts.maintenance.check_runner import load_registry

    registry = load_registry(ROOT / "docs/maintenance/maintenance-gates.v2.json")
    fast = [registry["checks"][name] for name in registry["levels"]["fast"]]
    layout_checks = [
        item for item in fast if "check_internal_layout.py" in " ".join(item["command"])
    ]
    assert len(layout_checks) == 1
    assert layout_checks[0]["command"][-2:] == ["--scope", "source"]
    assert "layout-runtime" in registry["levels"]["release"]
