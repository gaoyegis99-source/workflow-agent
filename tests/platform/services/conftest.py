"""Real independent PostgreSQL databases; no V6 resources."""

import secrets
import time
import uuid

import pytest
import sqlalchemy as sa
from platform_data.database import Database as DataDatabase
from platform_execution.database import Database as ExecutionDatabase

from tests.platform.database.conftest import database, owner, postgres  # noqa: F401


def initialize_service_database(service_database, timeout=30):
    """Retry a disposable database through transient rootless port-forward loss."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            service_database.migrate()
            service_database.check_schema()
            return
        except sa.exc.OperationalError:
            service_database.engine.dispose()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


@pytest.fixture
def service_databases(postgres):  # noqa: F811
    root = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
    databases = []
    try:
        for cls in (DataDatabase, ExecutionDatabase):
            name = "p3_" + uuid.uuid4().hex
            password = secrets.token_hex(24)
            with root.connect() as c:
                c.exec_driver_sql(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
                c.exec_driver_sql(f"CREATE DATABASE {name} OWNER {name}")
                c.exec_driver_sql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
            url = (
                sa.engine.make_url(postgres.url)
                .set(database=name, username=name, password=password)
                .render_as_string(hide_password=False)
            )
            db = cls(url)
            databases.append((name, db))
            initialize_service_database(db)
        yield tuple(db for _, db in databases)
    finally:
        for name, db in reversed(databases):
            db.close()
            with root.connect() as c:
                c.exec_driver_sql(f"DROP DATABASE {name} WITH (FORCE)")
                c.exec_driver_sql(f"DROP ROLE {name}")
        root.dispose()


@pytest.fixture
def cluster(tmp_path, database, service_databases):  # noqa: F811
    from tests.platform.services.harness import Cluster

    cluster = Cluster(tmp_path, database, service_databases)
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
