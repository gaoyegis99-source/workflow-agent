import math
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_execution import database as execution_database
from platform_execution.adapters.repository import Repository
from platform_execution.trace import build_event, record


def root_payload():
    return {
        "project_id": "project",
        "task_id": "task",
        "kind": "WORKFLOW",
    }


def child_payload(root):
    return {
        "project_id": "project",
        "task_id": "task",
        "kind": "OPERATOR",
        "node_request": {"epoch": 1},
        "parent_action_id": root["action_id"],
        "parent_execution_id": root["execution_id"],
    }


def test_trace_is_append_only_deduplicated_and_ordered(service_databases):
    repository = Repository(service_databases[1])
    root = repository.create("workflow-action", root_payload())
    child = repository.create(
        "node-action",
        child_payload(root),
        parent_execution_id=root["execution_id"],
        invocation_key="node-action",
    )
    event = build_event(
        workflow_execution_id=root["execution_id"],
        node_action_id=child["action_id"],
        engine_epoch=1,
        worker_generation=2,
        source="EXECUTION_WORKER",
        event_type="CONTAINER_STARTED",
        discriminator=child["attempt_id"],
        source_monotonic_ns=123,
        duration_ms=1.25,
        attributes={"container_id": "container"},
    )
    assert repository.append_trace(event)
    assert not repository.append_trace(event)
    rows = repository.traces(root["execution_id"])
    assert len(rows) == 1
    assert rows[0]["event_key"] == event["event_key"]
    assert rows[0]["node_action_id"] == child["action_id"]
    assert rows[0]["engine_epoch"] == 1
    assert rows[0]["worker_generation"] == 2
    assert rows[0]["attributes"] == {"container_id": "container"}
    assert rows[0]["observed_at"] is not None


@pytest.mark.parametrize(
    ("attributes", "duration"),
    [
        ({"token": "secret"}, 1),
        ({"container_id": {"nested": "payload"}}, 1),
        ({"container_id": "ok"}, math.nan),
    ],
)
def test_trace_rejects_unbounded_or_sensitive_values(attributes, duration):
    with pytest.raises(ValueError):
        build_event(
            workflow_execution_id="execution",
            source="EXECUTION_WORKER",
            event_type="CONTAINER_STARTED",
            discriminator="one",
            duration_ms=duration,
            attributes=attributes,
        )


def test_engine_binding_trace_allows_only_bounded_diagnostic_fields():
    event = build_event(
        workflow_execution_id="execution",
        node_action_id="node",
        engine_epoch=2,
        source="BRIDGE",
        event_type="ENGINE_BINDING_OBSERVED",
        discriminator=["instance", "job"],
        attributes={"kind": "toil", "binding_id": "job"},
    )
    assert event["attributes"] == {"kind": "toil", "binding_id": "job"}
    with pytest.raises(ValueError):
        build_event(
            workflow_execution_id="execution",
            source="BRIDGE",
            event_type="ENGINE_BINDING_OBSERVED",
            discriminator="bad",
            attributes={"token": "secret"},
        )


def test_trace_storage_failure_does_not_escape():
    class Unavailable:
        def append_trace(self, event):
            raise sa.exc.OperationalError("insert", {}, RuntimeError("unavailable"))

    result = record(
        Unavailable(),
        workflow_execution_id="execution",
        source="ENGINE",
        event_type="ENGINE_STARTED",
        discriminator="one",
        attributes={"container_id": "container"},
    )
    assert result == {"inserted": False, "failed": True, "write_ms": None}


def test_trace_validation_failure_does_not_escape_or_write():
    class MustNotWrite:
        def append_trace(self, event):
            raise AssertionError("invalid diagnostic event reached storage")

    result = record(
        MustNotWrite(),
        workflow_execution_id="execution",
        source="ENGINE",
        event_type="ENGINE_STARTED",
        discriminator="one",
        attributes={"token": "secret"},
    )
    assert result == {"inserted": False, "failed": True, "write_ms": None}


def test_trace_storage_recovery_records_new_events(service_databases):
    repository = Repository(service_databases[1])
    root = repository.create("trace-recovery", root_payload())

    class FailsOnce:
        failed = False

        def append_trace(self, event):
            if not self.failed:
                self.failed = True
                raise sa.exc.OperationalError("insert", {}, RuntimeError("unavailable"))
            return repository.append_trace(event)

    unavailable = FailsOnce()
    first = record(
        unavailable,
        workflow_execution_id=root["execution_id"],
        source="ENGINE",
        event_type="ENGINE_STARTED",
        discriminator="lost-diagnostic",
        attributes={"container_id": "container"},
    )
    second = record(
        unavailable,
        workflow_execution_id=root["execution_id"],
        source="ENGINE",
        event_type="ENGINE_STARTED",
        discriminator="recorded-after-recovery",
        attributes={"container_id": "container"},
    )
    assert first["failed"] is True
    assert second["failed"] is False and second["inserted"] is True
    assert len(repository.traces(root["execution_id"])) == 1


def test_0002_upgrade_preserves_existing_execution(service_databases):
    database = service_databases[1]
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(execution_database.__file__).parent / "migrations"),
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_workflows")
        connection.execute(
            sa.text(
                """
                INSERT INTO executions (
                  action_id, execution_id, project_id, task_id, digest, payload, state,
                  revision, executed, cancel_requested, attempt_id, runtime, generation
                ) VALUES (
                  'upgrade-workflow', 'eupgrade', 'project', 'task', 'legacy-digest',
                  '{"project_id":"project","task_id":"task","kind":"WORKFLOW"}'::jsonb,
                  'QUEUED', 1, false, false, 'aupgrade',
                  '{"phase":"ACCEPTED","publication":"UNKNOWN"}'::jsonb, 0
                )
                """
            )
        )
    repository = Repository(database)
    before = {
        "action_id": "upgrade-workflow",
        "execution_id": "eupgrade",
        "project_id": "project",
        "task_id": "task",
        "digest": "legacy-digest",
        "state": "QUEUED",
        "revision": 1,
        "executed": False,
        "attempt_id": "aupgrade",
        "runtime": {"phase": "ACCEPTED", "publication": "UNKNOWN"},
    }
    database.migrate()
    database.migrate()
    database.check_schema()
    upgraded = repository.get(action="upgrade-workflow")
    assert {key: upgraded[key] for key in before} == before
    assert repository.traces(before["execution_id"]) == []
    assert repository.append_trace(
        build_event(
            workflow_execution_id=before["execution_id"],
            source="ENGINE",
            event_type="ENGINE_STARTED",
            discriminator="rollback-only-observation",
            attributes={"container_id": "container"},
        )
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_workflows")
    assert "workflow_trace_events" not in sa.inspect(database.engine).get_table_names()
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT execution_id FROM executions WHERE action_id = 'upgrade-workflow'")
            )
            == before["execution_id"]
        )
    database.migrate()
    database.check_schema()
