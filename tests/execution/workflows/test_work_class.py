from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_contracts.service import ServiceFault
from platform_execution import database as execution_database
from platform_execution.adapters.repository import Repository
from platform_execution.worker import ROLE_CLASSES, Worker


def workflow_payload():
    return {"project_id": "project", "task_id": "task", "kind": "WORKFLOW"}


def node_payload():
    return {"project_id": "project", "task_id": "task", "kind": "OPERATOR"}


def test_roles_claim_only_their_durable_work_class(service_databases):
    repository = Repository(service_databases[1])
    root = repository.create("root", workflow_payload())
    node = repository.create(
        "node",
        node_payload(),
        parent_execution_id=root["execution_id"],
        invocation_key="node",
    )
    assert root["work_class"] == "WORKFLOW_SUPERVISOR"
    assert node["work_class"] == "NODE_EXECUTION"
    assert repository.claim("publication", work_classes=ROLE_CLASSES["publication"]) is None
    claimed_root = repository.claim("supervisor", work_classes=ROLE_CLASSES["supervisor"])
    claimed_node = repository.claim("node", work_classes=ROLE_CLASSES["node"])
    assert claimed_root["action_id"] == root["action_id"]
    assert claimed_node["action_id"] == node["action_id"]


def test_supervisor_retains_publication_reconciliation_capacity(service_databases):
    repository = Repository(service_databases[1])
    repository.create("publish", node_payload())
    claimed = repository.claim("node", work_classes=ROLE_CLASSES["node"])
    assert repository.transition(
        claimed,
        source_phase="ACCEPTED",
        runtime=claimed["runtime"] | {"phase": "PUBLISHING"},
    )
    assert repository.release(claimed, 0)
    reconciler = repository.claim("supervisor", work_classes=ROLE_CLASSES["supervisor"])
    assert reconciler["action_id"] == "publish"


def test_publication_transition_moves_claim_to_publication_role(service_databases):
    repository = Repository(service_databases[1])
    repository.create("direct", node_payload())
    claimed = repository.claim("node", work_classes=ROLE_CLASSES["node"])
    runtime = claimed["runtime"] | {"phase": "PUBLISHING", "manifest": {}}
    assert repository.transition(claimed, source_phase="ACCEPTED", runtime=runtime)
    assert repository.release(claimed, 0)
    assert repository.claim("node-2", work_classes=ROLE_CLASSES["node"]) is None
    publishing = repository.claim("publication", work_classes=ROLE_CLASSES["publication"])
    assert publishing["action_id"] == "direct"
    assert publishing["work_class"] == "PUBLICATION_RECONCILE"


def test_invalid_work_class_filter_is_rejected(service_databases):
    repository = Repository(service_databases[1])
    with pytest.raises(ServiceFault) as problem:
        repository.claim("worker", work_classes=("UNTRUSTED",))
    assert problem.value.code == "INVALID_BINDING"
    with pytest.raises(ValueError):
        Worker(None, ".", None, None, None, role="untrusted")


def test_0003_upgrade_classifies_existing_work_and_preserves_facts(service_databases):
    database = service_databases[1]
    config = Config()
    config.set_main_option(
        "script_location", str(Path(execution_database.__file__).parent / "migrations")
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0003_workflow_trace")
        base = {
            "project": "project",
            "task": "task",
            "state": "QUEUED",
            "revision": 1,
            "executed": False,
            "cancel": False,
        }
        connection.execute(
            sa.text(
                """
                INSERT INTO executions (
                  action_id, execution_id, project_id, task_id, digest, payload, state,
                  revision, executed, cancel_requested, attempt_id, runtime,
                  generation, available_at
                ) VALUES
                ('legacy-root','eroot',:project,:task,'root',
                 '{"kind":"WORKFLOW"}'::jsonb,:state,:revision,:executed,:cancel,
                 'aroot','{"phase":"ENGINE_RUNNING"}'::jsonb,0,clock_timestamp()),
                ('legacy-node','enode',:project,:task,'node',
                 '{"kind":"OPERATOR"}'::jsonb,:state,:revision,:executed,:cancel,
                 'anode','{"phase":"RUNNING"}'::jsonb,0,clock_timestamp()),
                ('legacy-publish','epublish',:project,:task,'publish',
                 '{"kind":"OPERATOR"}'::jsonb,:state,:revision,:executed,:cancel,
                 'apublish','{"phase":"PUBLISHING"}'::jsonb,0,clock_timestamp())
                """
            ),
            base,
        )
    database.migrate()
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        classes = dict(
            connection.execute(
                sa.text("SELECT action_id, work_class FROM executions ORDER BY action_id")
            ).all()
        )
        assert classes == {
            "legacy-node": "NODE_EXECUTION",
            "legacy-publish": "PUBLICATION_RECONCILE",
            "legacy-root": "WORKFLOW_SUPERVISOR",
        }
        assert "execution_work_ready" in {
            item["name"] for item in sa.inspect(connection).get_indexes("executions")
        }
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0003_workflow_trace")
    assert "work_class" not in {
        item["name"] for item in sa.inspect(database.engine).get_columns("executions")
    }
    database.migrate()
    database.check_schema()
