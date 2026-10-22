from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.application.control import ControlService
from platform_core.domain.control import RuleViolation
from platform_infrastructure import database as database_module


def config(connection):
    value = Config()
    value.set_main_option(
        "script_location", str(Path(database_module.__file__).parent / "migrations")
    )
    value.attributes["connection"] = connection
    return value


def test_0008_upgrade_marks_existing_deliverables_legacy_and_is_repeatable(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "upgrade-project", "Upgrade")["project_id"]
    task = control.create_task(
        actor,
        project,
        "upgrade-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Historical", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    with database.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO deliverables"
                "(deliverable_id,project_id,task_id,spec_revision,digest,payload,identity_mode) "
                "VALUES ('old-deliverable',:project,:task,1,'old-digest','{}'::jsonb,'EXACT')"
            ),
            {"project": project, "task": task["task_id"]},
        )
        command.downgrade(config(connection), "0008_open_acceptance")
    database.migrate()
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT identity_mode FROM deliverables WHERE deliverable_id='old-deliverable'"
                )
            )
            == "LEGACY"
        )
        columns = {value["name"] for value in sa.inspect(connection).get_columns("acceptances")}
        assert "analysis_registration_id" in columns


def test_new_verified_task_rejects_legacy_output_identity(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "strict-project", "Strict")["project_id"]
    with pytest.raises(RuleViolation) as error:
        control.create_task(
            actor,
            project,
            "strict-task",
            {
                "execution_mode": "DIRECT",
                "spec": {
                    "goal": "Legacy verified",
                    "requirements": [],
                    "acceptance_spec": {
                        "profile": "open-science-v1",
                        "delivery_class": "VERIFIED",
                        "requirements": [],
                        "required_outputs": [{"output_id": "result", "necessary": True}],
                        "allow_manual_review": False,
                        "max_check_executions": 1,
                    },
                },
                "budget": {"execution_count": 1, "max_in_flight": 1},
            },
        )
    assert error.value.code == "MALFORMED_REQUEST"
