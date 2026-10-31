import secrets
import uuid

import pytest
import sqlalchemy as sa
from platform_data.database import Database as DataDatabase
from platform_knowledge.database import Database as KnowledgeDatabase

from tests.platform.database.conftest import postgres  # noqa: F401


def isolated_database(postgres, cls, prefix):  # noqa: F811
    root = sa.create_engine(postgres.url, isolation_level="AUTOCOMMIT")
    name = prefix + "_" + uuid.uuid4().hex
    password = secrets.token_hex(24)
    with root.connect() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
        connection.exec_driver_sql(f"CREATE DATABASE {name} OWNER {name}")
        connection.exec_driver_sql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
    url = (
        sa.engine.make_url(postgres.url)
        .set(database=name, username=name, password=password)
        .render_as_string(hide_password=False)
    )
    database = cls(url)
    database.migrate()
    database.check_schema()
    return root, name, database


@pytest.fixture
def data_database(postgres):  # noqa: F811
    root, name, database = isolated_database(postgres, DataDatabase, "p51_data")
    try:
        yield database
    finally:
        database.close()
        with root.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE {name} WITH (FORCE)")
            connection.exec_driver_sql(f"DROP ROLE {name}")
        root.dispose()


@pytest.fixture
def knowledge_database(postgres):  # noqa: F811
    root, name, database = isolated_database(postgres, KnowledgeDatabase, "p51_knowledge")
    try:
        yield database
    finally:
        database.close()
        with root.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE {name} WITH (FORCE)")
            connection.exec_driver_sql(f"DROP ROLE {name}")
        root.dispose()
