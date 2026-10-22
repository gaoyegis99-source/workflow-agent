from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.application.authoring import AuthoringService
from platform_core.application.control import ControlService
from platform_infrastructure import database as database_module
from platform_infrastructure import tables
from platform_infrastructure.database import SCHEMA_VERSION


def test_a4_schema_is_current_and_repeatable(database):
    assert SCHEMA_VERSION == "0023_workflow_release_lifecycle"
    database.migrate()
    database.check_schema()
    names = set(sa.inspect(database.engine).get_table_names())
    assert {
        tables.operator_proposals.name,
        tables.operator_proposal_revisions.name,
        tables.operator_review_events.name,
        tables.operator_deployment_receipts.name,
    } <= names


def test_upgrade_from_0021_preserves_a3_definition(database, owner):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "a4-upgrade", "Upgrade")
    definition = AuthoringService(database).create(
        actor,
        project["project_id"],
        "definition",
        {
            "expected_revision": 0,
            "stable_key": "historical-a3",
            "metadata": {"title": "Historical"},
            "layout": {},
        },
    )
    with database.engine.begin() as connection:
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(database_module.__file__).parent / "migrations"),
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0022_operator_proposals")
    database.migrate()
    assert (
        AuthoringService(database).definition(
            actor, project["project_id"], definition["definition_id"]
        )["stable_key"]
        == "historical-a3"
    )
