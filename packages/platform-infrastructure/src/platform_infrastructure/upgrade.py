"""P8.2 maintenance-window upgrade, parking and rollback evidence.

The module owns release compatibility and durable operation facts.  Docker
Compose orchestration stays in ``scripts/operations/p8-upgrade.sh`` so operators can audit
every service transition without granting the application an orchestration API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from platform_contracts.release import P8_DATABASE_SCHEMAS, file_digest, load_release_lock
from psycopg.rows import dict_row

from .cold_backup import (
    DATABASE_ENV,
    TREE_ENV,
    canonical_digest,
    configuration_from_environment,
    is_backup_safe,
    psycopg_url,
    quiescence,
)

PARK_SCHEMA = 1
OPERATION_SCHEMA = 1
TERMINAL_EXECUTIONS = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"}
STATES = (
    "PLANNED",
    "BARRIER_ACTIVE",
    "PARKED",
    "BACKED_UP",
    "TARGET_INSTALLED",
    "TARGET_VERIFIED",
    "COMMITTED",
    "ROLLING_BACK",
    "ROLLED_BACK",
)
TRANSITIONS = {
    "PLANNED": {"BARRIER_ACTIVE", "ROLLING_BACK"},
    "BARRIER_ACTIVE": {"PARKED", "ROLLING_BACK"},
    "PARKED": {"BACKED_UP", "ROLLING_BACK"},
    "BACKED_UP": {"TARGET_INSTALLED", "ROLLING_BACK"},
    "TARGET_INSTALLED": {"TARGET_VERIFIED", "ROLLING_BACK"},
    "TARGET_VERIFIED": {"COMMITTED", "ROLLING_BACK"},
    "ROLLING_BACK": {"ROLLED_BACK"},
    "COMMITTED": set(),
    "ROLLED_BACK": set(),
}
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _atomic_json(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _environment_bytes(values: dict[str, str]) -> bytes:
    lines = []
    for key, value in values.items():
        if not ENV_KEY.fullmatch(key) or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("Environment contains an invalid name or value")
        lines.append(f"{key}={value}\n")
    return "".join(lines).encode("utf-8")


def load_env_file(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Deployment environment file is unavailable")
    if path.stat().st_mode & 0o077:
        raise ValueError("Deployment environment file permissions are too broad")
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#"):
            continue
        key, separator, value = raw.partition("=")
        if not separator or not ENV_KEY.fullmatch(key) or "\x00" in value:
            raise ValueError("Deployment environment file is malformed")
        result[key] = value
    return result


def load_release(directory: str | Path) -> tuple[Path, dict]:
    directory = Path(directory).resolve()
    path = directory / "release-lock.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Release directory has no readable lock") from error
    return directory, load_release_lock(path, raw.get("release_digest", ""))


def validate_upgrade(source_directory: str | Path, target_directory: str | Path) -> dict:
    source_root, source = load_release(source_directory)
    target_root, target = load_release(target_directory)
    if target.get("schema_version") != 3:
        raise ValueError("Upgrade target requires Release Lock schema v3")
    compatibility = target["compatibility"]
    source_version = source.get("version")
    recovery = compatibility["recovery_releases"].get(source_version)
    if source_version not in compatibility["upgrade_from"] or recovery is None:
        raise ValueError("Release upgrade path is not allowed")
    if recovery["release_digest"] != source["release_digest"]:
        raise ValueError("Recovery release identity differs")
    if compatibility["database_schemas"] != P8_DATABASE_SCHEMAS:
        raise ValueError("Target database compatibility differs")
    for name in ("operator_catalog", "checker_catalog", "tool_catalog"):
        if recovery["catalogs"].get(name) != source.get(name, {}).get("digest"):
            raise ValueError(f"Recovery {name} differs")
    if recovery["images"] != source.get("images"):
        raise ValueError("Recovery image set differs")
    required = (
        "release-lock.json",
        "operator-catalog.json",
        "checker-catalog.json",
        "tool-catalog.json",
        "model-catalog.json",
        "agent-runtime-profile.json",
    )
    for root in (source_root, target_root):
        for name in required:
            if not (root / name).is_file():
                raise ValueError(f"Release artifact is missing: {root.name}/{name}")
    target_profile = json.loads((target_root / "agent-runtime-profile.json").read_text())
    if "embedding_catalog_digest" in target_profile:
        embedding_path = target_root / "embedding-catalog.json"
        if not embedding_path.is_file():
            raise ValueError("Target embedding catalog is missing")
        embedding = json.loads(embedding_path.read_text())
        if embedding.get("catalog_digest") != target_profile["embedding_catalog_digest"]:
            raise ValueError("Target embedding catalog differs from the Agent runtime profile")
    if recovery["model_catalog"] != file_digest(source_root / "model-catalog.json"):
        raise ValueError("Recovery model catalog differs")
    if recovery["runtime_profile"] != file_digest(source_root / "agent-runtime-profile.json"):
        raise ValueError("Recovery runtime profile differs")
    return {
        "schema_version": OPERATION_SCHEMA,
        "source_version": source_version,
        "source_release_digest": source["release_digest"],
        "source_release_dir": str(source_root),
        "target_version": target["version"],
        "target_release_digest": target["release_digest"],
        "target_release_dir": str(target_root),
        "database_schemas": P8_DATABASE_SCHEMAS,
        "api_contract": compatibility["api_contract"],
        "backup_format": 2,
    }


class Operation:
    def __init__(self, directory: str | Path):
        self.root = Path(directory).resolve()
        self.state_path = self.root / "operation.json"
        self.events_path = self.root / "events.jsonl"

    def create(self, plan: dict, source_env: Path) -> dict:
        if self.root.exists():
            raise FileExistsError("Upgrade operation directory already exists")
        self.root.mkdir(parents=True, mode=0o700)
        source_env = source_env.resolve()
        source_snapshot = self.root / "source.env.snapshot"
        source_bytes = source_env.read_bytes()
        _atomic_bytes(source_snapshot, source_bytes)
        value = {
            **plan,
            "operation_id": uuid4().hex,
            "state": "PLANNED",
            "source_env": str(source_env.resolve()),
            "source_env_sha256": file_digest(source_env),
            "source_env_mode": oct(source_env.stat().st_mode & 0o777),
            "source_env_snapshot": str(source_snapshot),
            "source_env_snapshot_sha256": file_digest(source_snapshot),
            "created_at_unix_ns": time.time_ns(),
            "committed_at_unix_ns": None,
        }
        value["operation_digest"] = canonical_digest(value)
        _atomic_json(self.state_path, value)
        self._event("PLANNED", value["operation_digest"])
        return value

    def read(self) -> dict:
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        supplied = value.pop("operation_digest", None)
        if supplied != canonical_digest(value):
            raise RuntimeError("Upgrade operation state changed")
        value["operation_digest"] = supplied
        return value

    def _event(self, state: str, operation_digest: str) -> None:
        previous = "sha256:" + "0" * 64
        if self.events_path.is_file():
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
            if lines:
                previous = json.loads(lines[-1])["event_digest"]
        event = {
            "sequence": sum(1 for _ in self.events_path.open()) + 1
            if self.events_path.is_file()
            else 1,
            "state": state,
            "operation_digest": operation_digest,
            "previous_event_digest": previous,
            "recorded_at_unix_ns": time.time_ns(),
        }
        event["event_digest"] = canonical_digest(event)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events_path.chmod(0o600)

    def transition(self, target: str) -> dict:
        if target not in STATES:
            raise ValueError("Unknown upgrade state")
        value = self.read()
        if target not in TRANSITIONS[value["state"]]:
            raise RuntimeError(f"Invalid upgrade transition {value['state']} -> {target}")
        value.pop("operation_digest")
        value["state"] = target
        if target == "COMMITTED":
            value["committed_at_unix_ns"] = time.time_ns()
        value["operation_digest"] = canonical_digest(value)
        _atomic_json(self.state_path, value)
        self._event(target, value["operation_digest"])
        return value


@dataclass(frozen=True)
class ParkConfiguration:
    databases: dict[str, str]
    execution_root: Path
    output: Path
    deployment_id: str
    source_release_digest: str
    target_release_digest: str

    @classmethod
    def from_environment(cls, target_release_digest: str, output: str | Path):
        def required(name):
            return os.environ[name]

        return cls(
            databases={name: required(variable) for name, variable in DATABASE_ENV.items()},
            execution_root=Path(required(TREE_ENV["execution"])),
            output=Path(output),
            deployment_id=required("V7_DEPLOYMENT_ID"),
            source_release_digest=required("V7_RELEASE_DIGEST"),
            target_release_digest=target_release_digest,
        )


def _rows(url: str, query: str) -> list[dict]:
    with psycopg.connect(psycopg_url(url), row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            return [_jsonable(dict(row)) for row in cursor.fetchall()]


def _active_facts(configuration: ParkConfiguration) -> dict:
    platform = _rows(
        configuration.databases["platform"],
        """
        SELECT a.action_id,a.command_id,a.task_id,a.project_id,a.spec_revision,a.state,
               a.send_started,a.execution_id,a.external_revision,a.cancel_requested,
               r.charged,r.stopped AS reservation_stopped,
               b.execution_count,b.max_in_flight,b.used,b.reserved,b.in_flight
          FROM actions a
          JOIN reservations r ON r.action_id=a.action_id
          JOIN budgets b ON b.task_id=a.task_id
         WHERE NOT a.stopped ORDER BY a.action_id
        """,
    )
    execution = _rows(
        configuration.databases["execution"],
        """
        SELECT e.action_id,e.execution_id,e.parent_execution_id,e.project_id,e.task_id,
               e.state,e.revision,e.executed,e.cancel_requested,e.attempt_id,e.container_id,
               e.work_class,e.generation,e.runtime->>'phase' AS phase,
               (e.runtime->>'engine_epoch')::bigint AS engine_epoch,
               e.runtime->>'image_id' AS image_id,
               e.runtime->>'engine_package_digest' AS engine_package_digest,
               e.runtime->>'publication' AS publication,
               r.binding->>'service_binding_id' AS service_binding_id,
               (r.binding->>'revision')::bigint AS binding_revision,
               r.remote_execution_id,r.remote_revision,r.remote_state,r.executed AS remote_executed
          FROM executions e LEFT JOIN remote_calls r ON r.action_id=e.action_id
         WHERE e.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','REJECTED','SEALED')
         ORDER BY e.action_id
        """,
    )
    waiting = {
        "agent_runs": _rows(
            configuration.databases["platform"],
            "SELECT agent_run_id,task_id,status,epoch,generation,runtime_ref FROM agent_runs "
            "WHERE status IN ('ADVANCING','WAITING') ORDER BY agent_run_id",
        ),
        "workflow_trials": _rows(
            configuration.databases["platform"],
            "SELECT trial_id,task_id,action_id,case_verdict,generation FROM workflow_trials "
            "WHERE case_verdict='PENDING' ORDER BY trial_id",
        ),
        "acceptances": _rows(
            configuration.databases["platform"],
            "SELECT acceptance_id,task_id,action_id,status FROM acceptances "
            "WHERE status NOT IN ('PASS','FAIL','ERROR','CANCELLED') ORDER BY acceptance_id",
        ),
    }
    remote = []
    for item in execution:
        if item.get("service_binding_id"):
            remote.append(
                {
                    key: item.get(key)
                    for key in (
                        "action_id",
                        "execution_id",
                        "remote_execution_id",
                        "service_binding_id",
                        "binding_revision",
                        "remote_revision",
                        "remote_state",
                        "remote_executed",
                    )
                }
                | {"park_state": "PARKED_UNKNOWN"}
            )
    return {"actions": platform, "executions": execution, "remote_calls": remote, **waiting}


def _jobstore_facts(configuration: ParkConfiguration, executions: list[dict]) -> list[dict]:
    result = []
    for row in executions:
        epoch = row.get("engine_epoch")
        if row.get("work_class") != "WORKFLOW_SUPERVISOR" or epoch is None:
            continue
        root = configuration.execution_root / row["execution_id"]
        jobstore = root / "jobstore"
        records = []
        for name in ("shared/rootJobStoreID", "config.pickle"):
            path = jobstore / name
            if path.is_file() and not path.is_symlink():
                records.append(
                    {"path": name, "size": path.stat().st_size, "digest": file_digest(path)}
                )
        result.append(
            {
                "action_id": row["action_id"],
                "execution_id": row["execution_id"],
                "engine_epoch": epoch,
                "jobstore": str(jobstore.resolve()),
                "restartable": (jobstore / "shared" / "rootJobStoreID").is_file(),
                "key_files": records,
            }
        )
    return result


def _docker_inspect_all() -> list[dict]:
    listed = subprocess.run(
        ["docker", "ps", "-aq"], capture_output=True, text=True, timeout=10, check=False
    )
    if listed.returncode:
        raise RuntimeError("Docker inventory is unavailable")
    identities = listed.stdout.split()
    if not identities:
        return []
    inspected = subprocess.run(
        ["docker", "inspect", *identities],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if inspected.returncode:
        raise RuntimeError("Docker inspection failed")
    return json.loads(inspected.stdout)


def _owned_containers(configuration: ParkConfiguration, action_ids: set[str]) -> list[dict]:
    result = []
    for info in _docker_inspect_all():
        labels = info.get("Config", {}).get("Labels") or {}
        action = labels.get("v7.action")
        deployment = labels.get("v7.deployment")
        # 0.15.0 containers predate the deployment label.  Their frozen Action
        # identity is sufficient during this one N-1 upgrade only.
        if action not in action_ids or deployment not in {None, configuration.deployment_id}:
            continue
        result.append(
            {
                "container_id": info["Id"],
                "name": info.get("Name", "").lstrip("/"),
                "action_id": action,
                "execution_id": labels.get("v7.execution"),
                "attempt": labels.get("v7.attempt"),
                "epoch": labels.get("v7.epoch"),
                "phase": labels.get("v7.phase"),
                "release_digest": labels.get("v7.release"),
                "running": bool(info.get("State", {}).get("Running")),
                "exit_code": info.get("State", {}).get("ExitCode"),
            }
        )
    return result


def park(configuration: ParkConfiguration, *, timeout: float = 240.0) -> dict:
    if configuration.output.exists():
        raise FileExistsError("Park manifest already exists")
    backup_configuration = configuration_from_environment()
    deadline = time.monotonic() + timeout
    while True:
        counts = quiescence(backup_configuration)
        # Parking may retain durable execution, Action, Trial and Agent waiting
        # records, but never snapshots an active publication/import/model call.
        if is_backup_safe(counts, {"state": "PARKED"}):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Deployment did not reach a parking boundary")
        time.sleep(1)

    facts = _active_facts(configuration)
    action_ids = {item["action_id"] for item in facts["executions"]}
    while True:
        containers = _owned_containers(configuration, action_ids)
        active_science = [
            item for item in containers if item["running"] and item.get("phase") != "P4"
        ]
        if not active_science:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Local scientific containers did not settle before parking timeout")
        time.sleep(1)

    # A Toil leader is stopped only after leaf computation is no longer active.
    # Its supervisor writes the trusted exit fact and retains the original JobStore.
    for item in _owned_containers(configuration, action_ids):
        if item["running"] and item.get("phase") == "P4":
            stopped = subprocess.run(
                ["docker", "stop", "--time", "30", item["container_id"]],
                capture_output=True,
                timeout=35,
                check=False,
            )
            if stopped.returncode:
                raise RuntimeError("Toil leader could not be parked safely")

    final_containers = _owned_containers(configuration, action_ids)
    if any(item["running"] for item in final_containers):
        raise RuntimeError("A deployment container is still active after parking")
    jobstores = _jobstore_facts(configuration, facts["executions"])
    for record in jobstores:
        if not record["restartable"]:
            raise RuntimeError("Active workflow has no recoverable JobStore")
    manifest = {
        "schema_version": PARK_SCHEMA,
        "state": "PARKED",
        "deployment_id": configuration.deployment_id,
        "parking_generation": time.time_ns(),
        "source_release_digest": configuration.source_release_digest,
        "target_release_digest": configuration.target_release_digest,
        "database_schemas": P8_DATABASE_SCHEMAS,
        "quiescence": counts,
        **facts,
        "jobstores": jobstores,
        "containers": final_containers,
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    _atomic_json(configuration.output, manifest)
    return manifest


def verify_park(path: str | Path, *, source_digest: str | None = None) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    supplied = value.pop("manifest_digest", None)
    if supplied != canonical_digest(value):
        raise RuntimeError("Park manifest digest differs")
    value["manifest_digest"] = supplied
    if value.get("schema_version") != PARK_SCHEMA or value.get("state") != "PARKED":
        raise RuntimeError("Park manifest state is invalid")
    if source_digest is not None and value.get("source_release_digest") != source_digest:
        raise RuntimeError("Park manifest release differs")
    for record in value.get("jobstores", []):
        root = Path(record["jobstore"])
        for item in record["key_files"]:
            path = root / item["path"]
            if path.stat().st_size != item["size"] or file_digest(path) != item["digest"]:
                raise RuntimeError("Parked JobStore changed")
    return value


def write_target_env(
    source_env: Path,
    target_release: Path,
    output: Path,
    *,
    park_manifest: Path | None = None,
) -> None:
    source = load_env_file(source_env)
    root, release = load_release(target_release)
    values = dict(source)
    deployment_id = source.get("V7_DEPLOYMENT_ID") or source.get("V7_P8_PROJECT_NAME")
    control_root = source.get("V7_P8_CONTROL_DIR")
    if not deployment_id or not control_root:
        raise ValueError("Source deployment identity or control directory is missing")
    values |= {
        "V7_P8_RELEASE_DIR": str(root),
        # Compose interpolates V7_P8_ENV_FILE before creating a service.  If
        # this still pointed at the N-1 file, the target image would silently
        # receive the old release environment even though Compose itself was
        # invoked with target.env.
        "V7_P8_ENV_FILE": str(output.resolve()),
        "V7_P8_APP_IMAGE": release["image_refs"]["app"],
        "V7_RELEASE_LOCK": str(root / "release-lock.json"),
        "V7_RELEASE_DIGEST": release["release_digest"],
        "V7_OPERATOR_CATALOG": str(root / "operator-catalog.json"),
        "V7_CHECKER_CATALOG": str(root / "checker-catalog.json"),
        "V7_AGENT_TOOL_CATALOG": str(root / "tool-catalog.json"),
        "V7_MODEL_CATALOG": str(root / "model-catalog.json"),
        **(
            {"V7_EMBEDDING_CATALOG": str(root / "embedding-catalog.json")}
            if (root / "embedding-catalog.json").is_file()
            else {}
        ),
        "V7_AGENT_RUNTIME_PROFILE": str(root / "agent-runtime-profile.json"),
        "V7_OPERATOR_IMAGE": release["images"]["operator"],
        "V7_CHECKER_IMAGE": release["images"]["checker"],
        "V7_GDAL_CHECKER_IMAGE": release["images"]["gdal_checker"],
        "V7_SPATIAL_QGIS_IMAGE_ID": release["images"]["qgis"],
        "V7_SPATIAL_NATIVE_IMAGE_ID": release["images"]["spatial_native"],
        "V7_TABULAR_OPERATOR_IMAGE": release["images"]["tabular"],
        "V7_CWL_IMAGE": release["images"]["cwl"],
        # P8.1 environments predate both fields.  Their immutable Compose
        # project identity is the only safe deployment identity to adopt
        # during the N-1 upgrade.
        "V7_DEPLOYMENT_ID": deployment_id,
        "V7_PARK_MANIFEST": str(
            (park_manifest or Path(control_root) / "park-manifest.json").resolve()
        ),
    }
    _atomic_bytes(output, _environment_bytes(values))


def install_committed_environment(operation_dir: str | Path) -> dict:
    """Promote the verified target environment after the irreversible commit.

    Before COMMITTED the canonical environment remains byte-for-byte N-1 so
    rollback can always start the source release.  After COMMITTED this helper
    atomically makes the canonical path describe the target release.  Failure
    leaves the maintenance barrier in place and cannot trigger an unsafe
    automatic downgrade.
    """

    operation = Operation(operation_dir)
    state = operation.read()
    if state["state"] != "COMMITTED":
        raise RuntimeError("Canonical environment can only be installed after COMMITTED")
    source_path = Path(state["source_env"])
    snapshot_path = Path(state["source_env_snapshot"])
    if file_digest(snapshot_path) != state["source_env_snapshot_sha256"]:
        raise RuntimeError("Source environment snapshot changed")
    target_path = operation.root / "target.env"
    target = load_env_file(target_path)
    if target.get("V7_RELEASE_DIGEST") != state["target_release_digest"]:
        raise RuntimeError("Target environment release differs")
    desired = dict(target)
    desired["V7_P8_ENV_FILE"] = str(source_path)
    desired_bytes = _environment_bytes(desired)
    desired_digest = "sha256:" + hashlib.sha256(desired_bytes).hexdigest()
    current = load_env_file(source_path)
    if current == desired:
        return {"status": "already-installed", "path": str(source_path), "digest": desired_digest}
    if file_digest(source_path) != state["source_env_sha256"]:
        raise RuntimeError("Canonical source environment changed before promotion")
    _atomic_bytes(source_path, desired_bytes)
    installed = load_env_file(source_path)
    if installed != desired or file_digest(source_path) != desired_digest:
        raise RuntimeError("Canonical target environment verification failed")
    return {"status": "installed", "path": str(source_path), "digest": desired_digest}


def main() -> None:
    parser = argparse.ArgumentParser(description="P8.2 release upgrade evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("source_env", type=Path)
    plan_parser.add_argument("target_release", type=Path)
    init_parser = commands.add_parser("init")
    init_parser.add_argument("source_env", type=Path)
    init_parser.add_argument("target_release", type=Path)
    init_parser.add_argument("operation_dir", type=Path)
    transition = commands.add_parser("transition")
    transition.add_argument("operation_dir", type=Path)
    transition.add_argument("state", choices=STATES)
    show = commands.add_parser("show")
    show.add_argument("operation_dir", type=Path)
    target_env = commands.add_parser("target-env")
    target_env.add_argument("source_env", type=Path)
    target_env.add_argument("target_release", type=Path)
    target_env.add_argument("output", type=Path)
    target_env.add_argument("--park-manifest", type=Path)
    parked = commands.add_parser("park")
    parked.add_argument("target_release_digest")
    parked.add_argument("output", type=Path)
    parked.add_argument("--timeout", type=float, default=240)
    verify = commands.add_parser("verify-park")
    verify.add_argument("path", type=Path)
    verify.add_argument("--source-digest")
    installed = commands.add_parser("install-committed-environment")
    installed.add_argument("operation_dir", type=Path)
    args = parser.parse_args()

    if args.command in {"plan", "init"}:
        environment = load_env_file(args.source_env)
        plan = validate_upgrade(environment["V7_P8_RELEASE_DIR"], args.target_release)
        if args.command == "init":
            value = Operation(args.operation_dir).create(plan, args.source_env)
        else:
            value = plan
    elif args.command == "transition":
        value = Operation(args.operation_dir).transition(args.state)
    elif args.command == "show":
        value = Operation(args.operation_dir).read()
    elif args.command == "target-env":
        write_target_env(
            args.source_env,
            args.target_release,
            args.output,
            park_manifest=args.park_manifest,
        )
        value = {"status": "ok", "path": str(args.output.resolve())}
    elif args.command == "park":
        value = park(
            ParkConfiguration.from_environment(args.target_release_digest, args.output),
            timeout=args.timeout,
        )
    elif args.command == "verify-park":
        value = verify_park(args.path, source_digest=args.source_digest)
    else:
        value = install_committed_environment(args.operation_dir)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
