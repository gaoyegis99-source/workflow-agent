import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from platform_contracts.engine_runtime import OFF_PROFILE_ID, profile
from platform_execution.adapters.docker import CwlDocker
from platform_execution.application.workflow_runner import WorkflowRunner

ROOT = Path(__file__).resolve().parents[2]
ENGINE_SOURCE = ROOT / "runtimes/cwl/engine.py"
ENGINE_SPEC = importlib.util.spec_from_file_location("p4_cwl_engine", ENGINE_SOURCE)
assert ENGINE_SPEC is not None and ENGINE_SPEC.loader is not None
ENGINE = importlib.util.module_from_spec(ENGINE_SPEC)
ENGINE_SPEC.loader.exec_module(ENGINE)


def workflow_row():
    return {
        "action_id": "action",
        "execution_id": "execution",
        "attempt_id": "attempt",
        "digest": "digest",
    }


def test_cwl_engine_container_uses_init_and_preserves_security_contract(tmp_path, monkeypatch):
    calls = []
    runtime = CwlDocker("sha256:" + "1" * 64)

    def command(*arguments, timeout=5):
        calls.append((arguments, timeout))
        return ""

    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(
        runtime,
        "inspect",
        lambda execution, epoch: {
            "Id": "container",
            "Image": runtime.image,
            "Config": {"Labels": {}},
        },
    )
    config = tmp_path / "config.json"
    config.write_text("{}")
    runtime.create(workflow_row(), tmp_path, 1, config)

    create, timeout = calls[0]
    assert create[0] == "create"
    assert timeout == 5
    assert "--init" in create
    assert create[create.index("--stop-signal") + 1] == "SIGTERM"
    assert create[create.index("--stop-timeout") + 1] == "30"
    assert create[create.index("--user") + 1] not in {"0:0", "root"}
    assert "--read-only" in create
    assert create[create.index("--cap-drop") + 1] == "ALL"
    assert create[create.index("--security-opt") + 1] == "no-new-privileges"
    assert create[create.index("--network") + 1] == "host"
    assert create[create.index("--cpus") + 1] == "2"
    assert create[create.index("--memory") + 1] == "1g"


def test_legacy_running_engine_without_profile_labels_remains_compatible():
    runtime = CwlDocker("sha256:" + "1" * 64)
    row = workflow_row() | {
        "payload": {},
        "runtime": {
            "_engine_runtime_profile": profile(OFF_PROFILE_ID),
            "_engine_profile_legacy_unlabeled": True,
        },
    }
    info = {
        "Image": runtime.image,
        "Config": {
            "Labels": {
                "v7.action": row["action_id"],
                "v7.digest": row["digest"],
                "v7.attempt": row["attempt_id"],
                "v7.epoch": "1",
            }
        },
    }
    runtime.check(row, info, 1)
    row["runtime"].pop("_engine_profile_legacy_unlabeled")
    with pytest.raises(ValueError, match="labels differ"):
        runtime.check(row, info, 1)


def test_cwl_stop_uses_grace_period_and_confirms_stopped(monkeypatch):
    runtime = CwlDocker("sha256:" + "1" * 64)
    calls = []

    def command(*arguments, timeout=5):
        calls.append((arguments, timeout))
        return ""

    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(
        runtime, "inspect_container", lambda _container: {"State": {"Running": False}}
    )
    info = runtime.stop("container")

    assert calls == [(("stop", "--time", "30", "container"), 35)]
    assert info["State"]["Running"] is False


def test_cwl_stop_forces_only_after_failed_graceful_stop(monkeypatch):
    runtime = CwlDocker("sha256:" + "1" * 64)
    calls = []
    observations = iter(
        [
            {"Id": "container", "State": {"Running": True}},
            {"Id": "container", "State": {"Running": False}},
        ]
    )

    def command(*arguments, timeout=5):
        calls.append((arguments, timeout))
        if arguments[0] == "stop":
            raise subprocess.TimeoutExpired(arguments, timeout)
        return ""

    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(runtime, "inspect_container", lambda _container: next(observations))
    info = runtime.stop("container")

    assert calls == [
        (("stop", "--time", "30", "container"), 35),
        (("kill", "container"), 5),
    ]
    assert info["State"]["Running"] is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"epoch": 2, "returncode": 0}, {"epoch": 2, "returncode": 0}),
        (
            {
                "schema_version": 1,
                "epoch": 2,
                "returncode": -15,
                "termination_signal": "SIGTERM",
                "forced": False,
            },
            {
                "schema_version": 1,
                "epoch": 2,
                "returncode": -15,
                "termination_signal": "SIGTERM",
                "forced": False,
            },
        ),
        ({"schema_version": 1, "epoch": 1, "returncode": 0}, None),
        ({"schema_version": 1, "epoch": 2, "returncode": True}, None),
        ({"schema_version": 2, "epoch": 2, "returncode": 0}, None),
        ({"schema_version": 1, "epoch": 2, "returncode": 0, "forced": "no"}, None),
    ],
)
def test_exit_fact_accepts_legacy_and_rejects_invalid_or_wrong_epoch(tmp_path, value, expected):
    path = tmp_path / "exit-2.json"
    path.write_text(json.dumps(value))
    assert WorkflowRunner.exit_fact(tmp_path, 2) == expected


