from pathlib import Path

import pytest
from platform_contracts.service import ServiceFault
from platform_execution.adapters.docker import Docker
from platform_execution.application.components import (
    ArtifactPublisher,
    InputMaterializer,
    LocalExecutionDriver,
    OutputCollector,
    RemoteExecutionDriver,
    WorkflowExecutionDriver,
    WorkflowInputMaterializer,
    WorkflowOutputCollector,
)
from platform_execution.application.execution_lease import ExecutionLeaseGuard
from platform_execution.application.execution_state import (
    LOCAL_EXECUTION_STATE,
    WORKFLOW_EXECUTION_STATE,
)
from platform_execution.application.runner import Runner, execution_root_mode
from platform_execution.application.workflow_runner import WorkflowRunner


def test_state_machines_reject_unknown_phases():
    assert LOCAL_EXECUTION_STATE.phase({"runtime": {"phase": "ACCEPTED"}}) == "ACCEPTED"
    assert WORKFLOW_EXECUTION_STATE.phase({"runtime": {"phase": "ENGINE_RUNNING"}}) == (
        "ENGINE_RUNNING"
    )
    with pytest.raises(ValueError, match="Unknown execution phase"):
        LOCAL_EXECUTION_STATE.phase({"runtime": {"phase": "ENGINE_RUNNING"}})


@pytest.mark.parametrize(
    ("machine", "source", "target"),
    [
        (LOCAL_EXECUTION_STATE, "ACCEPTED", "PREPARED"),
        (LOCAL_EXECUTION_STATE, "PREPARED", "REMOTE_SUBMITTING"),
        (LOCAL_EXECUTION_STATE, "RUNNING", "PUBLISHING"),
        (LOCAL_EXECUTION_STATE, "REPORTING", "FINAL"),
        (WORKFLOW_EXECUTION_STATE, "PREPARED", "ENGINE_CREATED"),
        (WORKFLOW_EXECUTION_STATE, "ENGINE_RUNNING", "RESTART_READY"),
        (WORKFLOW_EXECUTION_STATE, "ENGINE_RUNNING", "PUBLISHING"),
    ],
)
def test_state_machines_accept_declared_transitions(machine, source, target):
    machine.validate_transition(source, target)


@pytest.mark.parametrize(
    ("machine", "source", "target"),
    [
        (LOCAL_EXECUTION_STATE, "ACCEPTED", "RUNNING"),
        (LOCAL_EXECUTION_STATE, "PUBLISHING", "PREPARED"),
        (LOCAL_EXECUTION_STATE, "FINAL", "REPORTING"),
        (WORKFLOW_EXECUTION_STATE, "ACCEPTED", "ENGINE_RUNNING"),
        (WORKFLOW_EXECUTION_STATE, "PUBLISHING", "RESTART_READY"),
        (WORKFLOW_EXECUTION_STATE, "FINAL", "PREPARED"),
    ],
)
def test_state_machines_reject_undeclared_transitions(machine, source, target):
    with pytest.raises(ValueError, match="Illegal .* transition"):
        machine.validate_transition(source, target)


def test_workflow_engine_profile_must_be_explicit():
    with pytest.raises(ServiceFault) as caught:
        WorkflowExecutionDriver.engine_profile({"runtime": {}})
    assert (caught.value.code, caught.value.status) == ("INVALID_BINDING", 422)


def test_runner_constructors_install_concrete_components(tmp_path: Path):
    action = Runner(object(), tmp_path / "actions", object(), object(), object())
    assert isinstance(action.input_materializer, InputMaterializer)
    assert isinstance(action.output_collector, OutputCollector)
    assert isinstance(action.artifact_publisher, ArtifactPublisher)
    assert isinstance(action.local_driver, LocalExecutionDriver)
    assert isinstance(action.remote_driver, RemoteExecutionDriver)

    workflow = WorkflowRunner(object(), object(), object(), object(), "http://execution")
    assert isinstance(workflow.input_materializer, WorkflowInputMaterializer)
    assert isinstance(workflow.output_collector, WorkflowOutputCollector)
    assert isinstance(workflow.driver, WorkflowExecutionDriver)


def test_runner_accepts_a_prepared_rootless_bind_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "shared-actions"
    root.mkdir()
    root.chmod(0o777)
    original = Path.chmod

    def deny_mount_chmod(path, mode, *, follow_symlinks=True):
        if path == root:
            raise PermissionError
        return original(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", deny_mount_chmod)
    runner = Runner(object(), root, object(), object(), object())
    assert runner.root == root


@pytest.mark.parametrize("phase", ("ENGINE_CREATED", "ENGINE_RUNNING"))
def test_runner_preserves_writable_root_for_live_rootless_workflow(phase):
    row = {"payload": {"kind": "WORKFLOW"}, "runtime": {"phase": phase}}
    assert execution_root_mode(row) == 0o777


@pytest.mark.parametrize("phase", ("ACCEPTED", "PREPARED", "RESTART_READY", "PUBLISHING"))
def test_runner_keeps_non_live_workflow_root_private(phase):
    row = {"payload": {"kind": "WORKFLOW"}, "runtime": {"phase": phase}}
    assert execution_root_mode(row) == 0o700


def test_lease_guard_releases_claim_when_process_lock_is_busy(tmp_path: Path, monkeypatch):
    class Repository:
        released = []

        @staticmethod
        def renew(row, seconds):
            return True

        def release(self, row, delay):
            self.released.append((row, delay))

    repository = Repository()
    row = {"action_id": "action", "generation": 1}

    def busy(*args):
        raise BlockingIOError

    monkeypatch.setattr("platform_execution.application.execution_lease.fcntl.flock", busy)
    with ExecutionLeaseGuard(repository, row, tmp_path, 30, 10) as guard:
        assert not guard.acquired
    assert repository.released == [(row, 0.2)]


def test_frozen_operator_image_is_enforced_in_development():
    installed = "sha256:" + "a" * 64
    docker = Docker(installed, "sha256:" + "b" * 64)
    row = {
        "payload": {
            "kind": "OPERATOR",
            "target": {
                "kind": "OPERATOR",
                "operator_id": "timeseries-completeness",
                "version_id": "1.0.0",
                "execution_binding": {
                    "kind": "LOCAL",
                    "implementation_id": "p3-completeness-v1",
                },
            },
            "_operator_snapshot": {
                "operator_id": "timeseries-completeness",
                "version_id": "1.0.0",
                "operator_digest": "sha256:" + "c" * 64,
                "catalog_digest": "sha256:" + "d" * 64,
                "ownership": "CORE",
                "trust_level": "TRUSTED",
                "implementation": {
                    "implementation_id": "p3-completeness-v1",
                    "implementation_digest": "sha256:" + "e" * 64,
                    "image_digest": "sha256:" + "f" * 64,
                    "protocol": "P3_TABLE",
                    "resource_profile": "native-small-v1",
                },
            },
        }
    }
    with pytest.raises(ValueError, match="Catalog image differs"):
        docker.profile(row)
