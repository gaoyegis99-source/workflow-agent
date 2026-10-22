import json
import os
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from platform_agent_runtime.checkpoint import CheckpointDatabase
from platform_infrastructure.cold_backup import (
    QUIESCENCE_QUERIES,
    BackupConfiguration,
    create_backup,
    is_quiescent,
    quiescence,
    restore_backup,
    verified_manifest,
)


def url(database):
    return database.engine.url.render_as_string(hide_password=False)


def configuration(tmp_path, platform, data, execution, knowledge):
    CheckpointDatabase(url(platform)).migrate()
    trees = {}
    for name in ("data", "knowledge", "execution", "spatial"):
        root = tmp_path / "state" / name
        root.mkdir(parents=True)
        trees[name] = root
    files = {}
    for name in (
        "release-lock.json",
        "operator-catalog.json",
        "checker-catalog.json",
        "tool-catalog.json",
    ):
        path = tmp_path / "release" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": name, "version": "0.15.0"}), encoding="utf-8")
        files[name] = path
    marker = tmp_path / "control" / "maintenance"
    marker.parent.mkdir()
    marker.touch(mode=0o600)
    return BackupConfiguration(
        databases={
            "platform": url(platform),
            "data": url(data),
            "execution": url(execution),
            "knowledge": url(knowledge),
        },
        trees=trees,
        files=files,
        release_digest="sha256:" + "1" * 64,
        maintenance_file=marker,
        state_uid=os.getuid(),
        state_gid=os.getgid(),
    )


def seed(platform, data, execution, knowledge, config):
    principal = platform.create_principal("backup-owner")
    with data.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO assets(asset_id,version_id,project_id,manifest,manifest_digest) "
                "VALUES ('asset-p8','version-p8','project-p8','{}'::jsonb,:digest)"
            ),
            {"digest": "sha256:" + "2" * 64},
        )
    with execution.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO executions(action_id,execution_id,project_id,task_id,digest,payload,"
                "state,revision,executed,cancel_requested,attempt_id,runtime,work_class) VALUES "
                "('action-p8','execution-p8','project-p8','task-p8',:digest,'{}'::jsonb,"
                "'SUCCEEDED',3,true,false,'attempt-p8','{}'::jsonb,'NODE_EXECUTION')"
            ),
            {"digest": "3" * 64},
        )
    now = datetime.now(UTC)
    with knowledge.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO knowledge_documents(document_id,project_id,created_at) "
                "VALUES ('document-p8','project-p8',:created_at)"
            ),
            {"created_at": now},
        )
    (config.trees["data"] / "assets" / "asset-p8").mkdir(parents=True)
    (config.trees["data"] / "assets" / "asset-p8" / "content.bin").write_bytes(b"asset")
    (config.trees["knowledge"] / "document-p8.txt").write_text("evidence", encoding="utf-8")
    (config.trees["execution"] / "jobstores" / "execution-p8").mkdir(parents=True)
    (config.trees["execution"] / "jobstores" / "execution-p8" / "jobstore.db").write_bytes(
        b"jobstore"
    )
    (config.trees["execution"] / "jobstores" / "execution-p8" / "input-link").symlink_to(
        "/run/work/input/frozen-input"
    )
    (config.trees["spatial"] / "inspection-p8.json").write_text("{}", encoding="utf-8")
    return principal


def test_agent_audit_outbox_is_preserved_without_blocking_a_cold_snapshot():
    platform = QUIESCENCE_QUERIES["platform"]
    assert "agent_outbox" not in platform
    assert {"active_actions", "agent_advancing", "agent_tools", "agent_model_calls"} <= set(
        platform
    )


