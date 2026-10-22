import hashlib
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables as t
from platform_infrastructure.database import SCHEMA_VERSION


def test_migration_is_repeatable_and_tokens_are_hashed(database, owner):
    database.migrate()
    database.check_schema()
    actor, raw = owner
    with database.engine.connect() as connection:
        assert connection.scalar(sa.text("SHOW server_version_num")).startswith("17")
        assert not compare_metadata(MigrationContext.configure(connection), t.metadata)
        token = connection.execute(sa.select(t.tokens)).mappings().one()
        assert token["digest"] == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in str(dict(token))
    database.revoke_token(actor.token_id)
    with pytest.raises(RuleViolation, match="authentication"):
        database.authenticate(raw)


def test_expired_token_rejected(database, owner):
    actor, raw = owner
    with database.engine.begin() as connection:
        connection.execute(
            t.tokens.update()
            .where(t.tokens.c.token_id == actor.token_id)
            .values(expires_at=sa.func.now() - timedelta(seconds=1))
        )
    with pytest.raises(RuleViolation) as error:
        database.authenticate(raw)
    assert error.value.status == 401


def test_roles_project_isolation_and_last_owner(database, owner):
    actor, _ = owner
    with database.transaction() as uow:
        project = uow.new_id()
        uow.access.create_project(project, "private", actor)
    visitor = database.create_principal("visitor")
    visitor_actor = database.authenticate(database.issue_token(visitor)["token"])
    with pytest.raises(RuleViolation) as error, database.transaction() as uow:
        uow.access.authorize(visitor_actor, project)
    assert error.value.status == 404
    database.set_member(project, visitor, "VIEWER", actor=actor)
    with pytest.raises(RuleViolation) as error:
        database.set_member(project, visitor, "OWNER", actor=visitor_actor)
    assert error.value.status == 403
    with database.transaction() as uow:
        uow.access.authorize(visitor_actor, project)
    with pytest.raises(RuleViolation) as error, database.transaction() as uow:
        uow.access.authorize(visitor_actor, project, "EDITOR")
    assert error.value.status == 403
    with pytest.raises(RuleViolation, match="last owner"):
        database.set_member(project, actor.principal_id, "REMOVE", actor=actor)
    database.set_member(project, visitor, "OWNER", actor=actor)
    database.set_member(project, actor.principal_id, "REMOVE", actor=actor)
    with pytest.raises(RuleViolation), database.transaction() as uow:
        uow.access.authorize(actor, project)


def test_transaction_rolls_back_partial_writes(database, owner):
    actor, _ = owner
    with pytest.raises(RuntimeError), database.transaction() as uow:
        uow.access.create_project("uncommitted", "no commit", actor)
        raise RuntimeError("process failed before commit")
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.projects)) == 0


def test_startup_refuses_schema_mismatch_without_running_migrations(database):
    with database.engine.begin() as connection:
        connection.execute(sa.text("UPDATE alembic_version SET version_num='wrong'"))
    try:
        with (
            pytest.raises(RuntimeError, match="migration version"),
            TestClient(create_app(database)),
        ):
            pass
        with database.engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "wrong"
    finally:
        with database.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num=:version"),
                {"version": SCHEMA_VERSION},
            )
