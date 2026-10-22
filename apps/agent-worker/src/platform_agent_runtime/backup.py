"""Consistent binary backups for the Platform Agent ledger and runtime checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import psycopg
from platform_infrastructure import tables
from platform_infrastructure.database import SCHEMA_VERSION as PLATFORM_SCHEMA
from psycopg import sql

from platform_agent_runtime.checkpoint import SCHEMA_VERSION as CHECKPOINT_SCHEMA
from platform_agent_runtime.checkpoint import psycopg_url


def _table_names(kind: str, connection) -> list[str]:
    if kind == "platform":
        return [value.name for value in tables.metadata.sorted_tables]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' "
            "AND (table_name LIKE 'checkpoint%%' OR table_name LIKE 'checkpoint_%%') "
            "ORDER BY table_name"
        )
        found = [row[0] for row in cursor.fetchall()]
    preferred = ["checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"]
    return [name for name in preferred if name in found] + [
        name for name in found if name not in preferred
    ]


def _schema_version(kind: str, connection) -> str:
    query = (
        "SELECT version_num FROM alembic_version"
        if kind == "platform"
        else "SELECT version FROM v7_agent_checkpoint_schema WHERE singleton"
    )
    with connection.cursor() as cursor:
        cursor.execute(query)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"{kind} schema marker is missing")
    return row[0]


def _copy_out(connection, table: str, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with destination.open("wb") as stream, connection.cursor() as cursor:
        with cursor.copy(
            sql.SQL("COPY {} TO STDOUT (FORMAT BINARY)").format(sql.Identifier(table))
        ) as copy:
            for chunk in copy:
                data = bytes(chunk)
                stream.write(data)
                digest.update(data)
    destination.chmod(0o600)
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        count = cursor.fetchone()[0]
    return count, "sha256:" + digest.hexdigest()


def _snapshot(kind: str, url: str, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    with psycopg.connect(psycopg_url(url)) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        version = _schema_version(kind, connection)
        expected = PLATFORM_SCHEMA if kind == "platform" else CHECKPOINT_SCHEMA
        if version != expected:
            raise RuntimeError(f"{kind} schema version differs from this release")
        records = []
        for index, table in enumerate(_table_names(kind, connection)):
            filename = f"{index:04d}-{table}.copy"
            count, digest = _copy_out(connection, table, directory / filename)
            records.append({"table": table, "filename": filename, "rows": count, "digest": digest})
    return {"kind": kind, "schema_version": version, "tables": records}


def create_backup(platform_url: str, checkpoint_url: str, destination: str | Path) -> dict:
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    manifest = {
        "schema_version": 1,
        "platform": _snapshot("platform", platform_url, root / "platform"),
        "checkpoint": _snapshot("checkpoint", checkpoint_url, root / "checkpoint"),
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["manifest_digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return manifest


def _verified_manifest(root: Path) -> dict:
    value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    supplied = value.pop("manifest_digest")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if supplied != "sha256:" + hashlib.sha256(payload).hexdigest():
        raise RuntimeError("Backup manifest digest differs")
    value["manifest_digest"] = supplied
    for section in ("platform", "checkpoint"):
        for record in value[section]["tables"]:
            content = (root / section / record["filename"]).read_bytes()
            if "sha256:" + hashlib.sha256(content).hexdigest() != record["digest"]:
                raise RuntimeError(f"Backup content digest differs for {record['table']}")
    return value


def _restore(kind: str, url: str, root: Path, section: dict) -> None:
    expected = PLATFORM_SCHEMA if kind == "platform" else CHECKPOINT_SCHEMA
    if section["schema_version"] != expected:
        raise RuntimeError(f"{kind} backup schema is not compatible with this release")
    with psycopg.connect(psycopg_url(url)) as connection:
        if _schema_version(kind, connection) != expected:
            raise RuntimeError(f"{kind} target schema is not compatible with this release")
        records = section["tables"]
        if records:
            identifiers = sql.SQL(", ").join(sql.Identifier(item["table"]) for item in records)
            connection.execute(sql.SQL("TRUNCATE {} CASCADE").format(identifiers))
        for record in records:
            statement = sql.SQL("COPY {} FROM STDIN (FORMAT BINARY)").format(
                sql.Identifier(record["table"])
            )
            with (root / kind / record["filename"]).open("rb") as stream:
                with connection.cursor().copy(statement) as copy:
                    while chunk := stream.read(1024 * 1024):
                        copy.write(chunk)


def restore_backup(
    platform_url: str,
    checkpoint_url: str,
    source: str | Path,
    *,
    confirmed: bool = False,
) -> dict:
    if not confirmed:
        raise RuntimeError("Restore requires explicit confirmation")
    root = Path(source)
    manifest = _verified_manifest(root)
    _restore("platform", platform_url, root, manifest["platform"])
    _restore("checkpoint", checkpoint_url, root, manifest["checkpoint"])
    return manifest


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up or restore the P6 Agent stores")
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("directory")
    parser.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args()
    platform_url = _required("V7_DATABASE_URL")
    checkpoint_url = _required("V7_AGENT_CHECKPOINT_DATABASE_URL")
    if args.command == "backup":
        value = create_backup(platform_url, checkpoint_url, args.directory)
    else:
        value = restore_backup(
            platform_url,
            checkpoint_url,
            args.directory,
            confirmed=args.confirm_restore,
        )
    print(json.dumps({"status": "ok", "manifest_digest": value["manifest_digest"]}))
