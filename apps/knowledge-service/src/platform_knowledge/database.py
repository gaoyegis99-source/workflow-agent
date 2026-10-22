"""Knowledge-owned PostgreSQL connection and explicit migrations."""

import argparse
import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

SCHEMA_VERSION = "0002_lifecycle_outbox"


class Database:
    def __init__(self, url):
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL psycopg URL required")
        self.engine = sa.create_engine(
            url, pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 3}
        )

    def migrate(self):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            connection.execute(sa.text("SELECT pg_advisory_xact_lock(770005)"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def check_schema(self):
        with self.engine.connect() as connection:
            current = (
                connection.execute(sa.text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
        if current != [SCHEMA_VERSION]:
            raise RuntimeError("Knowledge database migration version mismatch")

    def close(self):
        self.engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "check-schema"])
    args = parser.parse_args()
    database = Database(os.environ["V7_KNOWLEDGE_DATABASE_URL"])
    try:
        database.migrate() if args.command == "migrate" else database.check_schema()
    finally:
        database.close()
