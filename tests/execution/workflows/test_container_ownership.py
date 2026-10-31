import subprocess
from types import SimpleNamespace

import pytest

from tests.platform.services import harness


def test_copied_driver_requires_an_explicit_owned_docker_launcher(monkeypatch, tmp_path):
    launcher = tmp_path / "docker"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    monkeypatch.setenv("V7_TEST_DOCKER_EXECUTABLE", str(launcher))

    assert harness.docker_executable() == str(launcher)

    link = tmp_path / "linked-docker"
    link.symlink_to(launcher)
    monkeypatch.setenv("V7_TEST_DOCKER_EXECUTABLE", str(link))
    with pytest.raises(RuntimeError, match="V7_TEST_DOCKER_EXECUTABLE_INVALID"):
        harness.docker_executable()


def test_cleanup_rechecks_test_run_label_before_removal(monkeypatch):
    run_id = "a" * 32
    calls = []

    def execute(command, **_kwargs):
        calls.append(command)
        if command[1:4] == ["ps", "-aq", "--filter"]:
            return SimpleNamespace(returncode=0, stdout=b"owned\nhistorical\n")
        if command[1] == "inspect":
            label = run_id if command[-1] == "owned" else "b" * 32
            return SimpleNamespace(returncode=0, stdout=(label + "\n").encode())
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(harness.subprocess, "run", execute)
    harness.cleanup_test_containers(run_id)

    removals = [command for command in calls if command[1:4] == ["rm", "-f", "owned"]]
    assert len(removals) == 1
    assert not any(command[1:4] == ["rm", "-f", "historical"] for command in calls)
    assert not any("v7.phase" in part for command in calls for part in command)


def test_cleanup_rejects_unscoped_or_shell_like_identity():
    for value in ("", "short", "*", "run;docker-rm"):
        try:
            harness.cleanup_test_containers(value)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe test run identity was accepted")


def test_cleanup_reports_timed_out_label_check_for_bounded_retry(monkeypatch):
    run_id = "a" * 32
    calls = []

    def execute(command, **_kwargs):
        calls.append(command)
        if command[1:4] == ["ps", "-aq", "--filter"]:
            return SimpleNamespace(returncode=0, stdout=b"owned\n")
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(harness.subprocess, "run", execute)

    assert harness.cleanup_test_containers(run_id) == 1
    assert not any(command[1:3] == ["rm", "-f"] for command in calls)


def test_owned_container_kill_recovers_when_response_is_lost(monkeypatch):
    run_id = "a" * 32
    container = "v7-p4-" + "b" * 32 + "-e1"
    calls = []

    def execute(command, **_kwargs):
        calls.append(command)
        if command[1] == "kill":
            raise subprocess.TimeoutExpired(command, 10)
        running = len([call for call in calls if call[1] == "inspect"]) == 1
        return SimpleNamespace(
            returncode=0,
            stdout=f"{'true' if running else 'false'}\t{run_id}\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(harness.subprocess, "run", execute)

    harness.kill_owned_test_container(container, run_id, timeout=1)

    assert len([command for command in calls if command[1] == "kill"]) == 1


def test_owned_container_kill_refuses_a_different_test_run(monkeypatch):
    run_id = "a" * 32
    container = "v7-p3-" + "b" * 32
    calls = []

    def execute(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=("true\t" + "c" * 32 + "\n").encode())

    monkeypatch.setattr(harness.subprocess, "run", execute)

    with pytest.raises(RuntimeError, match="unowned V7 test container"):
        harness.kill_owned_test_container(container, run_id, timeout=1)
    assert not any(command[1] == "kill" for command in calls)


def test_network_cleanup_retries_until_owned_network_is_removed(monkeypatch):
    run_id = "a" * 32
    cluster = harness.Cluster.__new__(harness.Cluster)
    cluster.test_run_id = run_id
    cluster.workload_network = f"v7-test-{run_id}-workload"
    cluster.workload_network_created = True
    removals = 0

    def execute(command, **_kwargs):
        nonlocal removals
        if command[1:3] == ["network", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=(run_id + "\n").encode())
        if command[1:3] == ["network", "rm"]:
            removals += 1
            return SimpleNamespace(returncode=0 if removals == 3 else 1, stdout=b"")
        raise AssertionError(command)

    monkeypatch.setattr(harness.subprocess, "run", execute)
    monkeypatch.setattr(harness.time, "sleep", lambda _seconds: None)

    cluster.cleanup_workload_network()

    assert removals == 3
    assert not cluster.workload_network_created


def test_network_cleanup_refuses_a_changed_ownership_label(monkeypatch):
    run_id = "a" * 32
    cluster = harness.Cluster.__new__(harness.Cluster)
    cluster.test_run_id = run_id
    cluster.workload_network = f"v7-test-{run_id}-workload"
    cluster.workload_network_created = True

    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"different-owner\n"),
    )

    with pytest.raises(RuntimeError, match="unowned workflow test network"):
        cluster.cleanup_workload_network()
    assert cluster.workload_network_created
