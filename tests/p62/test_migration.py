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


def test_0011_to_0012_preserves_agent_ledger_and_is_repeatable(model_agent_setup):
    database = model_agent_setup["database"]
    with database.engine.begin() as connection:
        command.downgrade(config(connection), "0011_agent_coordination")
        assert connection.scalar(sa.text("SELECT count(*) FROM agent_runs")) == 1
    database.migrate()
    database.migrate()
    database.check_schema()
    assert database_module.SCHEMA_VERSION == "0023_workflow_release_lifecycle"
    with database.engine.connect() as connection:
        names = set(sa.inspect(connection).get_table_names())
        assert {
            "agent_model_budgets",
            "agent_model_reservations",
            "agent_model_calls",
            "agent_model_observations",
            "model_provider_reconciliation",
        } <= names
        assert connection.scalar(sa.text("SELECT count(*) FROM agent_runs")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM agent_model_budgets")) == 0
