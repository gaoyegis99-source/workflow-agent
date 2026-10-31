from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.application.control import ControlService
from platform_infrastructure import database as database_module


def config(connection):
    value = Config()
    value.set_main_option(
        "script_location", str(Path(database_module.__file__).parent / "migrations")
    )
    value.attributes["connection"] = connection
    return value


def test_0010_checker_run_schema_survives_current_migration(database):
    database.migrate()
    database.check_schema()
    assert database_module.SCHEMA_VERSION == "0026_worker_lane_leases"
    with database.engine.connect() as connection:
        inspector = sa.inspect(connection)
        assert {"acceptance_check_envelopes", "acceptance_check_runs"} <= set(
            inspector.get_table_names()
        )
        acceptance = {value["name"]: value for value in inspector.get_columns("acceptances")}
        assert acceptance["action_id"]["nullable"] is True
        coverage = {value["name"] for value in inspector.get_columns("acceptance_coverage_items")}
        assert {
            "coverage_status",
            "capability_id",
            "capability_version",
            "capability_digest",
            "check_run_id",
        } <= coverage


def test_0009_to_0010_preserves_existing_task_and_is_repeatable(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "r2-upgrade-project", "R2 upgrade")["project_id"]
    task = control.create_task(
        actor,
        project,
        "r2-upgrade-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Preserve R1 state", "requirements": []},
            "budget": {"execution_count": 2, "max_in_flight": 1},
        },
    )
    with database.engine.begin() as connection:
        command.downgrade(config(connection), "0009_output_identity")
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
            == "Preserve R1 state"
        )