def test_four_database_and_state_tree_backup_restores_exact_identity(
    tmp_path, database, service_databases, knowledge_database
):
    data, execution = service_databases
    config = configuration(tmp_path, database, data, execution, knowledge_database)
    principal = seed(database, data, execution, knowledge_database, config)
    assert is_quiescent(quiescence(config))

    destination = tmp_path / "backups" / "cold-1"
    manifest = create_backup(config, destination)
    assert verified_manifest(destination, config.release_digest) == manifest
    assert set(manifest["databases"]) == {"platform", "data", "execution", "knowledge"}
    assert set(manifest["trees"]) == {"data", "knowledge", "execution", "spatial"}
    assert any(
        item["path"].endswith("jobstore.db") for item in manifest["trees"]["execution"]["files"]
    )
    link = next(
        item
        for item in manifest["trees"]["execution"]["files"]
        if item["path"].endswith("input-link")
    )
    assert link == {
        "path": "jobstores/execution-p8/input-link",
        "type": "SYMLINK",
        "target": "/run/work/input/frozen-input",
    }

    extra_principal = database.create_principal("after-backup")
    with data.engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM assets WHERE asset_id='asset-p8'"))
    with execution.engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM executions WHERE action_id='action-p8'"))
    with knowledge_database.engine.begin() as connection:
        connection.execute(
            sa.text("DELETE FROM knowledge_documents WHERE document_id='document-p8'")
        )
    (config.trees["data"] / "assets" / "asset-p8" / "content.bin").write_bytes(b"changed")
    (config.trees["execution"] / "not-in-backup").write_text("remove", encoding="utf-8")

    restored = restore_backup(config, destination, confirmed=True)
    assert restored["manifest_digest"] == manifest["manifest_digest"]
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM principals WHERE principal_id=:identity"),
                {"identity": principal},
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM principals WHERE principal_id=:identity"),
                {"identity": extra_principal},
            )
            == 0
        )
    with data.engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM assets WHERE asset_id='asset-p8'")) == 1
        )
    with execution.engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT execution_id,revision FROM executions WHERE action_id='action-p8'")
        ).one()
        assert tuple(row) == ("execution-p8", 3)
    with knowledge_database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM knowledge_documents WHERE document_id='document-p8'")
            )
            == 1
        )
    assert (config.trees["data"] / "assets" / "asset-p8" / "content.bin").read_bytes() == b"asset"
    assert not (config.trees["execution"] / "not-in-backup").exists()
    assert (
        config.trees["execution"] / "jobstores" / "execution-p8" / "jobstore.db"
    ).read_bytes() == b"jobstore"
    assert (
        os.readlink(config.trees["execution"] / "jobstores" / "execution-p8" / "input-link")
        == "/run/work/input/frozen-input"
    )


def test_backup_requires_barrier_and_rejects_unsettled_or_tampered_state(
    tmp_path, database, service_databases, knowledge_database
):
    data, execution = service_databases
    config = configuration(tmp_path, database, data, execution, knowledge_database)
    config.maintenance_file.unlink()
    with pytest.raises(RuntimeError, match="write barrier"):
        create_backup(config, tmp_path / "blocked")

    config.maintenance_file.touch(mode=0o600)
    with execution.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO executions(action_id,execution_id,project_id,task_id,digest,payload,"
                "state,revision,executed,cancel_requested,attempt_id,runtime,work_class) VALUES "
                "('active-action','active-execution','project','task',:digest,'{}'::jsonb,"
                "'RUNNING',1,true,false,'active-attempt','{}'::jsonb,'NODE_EXECUTION')"
            ),
            {"digest": "4" * 64},
        )
    with pytest.raises(RuntimeError, match="unsettled"):
        create_backup(config, tmp_path / "active")
    with execution.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE executions SET state='SUCCEEDED' WHERE action_id='active-action'")
        )
    destination = tmp_path / "complete"
    create_backup(config, destination)
    state_file = destination / "trees" / "data"
    tamper = state_file / "unexpected"
    tamper.write_text("not listed", encoding="utf-8")
    # Unlisted files do not influence restoration, while any listed byte is pinned.
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    listed = manifest["configuration"][0]["name"]
    (destination / "configuration" / listed).write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="configuration changed"):
        verified_manifest(destination, config.release_digest)


def test_restore_requires_confirmation_and_same_release(
    tmp_path, database, service_databases, knowledge_database
):
    data, execution = service_databases
    config = configuration(tmp_path, database, data, execution, knowledge_database)
    destination = tmp_path / "complete"
    create_backup(config, destination)
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        restore_backup(config, destination)
    different = BackupConfiguration(
        databases=config.databases,
        trees=config.trees,
        files=config.files,
        release_digest="sha256:" + "9" * 64,
        maintenance_file=config.maintenance_file,
        state_uid=os.getuid(),
        state_gid=os.getgid(),
    )
    with pytest.raises(RuntimeError, match="release differs"):
        restore_backup(different, destination, confirmed=True)


def test_restore_preflights_all_schemas_before_modifying_any_database(
    tmp_path, database, service_databases, knowledge_database
):
    data, execution = service_databases
    config = configuration(tmp_path, database, data, execution, knowledge_database)
    seed(database, data, execution, knowledge_database, config)
    destination = tmp_path / "complete"
    create_backup(config, destination)
    extra_principal = database.create_principal("must-survive-failed-preflight")
    with data.engine.begin() as connection:
        connection.execute(sa.text("UPDATE alembic_version SET version_num='invalid'"))
    with pytest.raises(RuntimeError, match="data database schema differs"):
        restore_backup(config, destination, confirmed=True)
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM principals WHERE principal_id=:identity"),
                {"identity": extra_principal},
            )
            == 1
        )
    with data.engine.begin() as connection:
        connection.execute(sa.text("UPDATE alembic_version SET version_num='0004_document_assets'"))
