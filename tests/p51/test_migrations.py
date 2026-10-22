from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_data import database as data_database


def data_config(database):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(data_database.__file__).parent / "migrations")
    )
    config.attributes["connection"] = database
    return config


def test_data_0003_to_0004_preserves_existing_records(data_database):
    with data_database.engine.begin() as connection:
        command.downgrade(data_config(connection), "0003_spatial_import_runtime")
        connection.exec_driver_sql(
            """
            INSERT INTO uploads (upload_id, project_id, state, payload, generation, runtime)
            VALUES (
              'document-upgrade', 'project', 'QUEUED',
              '{"filename":"guide.md","format":"markdown","size":12,
                "digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'::jsonb,
              4, '{"fact":"preserved"}'::jsonb
            );
            INSERT INTO assets (asset_id, version_id, project_id, manifest, manifest_digest)
            VALUES (
              'old-asset', '1', 'project', '{"kind":"SPATIAL"}'::jsonb,
              'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            )
            """
        )
    data_database.migrate()
    data_database.migrate()
    data_database.check_schema()
    with data_database.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT state, generation, runtime FROM uploads "
                    "WHERE upload_id='document-upgrade'"
                )
            )
            .mappings()
            .one()
        )
        assert dict(row) == {
            "state": "QUEUED",
            "generation": 4,
            "runtime": {"fact": "preserved"},
        }
        assert connection.scalar(sa.text("SELECT count(*) FROM assets")) == 1
        indexes = {item["name"] for item in sa.inspect(connection).get_indexes("uploads")}
        assert "ix_uploads_project_format_state" in indexes


def test_knowledge_initial_migration_is_repeatable_and_complete(knowledge_database):
    knowledge_database.migrate()
    knowledge_database.migrate()
    knowledge_database.check_schema()
    expected = {
        "knowledge_imports",
        "knowledge_documents",
        "knowledge_versions",
        "evidence_units",
        "evidence_read_receipts",
        "knowledge_lifecycle_events",
        "knowledge_outbox",
        "knowledge_idempotency",
    }
    inspector = sa.inspect(knowledge_database.engine)
    assert expected <= set(inspector.get_table_names())
    with knowledge_database.engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'"))
            == 1
        )
