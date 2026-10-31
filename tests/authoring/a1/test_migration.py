from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.application.control import ControlService
from platform_infrastructure import database as database_module
from platform_infrastructure import tables
from platform_infrastructure.database import SCHEMA_VERSION


def test_authoring_schema_is_current_and_repeatable(database):
    assert SCHEMA_VERSION == "0026_worker_lane_leases"
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        names = set(sa.inspect(connection).get_table_names())
        assert tables.workflow_definitions.name in names
        assert tables.workflow_revisions.name in names


def test_upgrade_from_0018_preserves_existing_project(database, owner):
    actor, _ = owner
    project_id = ControlService(database).create_project(actor, "migration-project", "Existing")[
        "project_id"
    ]
    with database.engine.begin() as connection:
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(database_module.__file__).parent / "migrations"),
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0018_agent_acceptance_identity")
    database.migrate()
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(tables.projects)
                .where(tables.projects.c.project_id == project_id)
            )
            == 1
        )
