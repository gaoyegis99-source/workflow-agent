from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_contracts.service import ServiceFault
from platform_execution import database as execution_database
from platform_execution.application.components import RemoteExecutionDriver
from platform_execution.implementations import REMOTE_SPATIAL_SUMMARY_CONTRACT
from platform_infrastructure import database as platform_database


def binding(endpoint="http://127.0.0.1:19080"):
    return {
        "service_binding_id": "peer",
        "protocol": "v7-remote-execution-v1",
        "endpoint": endpoint,
        "credential_ref": "peer-token",
        "targets": [
            {
                "operator_id": "spatial-vector-summary",
                "version_id": "1.0.0",
                "remote_implementation_id": "summary-remote-v1",
                "contract_digest": REMOTE_SPATIAL_SUMMARY_CONTRACT,
            }
        ],
        "limits": {"input_bytes": 1024, "output_bytes": 2048, "timeout_seconds": 30},
    }


def test_binding_revisions_are_immutable_and_switch_is_separate(database, owner):
    from platform_core.application.control import ControlService

    project = ControlService(database).create_project(owner[0], "create-registry", "Registry")
    first = database.service_binding_create(project["project_id"], binding())
    second = database.service_binding_revise(
        project["project_id"], "peer", binding("http://127.0.0.1:19081")
    )
    assert first["revision"] == 1 and second["revision"] == 2
    database.service_binding_enable(project["project_id"], "peer", False)
    with database.transaction() as uow:
        assert uow.service_bindings.get(project["project_id"], "peer", 1)["endpoint"].endswith(
            "19080"
        )
        assert uow.service_bindings.get(project["project_id"], "peer", 2)["enabled"] is False


def test_same_binding_identity_is_scoped_by_project(database, owner):
    from platform_core.application.control import ControlService

    service = ControlService(database)
    first = service.create_project(owner[0], "create-registry-a", "Registry A")
    second = service.create_project(owner[0], "create-registry-b", "Registry B")
    assert database.service_binding_create(first["project_id"], binding())["revision"] == 1
    assert database.service_binding_create(second["project_id"], binding())["revision"] == 1
    with database.engine.begin() as connection:
        connection.execute(
            sa.text("delete from service_binding_revisions where service_binding_id='peer'")
        )
        connection.execute(sa.text("delete from service_bindings where service_binding_id='peer'"))


def test_privileged_write_uses_strict_validation(database, owner):
    from platform_core.application.control import ControlService

    project = ControlService(database).create_project(owner[0], "strict-registry", "Strict")
    with pytest.raises(Exception) as caught:
        database.service_binding_create(
            project["project_id"], binding("http://user:password@127.0.0.1:19080")
        )
    assert getattr(caught.value, "code", None) == "MALFORMED_REQUEST"


def test_platform_0004_upgrade_preserves_existing_rows(database):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(platform_database.__file__).parent / "migrations")
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_workflows")
        before = connection.scalar(sa.text("select count(*) from principals"))
    database.migrate()
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        assert connection.scalar(sa.text("select count(*) from principals")) == before
        assert {"service_bindings", "service_binding_revisions"} <= set(
            sa.inspect(connection).get_table_names()
        )


def test_execution_0004_upgrade_adds_empty_remote_ledger(service_databases):
    database = service_databases[1]
    config = Config()
    config.set_main_option(
        "script_location", str(Path(execution_database.__file__).parent / "migrations")
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_execution_work_class")
    database.migrate()
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        assert connection.scalar(sa.text("select count(*) from remote_calls")) == 0
        assert connection.scalar(sa.text("select count(*) from remote_observations")) == 0


class Repo:
    def __init__(self, call):
        self.call = call

    def change_remote(self, row, **changes):
        self.call |= changes
        return True

    def observe_remote(self, row, event, **changes):
        self.call |= changes
        return True

    def remote(self, action):
        return self.call.copy()

    def change(self, row, **changes):
        row.update(changes)
        return True


def execution_row():
    return {
        "action_id": "action",
        "owner": "worker",
        "generation": 1,
        "executed": True,
        "state": "RUNNING",
        "runtime": {"remote": {}, "phase": "REMOTE_TRACKING"},
    }


def test_remote_observation_ignores_old_and_rejects_same_revision_conflict(tmp_path):
    fact = {
        "event_id": "evt",
        "action_id": "action",
        "remote_execution_id": "remote",
        "revision": 3,
        "state": "RUNNING",
        "executed": True,
        "outputs": None,
    }
    call = {
        "action_id": "action",
        "remote_execution_id": "remote",
        "remote_revision": 3,
        "remote_state": "RUNNING",
        "executed": True,
        "observation": fact,
    }
    driver = object.__new__(RemoteExecutionDriver)
    driver.repo = Repo(call)
    row = execution_row()
    old = fact | {"revision": 2, "state": "QUEUED"}
    assert driver.apply_remote_observation(row, call, old)["remote_revision"] == 3
    with pytest.raises(ServiceFault):
        driver.apply_remote_observation(row, call, fact | {"state": "FAILED"})


def test_remote_executed_fact_cannot_regress(tmp_path):
    fact = {
        "event_id": "evt",
        "action_id": "action",
        "remote_execution_id": "remote",
        "revision": 3,
        "state": "RUNNING",
        "executed": True,
        "outputs": None,
    }
    call = {
        "action_id": "action",
        "remote_execution_id": "remote",
        "remote_revision": 3,
        "remote_state": "RUNNING",
        "executed": True,
        "observation": fact,
    }
    driver = object.__new__(RemoteExecutionDriver)
    driver.repo = Repo(call)
    with pytest.raises(ServiceFault):
        driver.apply_remote_observation(
            execution_row(), call, fact | {"revision": 4, "executed": False}
        )
