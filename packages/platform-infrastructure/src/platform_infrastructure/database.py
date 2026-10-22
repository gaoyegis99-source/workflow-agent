import hashlib
import os
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.domain.control import Identity, require
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from platform_infrastructure import tables as t
from platform_infrastructure.repositories import (
    AccessRepository,
    ActionRepository,
    AgentRepository,
    AuthoringRepository,
    BudgetRepository,
    DeliveryRepository,
    ResearchRepository,
    ScienceRepository,
    ServiceBindingRepository,
    TaskRepository,
    WorkflowRepository,
    one,
)

SCHEMA_VERSION = "0023_workflow_release_lifecycle"
StorageError = SQLAlchemyError


class UnitOfWork:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.connection = self.engine.connect()
        self.tx = self.connection.begin()
        self.access = AccessRepository(self.connection)
        self.tasks = TaskRepository(self.connection)
        self.budgets = BudgetRepository(self.connection)
        self.actions = ActionRepository(self.connection)
        self.delivery = DeliveryRepository(self.connection)
        self.science = ScienceRepository(self.connection)
        self.research = ResearchRepository(self.connection)
        self.workflows = WorkflowRepository(self.connection)
        self.authoring = AuthoringRepository(self.connection)
        self.service_bindings = ServiceBindingRepository(self.connection)
        self.agents = AgentRepository(self.connection)
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.tx.commit() if kind is None else self.tx.rollback()
        finally:
            self.connection.close()

    def now(self):
        return self.connection.scalar(sa.select(sa.func.clock_timestamp()))

    def new_id(self):
        return uuid4().hex


class Database:
    def __init__(self, url: str):
        require(
            url.startswith("postgresql+psycopg://"),
            "MALFORMED_REQUEST",
            "PostgreSQL psycopg URL required",
            400,
        )
        self.engine = sa.create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=20,
            connect_args={"connect_timeout": 3},
            hide_parameters=True,
        )

    @classmethod
    def from_environment(cls):
        url = os.environ.get("V7_DATABASE_URL")
        if not url:
            raise RuntimeError("V7_DATABASE_URL is required")
        return cls(url)

    def transaction(self):
        return UnitOfWork(self.engine)

    def close(self):
        self.engine.dispose()

    def migrate(self):
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            connection.execute(sa.text("SELECT pg_advisory_xact_lock(770002)"))
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")

    def check_schema(self):
        with self.engine.connect() as connection:
            current = (
                connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
        if current != [SCHEMA_VERSION]:
            raise RuntimeError("Database migration version does not match this application")

    def authenticate(self, raw: str):
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.engine.connect() as connection:
            row = one(
                connection, sa.select(t.tokens).where(t.tokens.c.digest == digest), required=False
            )
        require(row is not None, "AUTHENTICATION_REQUIRED", "Valid authentication is required", 401)
        actor = Identity(row["principal_id"], row["token_id"])
        with self.transaction() as uow:
            uow.access.authorize(actor, None)
        return actor

    def create_principal(self, name: str):
        require(
            0 < len(name) <= 200, "MALFORMED_REQUEST", "Name must contain 1 to 200 characters", 400
        )
        principal_id = uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(t.principals.insert().values(principal_id=principal_id, name=name))
        return principal_id

    def issue_token(self, principal_id: str, ttl_seconds: int = 2592000):
        require(1 <= ttl_seconds <= 31536000, "MALFORMED_REQUEST", "Invalid token lifetime", 400)
        token_id, raw = uuid4().hex, "v7_" + secrets.token_urlsafe(32)
        with self.engine.begin() as connection:
            one(
                connection,
                sa.select(t.principals).where(t.principals.c.principal_id == principal_id),
            )
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            connection.execute(
                t.tokens.insert().values(
                    token_id=token_id,
                    principal_id=principal_id,
                    digest=hashlib.sha256(raw.encode()).hexdigest(),
                    expires_at=now + timedelta(seconds=ttl_seconds),
                    revoked=False,
                )
            )
        return {"token_id": token_id, "token": raw}

    def revoke_token(self, token_id: str):
        with self.engine.begin() as connection:
            result = connection.execute(
                t.tokens.update().where(t.tokens.c.token_id == token_id).values(revoked=True)
            )
            require(result.rowcount == 1, "RESOURCE_NOT_FOUND", "Token not found", 404)

    def set_member(self, project_id: str, principal_id: str, role: str, *, actor: Identity):
        require(
            role in {"OWNER", "EDITOR", "VIEWER", "REMOVE"},
            "MALFORMED_REQUEST",
            "Invalid role",
            400,
        )
        with self.engine.begin() as connection:
            one(
                connection,
                sa.select(t.projects)
                .where(t.projects.c.project_id == project_id)
                # Serialize membership edits without blocking FK KEY SHARE locks
                # held by business writes that already authorized a member.
                .with_for_update(key_share=True),
            )
            AccessRepository(connection).authorize(actor, project_id, "OWNER")
            existing = one(
                connection,
                sa.select(t.members)
                .where(
                    t.members.c.project_id == project_id, t.members.c.principal_id == principal_id
                )
                .with_for_update(),
                required=False,
            )
            if existing and existing["role"] == "OWNER" and role != "OWNER":
                owners = connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(t.members)
                    .where(t.members.c.project_id == project_id, t.members.c.role == "OWNER")
                )
                require(owners > 1, "PRECONDITION_UNRESOLVED", "The last owner cannot be removed")
            if role == "REMOVE":
                connection.execute(
                    t.members.delete().where(
                        t.members.c.project_id == project_id,
                        t.members.c.principal_id == principal_id,
                    )
                )
            else:
                one(
                    connection,
                    sa.select(t.principals).where(t.principals.c.principal_id == principal_id),
                )
                connection.execute(
                    insert(t.members)
                    .values(project_id=project_id, principal_id=principal_id, role=role)
                    .on_conflict_do_update(
                        index_elements=["project_id", "principal_id"], set_={"role": role}
                    )
                )

    def service_binding_create(self, project_id, request):
        from platform_core.domain.remote import remote_binding_digest, validate_remote_binding

        value = validate_remote_binding(request)
        binding_digest = remote_binding_digest(value)
        with self.transaction() as uow:
            return uow.service_bindings.add(project_id, value, binding_digest)

    def service_binding_revise(self, project_id, identity, request):
        from platform_core.domain.remote import remote_binding_digest, validate_remote_binding

        value = validate_remote_binding(request)
        require(
            value["service_binding_id"] == identity, "INVALID_BINDING", "Binding identity changed"
        )
        binding_digest = remote_binding_digest(value)
        with self.transaction() as uow:
            return uow.service_bindings.revise(project_id, identity, value, binding_digest)

    def service_binding_enable(self, project_id, identity, enabled):
        with self.transaction() as uow:
            uow.service_bindings.set_enabled(project_id, identity, enabled)
            return uow.service_bindings.get(project_id, identity)
