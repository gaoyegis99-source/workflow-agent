from pathlib import Path

import pytest
from platform_contracts.service import ServiceFault
from platform_execution.application.execution_policies import PermitPolicy, TracePolicy
from platform_execution.application.execution_runtime import ExecutionRuntime, LeaseLostError
from platform_execution.application.execution_state import LOCAL_EXECUTION_STATE


class Repository:
    def __init__(self):
        self.events = []

    def change(self, row, **changes):
        row.update(changes)
        return row

    def transition(self, row, *, source_phase, runtime, **changes):
        assert row["runtime"]["phase"] == source_phase
        row.update(changes | {"runtime": runtime})
        return row

    def append_trace(self, event):
        self.events.append(event)
        return True


class Platform:
    def json(self, method, path, json):
        assert (method, path) == ("POST", "/permit")
        assert json == {"execution_id": "execution", "attempt_id": "attempt"}
        return {"decision": "ALLOW"}


class Trace(TracePolicy):
    @staticmethod
    def context(row, _event_type, epoch, source):
        return {
            "workflow_execution_id": row["execution_id"],
            "engine_epoch": epoch,
            "worker_generation": row["generation"],
            "source": source or "EXECUTION_WORKER",
        }


class Permit(PermitPolicy):
    @staticmethod
    def request(row):
        return "/permit", {
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
        }


def runtime():
    repository = Repository()
    return (
        ExecutionRuntime(
            repository,
            Platform(),
            trace_policy=Trace(),
            permit_policy=Permit(),
            changed_message="Frozen content changed",
            permit_source="PLATFORM_CLIENT",
        ),
        repository,
    )


def test_shared_runtime_freezes_once_and_rejects_changed_content(tmp_path: Path):
    service, _ = runtime()
    path = tmp_path / "nested" / "frozen.json"
    service.save(path, b"one")
    service.save(path, b"one")
    assert path.read_bytes() == b"one"
    assert path.stat().st_mode & 0o777 == 0o444
    with pytest.raises(ServiceFault) as raised:
        service.save(path, b"two")
    assert raised.value.code == "INVALID_BINDING"


def test_shared_runtime_clears_transient_failure_and_traces_permit():
    service, repository = runtime()
    row = {
        "action_id": "action",
        "execution_id": "execution",
        "attempt_id": "attempt",
        "generation": 3,
        "runtime": {"phase": "PREPARED", "uncertain": True, "failures": 2},
    }
    assert service.change(row, marker="current")["runtime"] == {
        "phase": "PREPARED",
        "marker": "current",
    }
    row["runtime"] = {"phase": "PREPARED"}
    assert service.permit(row) == {"decision": "ALLOW"}
    assert [event["event_type"] for event in repository.events] == [
        "PERMIT_REQUESTED",
        "PERMIT_ALLOWED",
    ]
    assert all(event["source"] == "PLATFORM_CLIENT" for event in repository.events)


def test_shared_runtime_enforces_declared_phase_transitions():
    repository = Repository()
    service = ExecutionRuntime(
        repository,
        Platform(),
        trace_policy=Trace(),
        permit_policy=Permit(),
        changed_message="Frozen content changed",
        permit_source="PLATFORM_CLIENT",
        state_machine=LOCAL_EXECUTION_STATE,
    )
    row = {"runtime": {"phase": "PREPARED"}}
    service.transition(row, "CREATE_INTENT")
    row["runtime"] = {"phase": "PREPARED"}
    with pytest.raises(ValueError, match="Illegal execution transition"):
        service.transition(row, "RUNNING")


def test_shared_runtime_persists_container_identity_outside_diagnostic_runtime():
    repository = Repository()
    service = ExecutionRuntime(
        repository,
        Platform(),
        trace_policy=Trace(),
        permit_policy=Permit(),
        changed_message="Frozen content changed",
        permit_source="PLATFORM_CLIENT",
        state_machine=LOCAL_EXECUTION_STATE,
    )
    row = {"runtime": {"phase": "CREATE_INTENT"}, "container_id": None}

    service.transition(row, "CREATED", container_id="container-id")

    assert row["container_id"] == "container-id"
    assert row["runtime"] == {"phase": "CREATED"}


def test_runtime_refuses_writes_and_effects_after_lease_loss():
    service, repository = runtime()
    service.state_machine = LOCAL_EXECUTION_STATE
    row = {
        "action_id": "action",
        "execution_id": "execution",
        "attempt_id": "attempt",
        "generation": 1,
        "runtime": {"phase": "PREPARED"},
    }

    class LostLease:
        @staticmethod
        def assert_active():
            raise LeaseLostError("EXECUTION_LEASE_LOST")

    service.bind_lease(LostLease())
    with pytest.raises(LeaseLostError):
        service.change(row, marker="must-not-write")
    with pytest.raises(LeaseLostError):
        service.transition(row, "CREATE_INTENT")
    with pytest.raises(LeaseLostError):
        service.effect("platform.request", lambda: None)
    assert repository.events == []
    assert row["runtime"] == {"phase": "PREPARED"}
