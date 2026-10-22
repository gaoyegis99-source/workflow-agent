"""Verify uncertain runtime facts with a real Execution ledger and controlled Docker replies."""

from datetime import UTC, datetime
from unittest.mock import Mock

from platform_execution.adapters.repository import Repository
from platform_execution.worker import Worker


def seed(service_databases, tmp_path, *, phase, executed):
    database = service_databases[1]
    repo = Repository(database)
    repo.create("action", {"project_id": "project", "task_id": "task"})
    row = repo.claim("seed")
    repo.change(
        row,
        state="RUNNING" if executed else "QUEUED",
        executed=executed,
        runtime={"phase": phase, "uncertain": True, "publication": "UNKNOWN"},
    )
    repo.release(row, 0)
    docker, data = Mock(), Mock()
    worker = Worker(database, tmp_path, docker, data, Mock())
    return repo, worker, docker, data


def test_running_fact_recovers_same_source_version(service_databases, tmp_path):
    repo, worker, docker, _ = seed(service_databases, tmp_path, phase="RUNNING", executed=True)
    before = repo.get(action="action")
    docker.inspect.return_value = {
        "State": {"StartedAt": datetime.now(UTC).isoformat(), "Running": True}
    }
    worker.tick()
    after = repo.get(action="action")
    assert after["revision"] == before["revision"]
    assert repo.observation(after)["state"] == "RUNNING"
    assert after["executed"] and not after["runtime"].get("uncertain")


def test_cancel_missing_start_intent_does_not_claim_unexecuted(service_databases, tmp_path):
    repo, worker, docker, data = seed(
        service_databases, tmp_path, phase="START_INTENT", executed=False
    )
    repo.cancel("action")
    docker.inspect.return_value = None
    worker.tick()
    after = repo.get(action="action")
    assert after["state"] == "QUEUED" and after["runtime"]["uncertain"]
    assert not after["executed"]
    data.json.assert_not_called()
