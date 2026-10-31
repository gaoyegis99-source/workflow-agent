import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_api.composition import PlatformComposition
from platform_core.application.control import ControlService
from platform_core.domain.control import Identity, RuleViolation
from platform_infrastructure import tables as t
from platform_infrastructure.database import SCHEMA_VERSION


def platform_metadata_object(obj, name, kind, reflected, compare_to):
    """Ignore separately migrated checkpoint tables in the shared database."""

    del reflected, compare_to
    table_name = name if kind == "table" else getattr(getattr(obj, "table", None), "name", None)
    return table_name is None or table_name in t.metadata.tables


def test_migration_is_repeatable_and_tokens_are_hashed(database, owner):
    database.migrate()
    database.check_schema()
    actor, raw = owner
    with database.engine.connect() as connection:
        assert connection.scalar(sa.text("SHOW server_version_num")).startswith("17")
        context = MigrationContext.configure(
            connection, opts={"include_object": platform_metadata_object}
        )
        assert not compare_metadata(context, t.metadata)
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


def test_external_subject_binding_is_unique_and_proxy_is_not_a_service_identity(database, owner):
    principal = database.create_principal("proxy user")
    binding = database.bind_subject(
        principal,
        "idp:proxy-user",
        reviewer="admin:alice",
        reason="approved trusted identity provider mapping",
    )
    proxy = database.authenticate_subject("idp:proxy-user")

    assert proxy.principal_id == principal
    assert proxy.token_id is None
    assert proxy.is_trusted_proxy
    assert binding["principal_id"] == principal
    with database.transaction() as uow:
        uow.access.authorize(proxy, None)
    with database.engine.connect() as connection:
        event = (
            connection.execute(
                sa.select(t.principal_identity_events).where(
                    t.principal_identity_events.c.event_id == binding["event_id"]
                )
            )
            .mappings()
            .one()
        )
    assert dict(event) | {"created_at": None} == {
        "event_id": binding["event_id"],
        "principal_id": principal,
        "previous_subject": None,
        "bound_subject": "idp:proxy-user",
        "reviewer": "admin:alice",
        "reason": "approved trusted identity provider mapping",
        "created_at": None,
    }
    another = database.create_principal("another")
    with pytest.raises(RuleViolation) as duplicate:
        database.bind_subject(
            another,
            "idp:proxy-user",
            reviewer="admin:alice",
            reason="duplicate mapping test",
        )
    assert duplicate.value.status == 409
    with pytest.raises(RuleViolation) as untyped:
        with database.transaction() as uow:
            uow.access.authorize(Identity(principal, None), None)
    assert untyped.value.status == 401


def test_me_supports_bearer_and_bound_trusted_proxy_identities(database, owner):
    actor, token = owner
    project = ControlService(database).create_project(actor, "me-project", "Current identity")
    database.bind_subject(
        actor.principal_id,
        "idp:owner",
        reviewer="admin:alice",
        reason="enable trusted proxy login",
    )

    with TestClient(create_app(database)) as client:
        bearer = client.get("/api/v1/me", headers={"Authorization": "Bearer " + token})
        assert bearer.status_code == 200
        assert bearer.json()["authentication_mode"] == "token"
        assert bearer.json()["projects"] == [{"project_id": project["project_id"], "role": "OWNER"}]

    composition = replace(
        PlatformComposition.for_test(),
        identity_mode="trusted-proxy",
        trusted_proxy_token="p" * 32,
    )
    with TestClient(create_app(database, composition=composition)) as client:
        response = client.get(
            "/api/v1/me",
            headers={
                "X-V7-Trusted-Proxy": "p" * 32,
                "X-Platform-Subject": "idp:owner",
                "X-Platform-Display-Name": "Owner from IdP",
                "X-Platform-Roles": "VIEWER",
            },
        )
        assert response.status_code == 200
        assert response.json()["authentication_mode"] == "trusted-proxy"
        assert response.json()["subject"] == "idp:owner"
        # The header's forged VIEWER value is ignored; database membership is OWNER.
        assert response.json()["projects"] == [
            {"project_id": project["project_id"], "role": "OWNER"}
        ]


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
