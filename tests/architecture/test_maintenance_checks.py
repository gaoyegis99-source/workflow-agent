from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.maintenance import check_runner as runner

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs/maintenance/maintenance-gates.v2.json"


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["PLANNED"], "PLANNED"),
        (["PASS", "PLANNED"], "PARTIAL"),
        (["PASS", "BLOCKED"], "BLOCKED"),
        (["FAILED", "BLOCKED"], "FAILED"),
        (["PASS", "PASS"], "PASS"),
        (["FAILED", "PLANNED"], "FAILED"),
    ],
)
def test_results_never_claim_unexecuted_work_passed(states, expected):
    assert runner.aggregate([{"status": value} for value in states]) == expected


@pytest.mark.parametrize(
    ("status", "plan", "code"),
    [
        ("PASS", False, 0),
        ("PLANNED", False, 2),
        ("PLANNED", True, 0),
        ("PARTIAL", False, 2),
        ("BLOCKED", True, 75),
        ("FAILED", False, 1),
    ],
)
def test_check_exit_contract(status, plan, code):
    assert runner.exit_code(status, plan_only=plan) == code


def fixture_registry(tmp_path):
    value = {
        "schema_version": 2,
        "checks": {
            "fast": {"kind": "local", "command": ["fast"], "requires": []},
            "local": {"kind": "local", "command": ["local"], "requires": []},
            "integration": {"kind": "integration", "command": ["integration"], "requires": []},
            "release": {"kind": "release", "command": ["release"], "requires": []},
        },
        "levels": {"fast": ["fast"], "changed": ["fast"], "release": ["release"]},
        "capabilities": {"capability": ["local", "integration"], "release": ["release"]},
        "paths": [
            {"classification": "active", "prefixes": ["app/"], "capabilities": ["capability"]},
            {"classification": "active", "prefixes": ["release/"], "capabilities": ["release"]},
        ],
    }
    path = tmp_path / "gates.json"
    path.write_text(json.dumps(value))
    return path


def test_changed_runs_only_local_checks_by_default(tmp_path, monkeypatch):
    path = fixture_registry(tmp_path)
    monkeypatch.setattr(runner, "changed_paths", lambda source: ["app/code.py"])
    commands = []

    def run(command, source, **kwargs):
        commands.append(command[0])
        return {"status": "PASS", "exit_code": 0}

    monkeypatch.setattr(runner, "execute", run)
    result = runner.run_checks(tmp_path, path, "changed", execute_requested=False, plan_only=False)
    assert commands == ["fast", "local"]
    assert result["status"] == "PARTIAL"
    assert result["pending_checks"] == ["integration"]


def test_explicit_plan_never_dispatches_a_check(tmp_path, monkeypatch):
    path = fixture_registry(tmp_path)
    monkeypatch.setattr(runner, "execute", lambda *args, **kwargs: pytest.fail("plan executed"))
    result = runner.run_checks(tmp_path, path, "fast", execute_requested=False, plan_only=True)
    assert result["status"] == "PLANNED"


def test_release_child_failure_and_blocked_change_are_not_success(tmp_path, monkeypatch):
    path = fixture_registry(tmp_path)
    monkeypatch.setattr(
        runner, "execute", lambda *args, **kwargs: {"status": "FAILED", "exit_code": 1}
    )
    result = runner.run_checks(tmp_path, path, "release", execute_requested=True, plan_only=False)
    assert result["status"] == "FAILED"
    monkeypatch.setattr(
        runner, "execute", lambda *args, **kwargs: {"status": "PASS", "exit_code": 0}
    )
    value = json.loads(path.read_text())
    value["checks"]["release"]["requires"] = ["p8-direct-config"]
    path.write_text(json.dumps(value))
    monkeypatch.setattr(runner, "changed_paths", lambda source: ["release/code.py"])
    result = runner.run_checks(tmp_path, path, "changed", execute_requested=True, plan_only=False)
    assert result["status"] == "BLOCKED"
    assert result["blocked_capabilities"] == ["release"]


def test_check_cleanup_failure_cannot_mask_child_result(tmp_path, monkeypatch):
    seen = {}

    class CleanupSensitiveDirectory:
        def __init__(self, *args, ignore_cleanup_errors=False, **kwargs):
            seen["ignore_cleanup_errors"] = ignore_cleanup_errors
            self.path = tmp_path / "check-work"

        def __enter__(self):
            self.path.mkdir()
            return str(self.path)

        def __exit__(self, *_):
            if not seen["ignore_cleanup_errors"]:
                raise PermissionError("namespace-owned fixture")

    monkeypatch.setattr(runner.tempfile, "TemporaryDirectory", CleanupSensitiveDirectory)
    monkeypatch.setattr(runner, "child_environment", lambda **_: {})
    monkeypatch.setattr(runner, "owned_path", lambda *_: tmp_path / "maintenance-checks")
    source = tmp_path / "source"
    source.mkdir()

    result = runner.execute(
        [sys.executable, "-c", "raise SystemExit(7)"],
        source,
        testing=True,
    )

    assert seen == {"ignore_cleanup_errors": True}
    assert result == {"status": "FAILED", "exit_code": 7}


