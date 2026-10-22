from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.application.authoring import AuthoringService
from platform_core.application.control import ControlService
from platform_infrastructure import database as database_module
from platform_infrastructure import tables
from platform_infrastructure.database import SCHEMA_VERSION


def test_a2_schema_is_current_and_repeatable(database):
    assert SCHEMA_VERSION == "0023_workflow_release_lifecycle"
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        names = set(sa.inspect(connection).get_table_names())
        assert tables.authoring_validations.name in names
        columns = {
            item["name"]
            for item in sa.inspect(connection).get_columns(tables.authoring_validations.name)
        }
        assert {"context_digest", "generation", "lease_until", "issues"} <= columns


def test_upgrade_from_a1_preserves_draft(database, owner):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "a2-upgrade", "Upgrade")
    definition = AuthoringService(database).create(
        actor,
        project["project_id"],
        "create-upgrade-draft",
        {
            "expected_revision": 0,
            "stable_key": "existing-draft",
            "metadata": {"title": "Existing"},
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
        command.downgrade(config, "0019_authoring_drafts")
    database.migrate()
    database.check_schema()
    restored = AuthoringService(database).definition(
        actor, project["project_id"], definition["definition_id"]
    )
    assert restored["head"]["source_digest"] == definition["head"]["source_digest"]
