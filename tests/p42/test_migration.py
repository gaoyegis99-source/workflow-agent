from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_data import database as data_database


def test_0002_upgrade_preserves_assets_publications_and_recoverable_upload(service_databases):
    database = service_databases[0]
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(data_database.__file__).parent / "migrations"),
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_publication_groups")
        connection.exec_driver_sql(
            """
                INSERT INTO uploads (
                  upload_id, project_id, state, payload, owner, generation, lease_until
                ) VALUES (
                  'spatial-upgrade', 'project', 'IMPORTING',
                  '{"filename":"points.gpkg","format":"geopackage","size":10,
                    "digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'::jsonb,
                  'old-worker', 7, now() + interval '1 minute'
                );
                INSERT INTO assets (
                  asset_id, version_id, project_id, manifest, manifest_digest
                ) VALUES (
                  'existing-asset', '1', 'project', '{"kind":"OBSERVATIONS"}'::jsonb,
                  'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
                );
                INSERT INTO publications (
                  execution_id, project_id, state, generation, payload
                ) VALUES (
                  'existing-publication', 'project', 'COMMITTED', 3,
                  '{"fact":"preserved"}'::jsonb
                )
                """
        )

    database.migrate()
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        upload = (
            connection.execute(
                sa.text(
                    "SELECT state, payload, owner, generation, runtime FROM uploads "
                    "WHERE upload_id='spatial-upgrade'"
                )
            )
            .mappings()
            .one()
        )
        assert dict(upload) == {
            "state": "IMPORTING",
            "payload": {
                "filename": "points.gpkg",
                "format": "geopackage",
                "size": 10,
                "digest": "sha256:" + "a" * 64,
            },
            "owner": "old-worker",
            "generation": 7,
            "runtime": {},
        }
        assert (
            connection.scalar(
                sa.text("SELECT manifest->>'kind' FROM assets WHERE asset_id='existing-asset'")
            )
            == "OBSERVATIONS"
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT payload->>'fact' FROM publications "
                    "WHERE execution_id='existing-publication'"
                )
            )
            == "preserved"
        )

    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_publication_groups")
    columns = sa.inspect(database.engine).get_columns("uploads")
    assert "runtime" not in {column["name"] for column in columns}
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT generation FROM uploads WHERE upload_id='spatial-upgrade'")
            )
            == 7
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM assets")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM publications")) == 1
    database.migrate()
    database.check_schema()