def test_exit_fact_rejects_partial_json(tmp_path):
    (tmp_path / "exit-2.json").write_text('{"schema_version": 1,')
    assert WorkflowRunner.exit_fact(tmp_path, 2) is None


def write_signal_probe(tmp_path, *, ignore=False):
    marker = tmp_path / "child-ready"
    received = tmp_path / "child-signal"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, signal, time\n"
        f"marker = {str(marker)!r}\n"
        f"received = {str(received)!r}\n"
        f"ignore = {ignore!r}\n"
        "def stop(signum, _frame):\n"
        "    open(received, 'a').write(str(signum) + '\\n')\n"
        "    if not ignore:\n"
        "        raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "open(marker, 'w').write(str(os.getpid()))\n"
        "while True: time.sleep(0.05)\n"
    )
    result = tmp_path / "supervisor-result.json"
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import importlib.util, json, pathlib, sys\n"
        f"spec = importlib.util.spec_from_file_location('engine', {str(ENGINE_SOURCE)!r})\n"
        "engine = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(engine)\n"
        f"output = open({str(tmp_path / 'child.out')!r}, 'wb')\n"
        f"value = engine.run_supervised([sys.executable, {str(child)!r}], output, "
        "grace_seconds=0.3, force_wait_seconds=0.5)\n"
        "output.close()\n"
        f"pathlib.Path({str(result)!r}).write_text(json.dumps(value))\n"
    )
    process = subprocess.Popen([sys.executable, str(launcher)])
    deadline = time.monotonic() + 5
    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), process.poll()
    return process, received, result


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_engine_supervisor_forwards_signal_and_waits_for_child(tmp_path, signum):
    process, received, result = write_signal_probe(tmp_path)
    process.send_signal(signum)
    assert process.wait(timeout=5) == 0
    value = json.loads(result.read_text())
    assert value["returncode"] == 0
    assert value["termination_signal"] == signal.Signals(signum).name
    assert value["forced"] is False
    assert received.read_text().splitlines() == [str(signum)]
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "child-ready").read_text()), 0)


def test_engine_supervisor_repeated_signal_does_not_extend_force_deadline(tmp_path):
    process, received, result = write_signal_probe(tmp_path, ignore=True)
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    time.sleep(0.2)
    process.send_signal(signal.SIGINT)
    assert process.wait(timeout=5) == 0
    elapsed = time.monotonic() - started
    value = json.loads(result.read_text())
    assert elapsed < 1.2
    assert value["returncode"] == -signal.SIGKILL
    assert value["termination_signal"] == "SIGTERM"
    assert value["forced"] is True
    assert received.read_text().splitlines() == [str(signal.SIGTERM)]


def test_engine_supervisor_keeps_signal_received_during_spawn(monkeypatch, tmp_path):
    forwarded = []

    class Process:
        pid = 12345

        @staticmethod
        def wait(timeout):
            assert timeout == 0.1
            return 0

    def spawn(*_arguments, **_keywords):
        os.kill(os.getpid(), signal.SIGTERM)
        return Process()

    monkeypatch.setattr(ENGINE.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        ENGINE, "_signal_group", lambda pid, signum: forwarded.append((pid, signum))
    )
    with (tmp_path / "output").open("wb") as output:
        result = ENGINE.run_supervised(["ignored"], output)
    assert forwarded == [(12345, signal.SIGTERM)]
    assert result == {
        "returncode": 0,
        "termination_signal": "SIGTERM",
        "forced": False,
    }
