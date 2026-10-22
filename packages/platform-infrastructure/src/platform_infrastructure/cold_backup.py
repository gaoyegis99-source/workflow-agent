"""Release-pinned cold backup and restore for all V7 service-owned state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
from platform_contracts.release import file_digest, load_release_lock
from psycopg import sql

BACKUP_SCHEMA = 2
EXPECTED_SCHEMAS = {
    "platform": {"alembic": "0023_workflow_release_lifecycle"},
    "data": {"alembic": "0004_document_assets"},
    "execution": {"alembic": "0006_remote_reliability"},
    "knowledge": {"alembic": "0002_lifecycle_outbox"},
}
DATABASE_ENV = {
    "platform": "V7_DATABASE_URL",
    "data": "V7_DATA_DATABASE_URL",
    "execution": "V7_EXECUTION_DATABASE_URL",
    "knowledge": "V7_KNOWLEDGE_DATABASE_URL",
}
TREE_ENV = {
    "data": "V7_DATA_STORAGE",
    "knowledge": "V7_KNOWLEDGE_STORAGE",
    "execution": "V7_EXECUTION_WORKDIR",
    "spatial": "V7_DATA_SPATIAL_WORK_ROOT",
}
CONFIG_ENV = {
    "release-lock.json": "V7_RELEASE_LOCK",
    "operator-catalog.json": "V7_OPERATOR_CATALOG",
    "checker-catalog.json": "V7_CHECKER_CATALOG",
    "tool-catalog.json": "V7_AGENT_TOOL_CATALOG",
}
MARKER_TABLES = {"alembic_version", "v7_agent_checkpoint_schema"}
QUIESCENCE_QUERIES = {
    "platform": {
        "outbox": "SELECT count(*) FROM outbox WHERE NOT done",
        "active_actions": "SELECT count(*) FROM reservations WHERE NOT stopped",
        "workflow_packages": (
            "SELECT count(*) FROM workflow_packages WHERE status IN ('QUEUED','VALIDATING')"
        ),
        "authoring_validations": (
            "SELECT count(*) FROM authoring_validations WHERE status IN ('QUEUED','RUNNING')"
        ),
        "workflow_trials": "SELECT count(*) FROM workflow_trials WHERE case_verdict='PENDING'",
        # agent_outbox is an append-only audit/event history in this release.
        # It has no external delivery consumer, so an unfinished row is durable
        # state to preserve rather than an in-flight side effect to drain.
        "agent_advancing": "SELECT count(*) FROM agent_runs WHERE status='ADVANCING'",
        "agent_tools": (
            "SELECT count(*) FROM agent_tool_requests WHERE status IN ('PENDING','RUNNING')"
        ),
        "agent_model_calls": (
            "SELECT count(*) FROM agent_model_reservations "
            "WHERE status IN ('ACTIVE','UNKNOWN_USAGE')"
        ),
    },
    "data": {
        "imports": "SELECT count(*) FROM uploads WHERE state IN ('QUEUED','IMPORTING')",
        "publications": (
            "SELECT count(*) FROM publications WHERE state NOT IN ('COMMITTED','ABORTED')"
        ),
    },
    "execution": {
        "executions": (
            "SELECT count(*) FROM executions "
            "WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED','REJECTED','SEALED')"
        )
    },
    "knowledge": {
        "imports": ("SELECT count(*) FROM knowledge_imports WHERE state IN ('QUEUED','IMPORTING')"),
        "outbox": "SELECT count(*) FROM knowledge_outbox WHERE NOT done",
    },
}


@dataclass(frozen=True)
class BackupConfiguration:
    databases: dict[str, str]
    trees: dict[str, Path]
    files: dict[str, Path]
    release_digest: str
    maintenance_file: Path
    park_manifest: Path | None = None
    state_uid: int = 65532
    state_gid: int = 65532


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def psycopg_url(value: str) -> str:
    prefix = "postgresql+psycopg://"
    if not value.startswith(prefix):
        raise ValueError("PostgreSQL psycopg URL required")
    return "postgresql://" + value[len(prefix) :]


def configuration_from_environment() -> BackupConfiguration:
    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"{name} is required")
        return value

    release_digest = required("V7_RELEASE_DIGEST")
    files = {name: Path(required(variable)) for name, variable in CONFIG_ENV.items()}
    release = load_release_lock(files["release-lock.json"], release_digest)
    if release["release_digest"] != release_digest:
        raise ValueError("Release identity changed")
    return BackupConfiguration(
        databases={name: required(variable) for name, variable in DATABASE_ENV.items()},
        trees={name: Path(required(variable)) for name, variable in TREE_ENV.items()},
        files=files,
        release_digest=release_digest,
        maintenance_file=Path(
            os.environ.get("V7_MAINTENANCE_FILE", "/var/lib/v7-control/maintenance")
        ),
        park_manifest=(
            Path(os.environ["V7_PARK_MANIFEST"]) if os.environ.get("V7_PARK_MANIFEST") else None
        ),
        state_uid=int(os.environ.get("V7_STATE_UID", "65532")),
        state_gid=int(os.environ.get("V7_STATE_GID", "65532")),
    )


def _schema_facts(connection) -> dict[str, str]:
    facts: dict[str, str] = {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.alembic_version')")
        if cursor.fetchone()[0] is not None:
            cursor.execute("SELECT version_num FROM alembic_version")
            row = cursor.fetchone()
            if row:
                facts["alembic"] = row[0]
        cursor.execute("SELECT to_regclass('public.v7_agent_checkpoint_schema')")
        if cursor.fetchone()[0] is not None:
            cursor.execute("SELECT version FROM v7_agent_checkpoint_schema WHERE singleton")
            row = cursor.fetchone()
            if row:
                facts["agent_checkpoint"] = row[0]
    return facts


def _check_schema(kind: str, facts: dict[str, str]) -> None:
    expected = EXPECTED_SCHEMAS[kind]
    if any(facts.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"{kind} database schema differs from this release")
    if kind == "platform" and facts.get("agent_checkpoint") not in {
        None,
        "0001_langgraph_checkpoint",
    }:
        raise RuntimeError("Agent checkpoint schema differs from this release")


def _tables(connection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
        )
        return [row[0] for row in cursor.fetchall() if row[0] not in MARKER_TABLES]


def _table_order(connection, tables: list[str]) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tc.table_name, ccu.table_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.constraint_column_usage ccu "
            "ON ccu.constraint_schema=tc.constraint_schema "
            "AND ccu.constraint_name=tc.constraint_name "
            "WHERE tc.constraint_schema='public' AND tc.constraint_type='FOREIGN KEY'"
        )
        edges = [(child, parent) for child, parent in cursor.fetchall() if child != parent]
    remaining = set(tables)
    dependencies = {table: set() for table in tables}
    for child, parent in edges:
        if child in remaining and parent in remaining:
            dependencies[child].add(parent)
    ordered: list[str] = []
    while remaining:
        ready = sorted(table for table in remaining if not (dependencies[table] & remaining))
        if not ready:
            raise RuntimeError(
                "Database contains a foreign-key cycle that cannot be restored safely"
            )
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _copy_out(connection, table: str, destination: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    with destination.open("wb") as stream, connection.cursor() as cursor:
        statement = sql.SQL("COPY {} TO STDOUT (FORMAT BINARY)").format(sql.Identifier(table))
        with cursor.copy(statement) as copy:
            for chunk in copy:
                content = bytes(chunk)
                stream.write(content)
                hasher.update(content)
        stream.flush()
        os.fsync(stream.fileno())
    destination.chmod(0o600)
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        rows = cursor.fetchone()[0]
    return rows, "sha256:" + hasher.hexdigest()


def _snapshot_database(kind: str, url: str, destination: Path) -> dict:
    destination.mkdir(parents=True, mode=0o700)
    with psycopg.connect(psycopg_url(url)) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        facts = _schema_facts(connection)
        _check_schema(kind, facts)
        order = _table_order(connection, _tables(connection))
        records = []
        for index, table in enumerate(order):
            filename = f"{index:04d}-{table}.copy"
            rows, digest = _copy_out(connection, table, destination / filename)
            records.append({"table": table, "filename": filename, "rows": rows, "digest": digest})
    return {"kind": kind, "schema": facts, "tables": records}


def _safe_entries(root: Path):
    root = root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"State tree is missing: {root}")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            link_target = os.readlink(path)
            if not link_target or len(link_target.encode()) > 4096 or "\x00" in link_target:
                raise RuntimeError(f"State tree contains an invalid symlink: {relative}")
            # Toil JobStores can retain absolute links to their former container
            # workspace. Preserve the link text as recovery metadata, but never
            # follow it while taking or verifying a backup.
            yield relative, "SYMLINK", link_target
        elif path.is_file():
            yield relative, "FILE", path
        elif not path.is_dir():
            raise RuntimeError(f"State tree contains an unsupported entry: {relative}")


def _snapshot_tree(name: str, source: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, mode=0o700)
    records = []
    for relative, entry_type, value in _safe_entries(source):
        if entry_type == "SYMLINK":
            records.append({"path": relative.as_posix(), "type": entry_type, "target": value})
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(value, target)
        target.chmod(0o600)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        records.append(
            {
                "path": relative.as_posix(),
                "type": entry_type,
                "size": target.stat().st_size,
                "digest": file_digest(target),
            }
        )
    return {"kind": name, "files": records, "tree_digest": canonical_digest(records)}


def quiescence(configuration: BackupConfiguration) -> dict[str, dict[str, int]]:
    result = {}
    for kind, queries in QUIESCENCE_QUERIES.items():
        with psycopg.connect(psycopg_url(configuration.databases[kind])) as connection:
            _check_schema(kind, _schema_facts(connection))
            with connection.cursor() as cursor:
                result[kind] = {}
                for name, query in queries.items():
                    cursor.execute(query)
                    result[kind][name] = cursor.fetchone()[0]
    return result


def is_quiescent(result: dict[str, dict[str, int]]) -> bool:
    return all(value == 0 for service in result.values() for value in service.values())


def _verified_park(configuration: BackupConfiguration) -> dict | None:
    path = configuration.park_manifest
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Park manifest cannot be read") from error
    supplied = value.pop("manifest_digest", None)
    if supplied != canonical_digest(value):
        raise RuntimeError("Park manifest digest differs")
    value["manifest_digest"] = supplied
    if (
        value.get("schema_version") != 1
        or value.get("source_release_digest") != configuration.release_digest
        or value.get("state") != "PARKED"
    ):
        raise RuntimeError("Park manifest does not describe this parked release")
    return value


def is_backup_safe(result: dict[str, dict[str, int]], park: dict | None) -> bool:
    if is_quiescent(result):
        return True
    if park is None:
        return False
    # Durable Action/Execution/Trial/Agent waiting facts may remain active after
    # their workers and Toil leaders are parked.  Import, publication, model
    # transport and lifecycle delivery must have crossed a settled boundary.
    allowed = {
        ("platform", "outbox"),
        ("platform", "active_actions"),
        ("platform", "workflow_trials"),
        ("platform", "agent_advancing"),
        ("platform", "agent_tools"),
        ("execution", "executions"),
    }
    return all(
        count == 0 or (service, name) in allowed
        for service, values in result.items()
        for name, count in values.items()
    )


def wait_quiescent(configuration: BackupConfiguration, timeout: float, interval: float = 1) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        result = quiescence(configuration)
        if is_quiescent(result):
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError("V7 did not reach a cold-backup boundary before the timeout")
        time.sleep(interval)


def create_backup(configuration: BackupConfiguration, destination: str | Path) -> dict:
    if not configuration.maintenance_file.is_file():
        raise RuntimeError("The public write barrier must be active before backup")
    current = quiescence(configuration)
    park = _verified_park(configuration)
    if not is_backup_safe(current, park):
        raise RuntimeError("The deployment still has unsettled work")
    target = Path(destination)
    if target.exists():
        raise FileExistsError("Backup destination already exists")
    temporary = target.with_name(target.name + ".partial-" + uuid4().hex)
    temporary.mkdir(parents=True, mode=0o700)
    try:
        databases = {
            name: _snapshot_database(name, url, temporary / "databases" / name)
            for name, url in configuration.databases.items()
        }
        trees = {
            name: _snapshot_tree(name, path, temporary / "trees" / name)
            for name, path in configuration.trees.items()
        }
        config_directory = temporary / "configuration"
        config_directory.mkdir(mode=0o700)
        config_records = []
        for name, source in configuration.files.items():
            target_file = config_directory / name
            shutil.copyfile(source, target_file)
            target_file.chmod(0o600)
            config_records.append({"name": name, "digest": file_digest(target_file)})
        manifest = {
            "schema_version": BACKUP_SCHEMA,
            "release_digest": configuration.release_digest,
            "created_at_unix_ns": time.time_ns(),
            "quiescence": current,
            "databases": databases,
            "trees": trees,
            "configuration": config_records,
            "park": (
                {
                    "filename": "park-manifest.json",
                    "manifest_digest": park["manifest_digest"],
                    "file_sha256": file_digest(configuration.park_manifest),
                }
                if park is not None
                else None
            ),
        }
        if park is not None:
            shutil.copyfile(configuration.park_manifest, temporary / "park-manifest.json")
            (temporary / "park-manifest.json").chmod(0o600)
        manifest["manifest_digest"] = canonical_digest(manifest)
        path = temporary / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verified_manifest(source: str | Path, expected_release_digest: str | None = None) -> dict:
    root = Path(source)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {1, 2}:
        raise RuntimeError("Unsupported backup manifest")
    supplied = manifest.pop("manifest_digest", None)
    if supplied != canonical_digest(manifest):
        raise RuntimeError("Backup manifest digest differs")
    manifest["manifest_digest"] = supplied
    if expected_release_digest and manifest["release_digest"] != expected_release_digest:
        raise RuntimeError("Backup release differs from the selected deployment")
    for service in manifest["databases"].values():
        for record in service["tables"]:
            path = root / "databases" / service["kind"] / record["filename"]
            if file_digest(path) != record["digest"]:
                raise RuntimeError(f"Database backup changed: {service['kind']}.{record['table']}")
    for tree in manifest["trees"].values():
        for record in tree["files"]:
            if record.get("type", "FILE") == "SYMLINK":
                if (
                    not isinstance(record.get("target"), str)
                    or not record["target"]
                    or len(record["target"].encode()) > 4096
                    or "\x00" in record["target"]
                ):
                    raise RuntimeError(f"State backup has an invalid link: {record['path']}")
                continue
            path = root / "trees" / tree["kind"] / record["path"]
            if path.stat().st_size != record["size"] or file_digest(path) != record["digest"]:
                raise RuntimeError(f"State backup changed: {tree['kind']}/{record['path']}")
        if canonical_digest(tree["files"]) != tree["tree_digest"]:
            raise RuntimeError(f"State tree manifest changed: {tree['kind']}")
    for record in manifest["configuration"]:
        if file_digest(root / "configuration" / record["name"]) != record["digest"]:
            raise RuntimeError(f"Deployment configuration changed: {record['name']}")
    park = manifest.get("park")
    if park is not None:
        park_path = root / park["filename"]
        parked = json.loads(park_path.read_text(encoding="utf-8"))
        if file_digest(park_path) != park.get("file_sha256") or parked.get(
            "manifest_digest"
        ) != park.get("manifest_digest"):
            raise RuntimeError("Park manifest changed")
        park_supplied = parked.pop("manifest_digest", None)
        if park_supplied != canonical_digest(parked):
            raise RuntimeError("Park manifest changed")
    return manifest


def _restore_database(kind: str, url: str, root: Path, section: dict) -> None:
    with psycopg.connect(psycopg_url(url)) as connection:
        facts = _schema_facts(connection)
        _check_schema(kind, facts)
        if facts != section["schema"]:
            raise RuntimeError(f"{kind} restore target schema differs from the backup")
        records = section["tables"]
        if records:
            identifiers = sql.SQL(", ").join(sql.Identifier(item["table"]) for item in records)
            connection.execute(sql.SQL("TRUNCATE {} CASCADE").format(identifiers))
        for record in records:
            statement = sql.SQL("COPY {} FROM STDIN (FORMAT BINARY)").format(
                sql.Identifier(record["table"])
            )
            with (root / "databases" / kind / record["filename"]).open("rb") as stream:
                with connection.cursor().copy(statement) as copy:
                    while chunk := stream.read(1024 * 1024):
                        copy.write(chunk)
        for record in records:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(record["table"]))
                )
                if cursor.fetchone()[0] != record["rows"]:
                    raise RuntimeError(f"Restored row count differs: {kind}.{record['table']}")


def _validate_restore_targets(configuration: BackupConfiguration, manifest: dict) -> None:
    """Validate all database targets before mutating the first one."""
    for kind, section in manifest["databases"].items():
        with psycopg.connect(psycopg_url(configuration.databases[kind])) as connection:
            facts = _schema_facts(connection)
            _check_schema(kind, facts)
            if facts != section["schema"]:
                raise RuntimeError(f"{kind} restore target schema differs from the backup")


def _restore_tree(
    target: Path,
    root: Path,
    section: dict,
    *,
    owner: tuple[int, int],
) -> None:
    target = target.resolve()
    if target == Path("/") or len(target.parts) < 3:
        raise RuntimeError("Refusing to replace an unsafe state path")
    target.mkdir(parents=True, exist_ok=True)
    os.chown(target, *owner)
    for item in target.iterdir():
        shutil.rmtree(item) if item.is_dir() and not item.is_symlink() else item.unlink()
    for record in section["files"]:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Backup contains an unsafe state path")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if record.get("type", "FILE") == "SYMLINK":
            os.symlink(record["target"], destination)
            os.chown(destination, *owner, follow_symlinks=False)
        else:
            source = root / "trees" / section["kind"] / relative
            shutil.copyfile(source, destination)
            destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
            os.chown(destination, *owner, follow_symlinks=False)
    for directory, directories, _files in os.walk(target, topdown=False):
        for name in directories:
            os.chown(Path(directory) / name, *owner, follow_symlinks=False)
        os.chown(directory, *owner, follow_symlinks=False)


def restore_backup(
    configuration: BackupConfiguration,
    source: str | Path,
    *,
    confirmed: bool = False,
) -> dict:
    if not confirmed:
        raise RuntimeError("Restore requires explicit confirmation")
    if not configuration.maintenance_file.is_file():
        raise RuntimeError("The public write barrier must remain active during restore")
    root = Path(source)
    manifest = verified_manifest(root, configuration.release_digest)
    _validate_restore_targets(configuration, manifest)
    for kind, section in manifest["databases"].items():
        _restore_database(kind, configuration.databases[kind], root, section)
    for name, section in manifest["trees"].items():
        _restore_tree(
            configuration.trees[name],
            root,
            section,
            owner=(configuration.state_uid, configuration.state_gid),
        )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Cold backup and restore all V7 service state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    wait = subparsers.add_parser("wait")
    wait.add_argument("--timeout", type=float, default=600)
    backup = subparsers.add_parser("backup")
    backup.add_argument("directory")
    backup.add_argument("--park-manifest", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("directory")
    restore = subparsers.add_parser("restore")
    restore.add_argument("directory")
    restore.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args()
    configuration = configuration_from_environment()
    if args.command == "status":
        result = quiescence(configuration)
        value = {"quiescent": is_quiescent(result), "counts": result}
    elif args.command == "wait":
        result = wait_quiescent(configuration, args.timeout)
        value = {"quiescent": True, "counts": result}
    elif args.command == "backup":
        if args.park_manifest is not None:
            configuration = BackupConfiguration(
                **{
                    **configuration.__dict__,
                    "park_manifest": args.park_manifest,
                }
            )
        result = create_backup(configuration, args.directory)
        value = {"status": "ok", "manifest_digest": result["manifest_digest"]}
    elif args.command == "verify":
        result = verified_manifest(args.directory, configuration.release_digest)
        value = {"status": "ok", "manifest_digest": result["manifest_digest"]}
    else:
        result = restore_backup(
            configuration,
            args.directory,
            confirmed=args.confirm_restore,
        )
        value = {"status": "ok", "manifest_digest": result["manifest_digest"]}
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
