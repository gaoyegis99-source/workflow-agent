from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_infrastructure import database as database_module


def config(connection):
    value = Config()
    value.set_main_option(
        "script_location", str(Path(database_module.__file__).parent / "migrations")
    )
    value.attributes["connection"] = connection
    return value


def test_0012_to_0013_adds_read_ledger_and_is_repeatable(model_agent_setup):
    database = model_agent_setup["database"]
    with database.engine.begin() as connection:
        command.downgrade(config(connection), "0012_agent_model_calls")
        assert connection.scalar(sa.text("SELECT count(*) FROM agent_runs")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM agent_model_budgets")) == 1
    assert database_module.SCHEMA_VERSION == "0023_workflow_release_lifecycle"
    database.migrate()
    database.migrate()
    with database.engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert version == "0023_workflow_release_lifecycle"
    assert {"agent_tool_requests", "agent_tool_observations"} <= tables
