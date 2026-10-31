from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.application.control import ControlService
from platform_infrastructure import database as platform_database


def config(connection):
    value = Config()
    value.set_main_option(
        "script_location", str(Path(platform_database.__file__).parent / "migrations")
    )
    value.attributes["connection"] = connection
    return value


def test_0006_to_0007_preserves_existing_platform_state(database, owner):  # noqa: F811
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "migration-project", "Migration")
    task = control.create_task(
        actor,
        project["project_id"],
        "migration-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Preserve me", "requirements": []},
            "budget": {"execution_count": 2, "max_in_flight": 1},
        },
    )
    with database.engine.begin() as connection:
        command.downgrade(config(connection), "0006_service_binding_scope")
        assert connection.scalar(sa.text("SELECT count(*) FROM tasks")) == 1
    database.migrate()
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT spec->>'goal' FROM task_specs WHERE task_id=:task"),
                {"task": task["task_id"]},
            )
            == "Preserve me"
        )
        assert {
            "analysis_revisions",
            "analysis_registrations",
            "claim_revisions",
            "policy_decisions",
            "provenance_relations",
        } <= set(sa.inspect(connection).get_table_names())
