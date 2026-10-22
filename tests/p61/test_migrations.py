from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_agent_runtime.checkpoint import SCHEMA_VERSION, CheckpointDatabase
from platform_core.application.control import ControlService
from platform_infrastructure import database as database_module


def config(connection):
    value = Config()
    value.set_main_option(
        "script_location", str(Path(database_module.__file__).parent / "migrations")
    )
    value.attributes["connection"] = connection
    return value


def test_0010_to_0011_preserves_direct_task_and_is_repeatable(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "p61-upgrade-project", "upgrade")["project_id"]
    task = control.create_task(
        actor,
        project,
        "p61-upgrade-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Preserve pre-Agent task", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    with database.engine.begin() as connection:
        command.downgrade(config(connection), "0010_checker_runs")
        assert connection.scalar(sa.text("SELECT count(*) FROM tasks")) == 1
    database.migrate()
    database.migrate()
    database.check_schema()
    assert database_module.SCHEMA_VERSION == "0023_workflow_release_lifecycle"
    with database.engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert {
            "agent_runs",
            "agent_plan_revisions",
            "agent_decision_frames",
            "agent_proposals",
            "agent_questions",
            "agent_answers",
            "agent_events",
            "agent_inbox",
            "agent_outbox",
            "agent_wakeups",
            "agent_leases",
            "agent_context_manifests",
        } <= tables
        assert connection.scalar(sa.text("SELECT count(*) FROM agent_runs")) == 0
        assert control.task(actor, project, task["task_id"])["execution_mode"] == "DIRECT"


def checkpoint_url(postgres):
    source = postgres.url.replace("postgresql+psycopg://", "postgresql://")
    admin = source.rsplit("/", 1)[0] + "/postgres"
    target = source.rsplit("/", 1)[0] + "/v7_agent_checkpoint"
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute("CREATE DATABASE v7_agent_checkpoint")
    return target


def test_checkpoint_database_requires_explicit_repeatable_migration(postgres):
    url = checkpoint_url(postgres)
    checkpoint = CheckpointDatabase(url)
    with pytest.raises(RuntimeError, match="migration is required"):
        checkpoint.check_schema()
    checkpoint.migrate()
    checkpoint.migrate()
    checkpoint.check_schema()
    with psycopg.connect(url) as connection:
        version = connection.execute(
            "SELECT version FROM v7_agent_checkpoint_schema WHERE singleton"
        ).fetchone()[0]
        assert version == SCHEMA_VERSION
        assert connection.execute("SELECT to_regclass('public.checkpoints')").fetchone()[0]