def test_unknown_changes_invalid_registry_and_git_failure_close(tmp_path, monkeypatch):
    path = fixture_registry(tmp_path)
    monkeypatch.setattr(runner, "changed_paths", lambda source: ["unowned/code.py"])
    result = runner.run_checks(tmp_path, path, "changed", execute_requested=False, plan_only=True)
    assert result["status"] == "FAILED"
    assert result["unmapped_paths"] == ["unowned/code.py"]
    value = json.loads(path.read_text())
    value["levels"]["fast"] = ["unknown"]
    path.write_text(json.dumps(value))
    with pytest.raises(runner.CheckFailure, match="REGISTRY_INVALID"):
        runner.load_registry(path)
    monkeypatch.undo()
    with pytest.raises(runner.CheckFailure, match="GIT_CHANGE_QUERY_FAILED"):
        runner.changed_paths(tmp_path)


def test_git_collects_staged_unstaged_untracked_deleted_and_both_rename_paths(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    for name in ("old", "deleted", "edited"):
        (tmp_path / name).write_text(name)
    git("add", ".")
    git(
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    git("mv", "old", "new")
    (tmp_path / "deleted").unlink()
    (tmp_path / "edited").write_text("changed")
    (tmp_path / "untracked").write_text("untracked")
    assert set(runner.changed_paths(tmp_path)) == {"old", "new", "deleted", "edited", "untracked"}


def test_real_registry_covers_maintenance_and_dependencies():
    registry = runner.load_registry(REGISTRY)
    caps, unknown = runner.select(registry, ["scripts/maintenance/cleanup_core.py"])
    assert "maintenance" in caps and not unknown
    caps, unknown = runner.select(registry, ["pyproject.toml", "uv.lock"])
    assert set(caps) == set(registry["capabilities"]) and not unknown
    if registry["project"].endswith("v7"):
        for path, expected in (
            ("apps/knowledge-service/src/platform_knowledge/api.py", "knowledge"),
            ("apps/data-service/src/platform_data/api.py", "data"),
            ("packages/operator-pack/src/platform_operators/series.py", "operators"),
            (
                "packages/tabular-operator-pack/src/platform_tabular_operators/operators.py",
                "operators",
            ),
            ("packages/checker-pack/src/platform_checkers/tabular.py", "operators"),
        ):
            caps, unknown = runner.select(registry, [path])
            assert expected in caps and not unknown
        assert all(
            "identity" not in item["command"] or "check_p2.sh" not in " ".join(item["command"])
            for item in registry["checks"].values()
        )


def test_local_check_targets_do_not_include_full_phase_scripts():
    registry = runner.load_registry(REGISTRY)
    for check in registry["checks"].values():
        if check["kind"] == "local":
            text = " ".join(check["command"])
            assert "check_p2.sh" not in text and "npm ci" not in text
            assert "--phase" not in text


@pytest.mark.parametrize("name", ["check_p3.sh", "check_p4.sh", "check_p42.sh", "check_p422.sh"])
def test_full_suite_image_gates_refresh_checker_lock_after_p3_build(name):
    script = (ROOT / "scripts/checks" / name).read_text(encoding="utf-8")
    p3_build = script.index("build_p3_images.sh")
    checker_build = script.index("build_p5_checker_images.sh")
    full_suite = script.index("pytest -q --tb=short")
    assert p3_build < checker_build < full_suite


@pytest.mark.parametrize(
    "field,value",
    [
        ("capabilities", []),
        ("paths", "invalid"),
        ("paths", [None]),
    ],
)
def test_malformed_registry_is_an_explicit_failure(tmp_path, field, value):
    path = fixture_registry(tmp_path)
    registry = json.loads(path.read_text())
    registry[field] = value
    path.write_text(json.dumps(registry))
    with pytest.raises(runner.CheckFailure, match="REGISTRY_INVALID"):
        runner.load_registry(path)


def test_ops_rejects_execute_with_plan():
    from scripts.maintenance import ops

    with pytest.raises(SystemExit) as error:
        ops._parse(["check", "changed", "--plan", "--execute"])
    assert error.value.code == 2


def test_plan_flag_is_not_silently_ignored_by_other_commands():
    from scripts.maintenance import ops

    with pytest.raises(SystemExit) as error:
        ops._parse(["cleanup", "verify", "--plan"])
    assert error.value.code == 2


def test_release_verify_retains_planned_failed_and_blocked_states(monkeypatch):
    from scripts.maintenance import ops

    if runner.load_registry(REGISTRY)["project"].endswith("v7"):
        monkeypatch.setattr(ops, "_p8_env_file", lambda value: Path("/fixture/compose.env"))
        monkeypatch.setattr(
            ops,
            "_run",
            lambda command, execute: {
                "command": "fixture",
                "status": "FAILED" if execute else "PLANNED",
            },
        )
        assert (
            ops.release("verify", "fixture", execute=False, plan_only=True)["status"] == "PLANNED"
        )
        assert ops.release("verify", "fixture", execute=True)["status"] == "FAILED"

        def missing_direct_environment(_value):
            raise ops.OpsFailure("P8_DIRECT_CONFIG_REQUIRED")

        monkeypatch.setattr(ops, "_p8_env_file", missing_direct_environment)
        with pytest.raises(ops.OpsFailure, match="P8_DIRECT_CONFIG_REQUIRED"):
            ops.release("verify", None, execute=False)
    else:
        calls = []

        def check(level, *, execute, plan_only):
            calls.append((level, execute, plan_only))
            return {"status": "PLANNED"}

        monkeypatch.setattr(ops, "check", check)
        assert ops.release("verify", execute=False, plan_only=True)["status"] == "PLANNED"
        assert calls == [("release", False, True)]
