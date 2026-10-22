"""Explicit administration for the independent LangGraph checkpoint database."""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver

SCHEMA_VERSION = "0001_langgraph_checkpoint"


def psycopg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        return url
    raise ValueError("A PostgreSQL psycopg URL is required")


class CheckpointDatabase:
    def __init__(self, url: str):
        self.url = psycopg_url(url)

    def migrate(self) -> None:
        # LangGraph owns its checkpoint tables. setup() is invoked only by this
        # explicit administration command, never by worker startup.
        with PostgresSaver.from_conn_string(self.url) as saver:
            saver.setup()
        with psycopg.connect(self.url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(7700061)")
                try:
                    cursor.execute(
                        "CREATE TABLE IF NOT EXISTS v7_agent_checkpoint_schema "
                        "(singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
                        "version text NOT NULL)"
                    )
                    cursor.execute(
                        "INSERT INTO v7_agent_checkpoint_schema(singleton, version) "
                        "VALUES (true, %s) ON CONFLICT (singleton) "
                        "DO UPDATE SET version = EXCLUDED.version",
                        (SCHEMA_VERSION,),
                    )
                finally:
                    cursor.execute("SELECT pg_advisory_unlock(7700061)")

    def check_schema(self) -> None:
        with psycopg.connect(self.url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.v7_agent_checkpoint_schema')")
            if cursor.fetchone()[0] is None:
                raise RuntimeError("Agent checkpoint migration is required")
            cursor.execute("SELECT version FROM v7_agent_checkpoint_schema WHERE singleton")
            row = cursor.fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise RuntimeError("Agent checkpoint schema version does not match")
            cursor.execute("SELECT to_regclass('public.checkpoints')")
            if cursor.fetchone()[0] is None:
                raise RuntimeError("LangGraph checkpoint tables are missing")

    @contextmanager
    def saver(self):
        self.check_schema()
        with PostgresSaver.from_conn_string(self.url) as saver:
            yield saver
