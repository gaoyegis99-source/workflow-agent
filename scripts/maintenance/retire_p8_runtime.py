"""Fail-closed retirement workflow for an explicitly inventoried V7 P8 runtime.

V7 currently has no active P8 instance. Any residual runtime may be retired
only after an external, restore-tested cold backup and secret-escrow receipt
have been reviewed.

No public method accepts a P8 path, Docker path, glob, or arbitrary deletion
target.  The fixed lifecycle is::

    plan -> backup-verify -> stage --execute -> verify
         -> purge --scope metadata|docker-data --execute

``metadata`` is limited to the exact P8 paths listed in the immutable plan.
``docker-data`` is limited to exact stopped container IDs and V7-labelled
volume names captured by the V7 rootless daemon.  Images are inventory only;
there is deliberately no prune operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .project_environment import (
        EnvironmentRefusal,
        child_environment,
        docker_executable,
        require_socket,
    )
except ImportError:  # pragma: no cover - direct script entrypoint support.
    from project_environment import (  # type: ignore
        EnvironmentRefusal,
        child_environment,
        docker_executable,
        require_socket,
    )


SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SOURCE_ROOT.parent
P8_ROOT = PROJECT_ROOT / "runtime/p8"
RUN_ID = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-[a-z0-9][a-z0-9-]{0,48})?$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SCHEMA = "v7-p8-retirement.v1"
BACKUP_SCHEMA = "v7-p8-cold-backup.v1"
RESTORE_SCHEMA = "v7-p8-restore-evidence.v1"
ESCROW_SCHEMA = "v7-secret-escrow-receipt.v1"
REQUIRED_BACKUP_COVERAGE = frozenset({"databases", "object-storage", "artifacts", "configuration"})
REQUIRED_ESCROW_COVERAGE = frozenset({"p8-service-secrets"})
SENSITIVE_PART = re.compile(r"(?:^|[-_.])(secret|token|password|credential|key)(?:$|[-_.])", re.I)
SENSITIVE_TEXT_MARKER = re.compile(
    rb"(?:authorization|token|secret|password|credential|api[_-]?key)\s*(?:=|:)",
    re.I,
)
STAGEABLE_TOP_LEVEL = frozenset({"releases", "verification", "logs"})


class P8RetirementRefusal(RuntimeError):
    """A safety precondition failed and no destructive action may proceed."""


@dataclass(frozen=True)
class TreeSummary:
    sha256: str
    file_count: int
    bytes: int
    secret_file_count: int
    secret_bytes: int
    secret_sha256: str | None
    contains_sensitive_path: bool


DockerProbe = Callable[[str], dict[str, Any]]


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_run_id(value: str | None) -> str:
    if not isinstance(value, str) or not RUN_ID.fullmatch(value):
        raise P8RetirementRefusal("P8_RETIREMENT_RUN_ID_INVALID")
    return value


def _safe_name(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        raise P8RetirementRefusal(f"{code}_INVALID")
    return value


def _assert_directory(path: Path, *, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise P8RetirementRefusal(f"{code}_MISSING") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise P8RetirementRefusal(f"{code}_INVALID")


def _read_json(path: Path, *, code: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise P8RetirementRefusal(f"{code}_INVALID")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise P8RetirementRefusal(f"{code}_INVALID") from error
    if not isinstance(payload, dict):
        raise P8RetirementRefusal(f"{code}_INVALID")
    return payload


def _is_sensitive_path(relative: Path) -> bool:
    return any(
        part == "secrets"
        or part == ".env"
        or part.endswith(".env")
        or part.startswith(".env.")
        or SENSITIVE_PART.search(part)
        for part in relative.parts
    )


def _has_sensitive_text_marker(path: Path) -> bool:
    """Screen copy candidates without recording or redacting their content."""
    try:
        with path.open("rb") as stream:
            previous = b""
            while block := stream.read(1024 * 1024):
                if SENSITIVE_TEXT_MARKER.search(previous + block):
                    return True
                previous = block[-128:]
    except OSError as error:
        raise P8RetirementRefusal("P8_RUNTIME_ENTRY_UNREADABLE") from error
    return False


def _tree_summary(path: Path, *, code: str) -> TreeSummary:
    """Hash a tree without link following; secret names/values stay private."""

    _assert_directory(path, code=code)
    public: list[list[object]] = []
    secret: list[list[object]] = []
    sensitive = False

    def on_error(error: OSError) -> None:
        raise P8RetirementRefusal(f"{code}_UNREADABLE") from error

    for directory, names, files in os.walk(path, followlinks=False, onerror=on_error):
        current = Path(directory)
        allowed: list[str] = []
        for name in sorted(names):
            child = current / name
            try:
                info = child.lstat()
            except OSError as error:
                raise P8RetirementRefusal(f"{code}_UNREADABLE") from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise P8RetirementRefusal(f"{code}_LINK_OR_TYPE")
            allowed.append(name)
            sensitive = sensitive or _is_sensitive_path(child.relative_to(path))
        names[:] = allowed
        for name in sorted(files):
            child = current / name
            try:
                info = child.lstat()
            except OSError as error:
                raise P8RetirementRefusal(f"{code}_UNREADABLE") from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise P8RetirementRefusal(f"{code}_LINK_OR_TYPE")
            try:
                digest = _file_sha256(child)
            except OSError as error:
                raise P8RetirementRefusal(f"{code}_UNREADABLE") from error
            record: list[object] = [
                child.relative_to(path).as_posix(),
                info.st_size,
                digest,
            ]
            if _is_sensitive_path(child.relative_to(path)) or _has_sensitive_text_marker(child):
                sensitive = True
                secret.append(record)
            else:
                public.append(record)
    all_records = sorted([*public, *secret])
    return TreeSummary(
        sha256=_canonical_sha256(all_records),
        file_count=len(all_records),
        bytes=sum(int(item[1]) for item in all_records),
        secret_file_count=len(secret),
        secret_bytes=sum(int(item[1]) for item in secret),
        secret_sha256=_canonical_sha256(secret) if secret else None,
        contains_sensitive_path=sensitive,
    )


class P8Retirement:
    """Fixed-root P8 retirement implementation with injectable read-only probe."""

    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        *,
        p8_root: Path | None = None,
        docker_probe: DockerProbe | None = None,
    ) -> None:
        self.project_root = project_root.absolute()
        self.source_root = self.project_root / "source"
        self.p8_root = (p8_root or self.project_root / "runtime/p8").absolute()
        self._docker_probe = docker_probe or self._project_docker_probe

    @property
    def audit_root(self) -> Path:
        return self.project_root / "operations/maintenance/p8-retirement"

    @property
    def quarantine_root(self) -> Path:
        return self.project_root / "runtime/archive/p8-retirement"

    def _audit(self, run: str) -> Path:
        return self.audit_root / _safe_run_id(run)

    def _quarantine(self, run: str) -> Path:
        return self.quarantine_root / _safe_run_id(run)

    def _assert_roots(self) -> None:
        _assert_directory(self.project_root, code="PROJECT_ROOT")
        if self.source_root.is_symlink() or not self.source_root.is_dir():
            raise P8RetirementRefusal("PROJECT_SOURCE_INVALID")
        if not self.p8_root.is_relative_to(self.project_root / "runtime"):
            raise P8RetirementRefusal("P8_RUNTIME_OUTSIDE_PROJECT")
        _assert_directory(self.p8_root, code="P8_RUNTIME")
        for directory in (self.audit_root, self.quarantine_root):
            current = self.project_root
            for part in directory.relative_to(self.project_root).parts:
                current /= part
                if current.exists() and current.is_symlink():
                    raise P8RetirementRefusal("P8_RETIREMENT_PATH_SYMLINK")

    def _instance_metadata(self, instance: Path) -> tuple[dict[str, Any], str]:
        environment = instance / "p8.env"
        if environment.is_symlink() or not environment.is_file():
            raise P8RetirementRefusal("P8_INSTANCE_ENVIRONMENT_INVALID")
        try:
            lines = environment.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            raise P8RetirementRefusal("P8_INSTANCE_ENVIRONMENT_UNREADABLE") from error
        keys: list[str] = []
        project: str | None = None
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            keys.append(key)
            if key == "V7_P8_PROJECT_NAME":
                project = value.strip()
        if project is None:
            raise P8RetirementRefusal("P8_INSTANCE_PROJECT_MISSING")
        project = _safe_name(project, code="P8_INSTANCE_PROJECT")
        summary = _tree_summary(instance, code="P8_INSTANCE")
        metadata = {
            "instance": _safe_name(instance.name, code="P8_INSTANCE_NAME"),
            "p8_env_sha256": _file_sha256(environment),
            "environment_keys": sorted(set(keys)),
            "compose_project": project,
            "tree_sha256": summary.sha256,
            "file_count": summary.file_count,
            "bytes": summary.bytes,
            "secrets": {
                "present": bool(summary.secret_file_count),
                "file_count": summary.secret_file_count,
                "bytes": summary.secret_bytes,
                "tree_sha256": summary.secret_sha256,
                "note": "Secret names and values are excluded from retirement records.",
            },
        }
        return metadata, project

    def _project_docker_probe(self, compose_project: str) -> dict[str, Any]:
        """Inventory only this V7 rootless daemon and exact Compose project."""
        try:
            require_socket()
            executable = docker_executable()
            environment = child_environment()
            containers = subprocess.run(
                [
                    executable,
                    "ps",
                    "-a",
                    "--filter",
                    f"label=com.docker.compose.project={compose_project}",
                    "--format",
                    "{{.ID}}\\t{{.State}}\\t{{.Image}}",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            volumes = subprocess.run(
                [
                    executable,
                    "volume",
                    "ls",
                    "--filter",
                    f"label=com.docker.compose.project={compose_project}",
                    "--format",
                    "{{.Name}}",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        except (EnvironmentRefusal, OSError) as error:
            return {"status": "UNAVAILABLE", "reason": str(error)}
        if containers.returncode or volumes.returncode:
            return {"status": "UNAVAILABLE", "reason": "PROJECT_DOCKER_QUERY_FAILED"}
        values: list[dict[str, str]] = []
        for line in containers.stdout.splitlines():
            identifier, _, remainder = line.partition("\t")
            state, _, image = remainder.partition("\t")
            if identifier:
                values.append({"id": identifier, "state": state.lower(), "image": image})
        checked_volumes: list[dict[str, str]] = []
        for name in (line for line in volumes.stdout.splitlines() if line):
            inspected = subprocess.run(
                [
                    executable,
                    "volume",
                    "inspect",
                    name,
                    "--format",
                    "{{json .Labels}}",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            if inspected.returncode:
                return {"status": "UNAVAILABLE", "reason": "PROJECT_DOCKER_QUERY_FAILED"}
            try:
                labels = json.loads(inspected.stdout.strip() or "null")
            except ValueError:
                return {"status": "UNAVAILABLE", "reason": "P8_VOLUME_LABEL_MISMATCH"}
            if (
                not isinstance(labels, dict)
                or labels.get("com.docker.compose.project") != compose_project
            ):
                return {"status": "UNAVAILABLE", "reason": "P8_VOLUME_LABEL_MISMATCH"}
            checked_volumes.append({"name": name, "compose_project": compose_project})
        return {
            "status": "PASS",
            "containers": values,
            "volumes": checked_volumes,
            "images": [item["image"] for item in values if item["image"]],
        }

    def _docker_state(self, compose_project: str) -> dict[str, Any]:
        value = self._docker_probe(compose_project)
        if value.get("status") != "PASS":
            if value.get("reason") == "P8_VOLUME_LABEL_MISMATCH":
                raise P8RetirementRefusal("P8_VOLUME_LABEL_MISMATCH")
            return {"status": "UNAVAILABLE", "reason": value.get("reason", "UNKNOWN")}
        raw_containers = value.get("containers")
        raw_volumes = value.get("volumes")
        raw_images = value.get("images", [])
        if (
            not isinstance(raw_containers, list)
            or not isinstance(raw_volumes, list)
            or not isinstance(raw_images, list)
        ):
            return {"status": "UNAVAILABLE", "reason": "PROJECT_DOCKER_RESPONSE_INVALID"}
        containers: list[dict[str, str]] = []
        for item in raw_containers:
            if not isinstance(item, dict):
                return {"status": "UNAVAILABLE", "reason": "PROJECT_DOCKER_RESPONSE_INVALID"}
            identifier = item.get("id")
            state = item.get("state")
            if not isinstance(identifier, str) or not identifier or not isinstance(state, str):
                return {"status": "UNAVAILABLE", "reason": "PROJECT_DOCKER_RESPONSE_INVALID"}
            containers.append(
                {
                    "id": identifier,
                    "state": state.lower(),
                    "image": str(item.get("image", "")),
                }
            )
        volumes: list[str] = []
        for item in raw_volumes:
            if not isinstance(item, dict) or item.get("compose_project") != compose_project:
                raise P8RetirementRefusal("P8_VOLUME_LABEL_MISMATCH")
            name = item.get("name")
            if not isinstance(name, str) or not name:
                return {"status": "UNAVAILABLE", "reason": "PROJECT_DOCKER_RESPONSE_INVALID"}
            volumes.append(name)
        active = sorted(
            item["id"]
            for item in containers
            if item["state"] in {"running", "paused", "restarting", "created"}
        )
        return {
            "status": "PASS",
            "active_containers": active,
            "stopped_containers": sorted(
                item["id"] for item in containers if item["id"] not in active
            ),
            "volumes": sorted(volumes),
            "images": sorted(item["image"] for item in containers if item["image"]),
        }

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        if path.exists() or path.is_symlink():
            raise P8RetirementRefusal("P8_RETIREMENT_AUDIT_RECORD_EXISTS")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    def _runtime_targets(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        targets: list[dict[str, Any]] = []
        instances: list[dict[str, Any]] = []
        projects: dict[str, str] = {}
        for child in sorted(self.p8_root.iterdir(), key=lambda value: value.name):
            if child.is_symlink() or not child.is_dir():
                raise P8RetirementRefusal("P8_RUNTIME_ENTRY_INVALID")
            name = _safe_name(child.name, code="P8_RUNTIME_ENTRY")
            if name == "instances":
                for instance in sorted(child.iterdir(), key=lambda value: value.name):
                    if instance.is_symlink() or not instance.is_dir():
                        raise P8RetirementRefusal("P8_INSTANCE_ENTRY_INVALID")
                    metadata, project = self._instance_metadata(instance)
                    instances.append(metadata)
                    projects[metadata["instance"]] = project
                summary = _tree_summary(child, code="P8_INSTANCES")
            else:
                summary = _tree_summary(child, code="P8_RUNTIME_ENTRY")
            targets.append(
                {
                    "relative": name,
                    "tree_sha256": summary.sha256,
                    "file_count": summary.file_count,
                    "bytes": summary.bytes,
                    "secrets": {
                        "present": bool(summary.secret_file_count),
                        "file_count": summary.secret_file_count,
                        "bytes": summary.secret_bytes,
                        "tree_sha256": summary.secret_sha256,
                    },
                    "stageable": name in STAGEABLE_TOP_LEVEL
                    and not summary.contains_sensitive_path,
                    "retirement_scope": "metadata",
                }
            )
        return targets, instances, projects

    def plan(self, run_id: str | None) -> dict[str, Any]:
        run = _safe_run_id(run_id)
        self._assert_roots()
        audit = self._audit(run)
        if audit.exists() or audit.is_symlink():
            raise P8RetirementRefusal("P8_RETIREMENT_RUN_ALREADY_EXISTS")
        targets, instances, projects = self._runtime_targets()
        docker: dict[str, dict[str, Any]] = {}
        for project in sorted(set(projects.values())):
            docker[project] = self._docker_state(project)
        inventory = {
            "schema_version": SCHEMA,
            "run_id": run,
            "operation": "plan",
            "p8_root": "runtime/p8",
            "metadata_targets": targets,
            "instances": instances,
            "docker": docker,
            "protected_images": sorted(
                {
                    image
                    for state in docker.values()
                    if state.get("status") == "PASS"
                    for image in state.get("images", [])
                }
            ),
            "rules": {
                "no_source_deletion_before_backup_verify": True,
                "no_secret_copy_to_audit_or_quarantine": True,
                "docker_images_never_deleted": True,
                "host_docker_fallback": False,
            },
        }
        inventory["inventory_sha256"] = _canonical_sha256(
            {
                "metadata_targets": targets,
                "instances": instances,
                "docker": docker,
                "protected_images": inventory["protected_images"],
            }
        )
        audit.mkdir(parents=True, mode=0o700)
        self._write_json(audit / "inventory.json", inventory)
        state = (
            "PARTIAL" if any(item.get("status") != "PASS" for item in docker.values()) else "PASS"
        )
        if any(
            item.get("status") == "PASS" and item.get("active_containers")
            for item in docker.values()
        ):
            state = "BLOCKED"
        return {
            "status": state,
            "operation": "runtime retire-p8 plan",
            "run_id": run,
            "inventory": str((audit / "inventory.json").relative_to(self.project_root)),
            "metadata_candidate_count": len(targets),
            "docker_project_count": len(docker),
        }

    def _inventory(self, run: str) -> tuple[Path, dict[str, Any]]:
        audit = self._audit(run)
        inventory = _read_json(audit / "inventory.json", code="P8_RETIREMENT_INVENTORY")
        if inventory.get("schema_version") != SCHEMA or inventory.get("run_id") != run:
            raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
        payload = {
            "metadata_targets": inventory.get("metadata_targets"),
            "instances": inventory.get("instances"),
            "docker": inventory.get("docker"),
            "protected_images": inventory.get("protected_images"),
        }
        if inventory.get("inventory_sha256") != _canonical_sha256(payload):
            raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_TAMPERED")
        if (
            not isinstance(payload["metadata_targets"], list)
            or not isinstance(payload["instances"], list)
            or not isinstance(payload["docker"], dict)
        ):
            raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
        return audit, inventory

    def _external_directory(self, path: str | Path, *, code: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise P8RetirementRefusal(f"{code}_INVALID")
        if candidate.is_symlink() or not candidate.is_dir():
            raise P8RetirementRefusal(f"{code}_INVALID")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise P8RetirementRefusal(f"{code}_INVALID") from error
        if resolved.is_relative_to(self.project_root.resolve()):
            raise P8RetirementRefusal(f"{code}_INSIDE_PROJECT")
        current = resolved.anchor and Path(resolved.anchor)
        # Verify every named component; a symlink anywhere invalidates an
        # external backup as a recovery authority.
        for part in resolved.parts[1:]:
            assert current is not None
            current /= part
            if current.is_symlink():
                raise P8RetirementRefusal(f"{code}_SYMLINK")
        return resolved

    def _external_file(self, path: str | Path, *, code: str) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise P8RetirementRefusal(f"{code}_INVALID")
        if candidate.is_symlink() or not candidate.is_file():
            raise P8RetirementRefusal(f"{code}_INVALID")
        parent = self._external_directory(candidate.parent, code=code)
        resolved = candidate.resolve(strict=True)
        if resolved.parent != parent:
            raise P8RetirementRefusal(f"{code}_SYMLINK")
        return resolved

    def backup_verify(
        self,
        run_id: str | None,
        backup_root: str | Path | None,
        secret_escrow_receipt: str | Path | None,
    ) -> dict[str, Any]:
        run = _safe_run_id(run_id)
        audit, inventory = self._inventory(run)
        if backup_root is None or secret_escrow_receipt is None:
            raise P8RetirementRefusal("P8_EXTERNAL_BACKUP_OR_ESCROW_REQUIRED")
        backup = self._external_directory(backup_root, code="P8_BACKUP_ROOT")
        receipt = self._external_file(secret_escrow_receipt, code="P8_ESCROW_RECEIPT")
        manifest = _read_json(backup / "backup-manifest.json", code="P8_BACKUP_MANIFEST")
        restore = _read_json(backup / "restore-evidence.json", code="P8_RESTORE_EVIDENCE")
        escrow = _read_json(receipt, code="P8_ESCROW_RECEIPT")
        backup_id = manifest.get("backup_id")
        if (
            manifest.get("schema_version") != BACKUP_SCHEMA
            or manifest.get("status") != "PASSED"
            or manifest.get("run_id") != run
            or not isinstance(backup_id, str)
            or not backup_id
            or not REQUIRED_BACKUP_COVERAGE <= set(manifest.get("coverage", []))
        ):
            raise P8RetirementRefusal("P8_BACKUP_NOT_VERIFIED")
        if (
            restore.get("schema_version") != RESTORE_SCHEMA
            or restore.get("status") != "PASSED"
            or restore.get("run_id") != run
            or restore.get("backup_id") != backup_id
        ):
            raise P8RetirementRefusal("P8_RESTORE_EXERCISE_NOT_VERIFIED")
        if (
            escrow.get("schema_version") != ESCROW_SCHEMA
            or escrow.get("status") != "AVAILABLE"
            or escrow.get("backup_id") != backup_id
            or not isinstance(escrow.get("escrow_id"), str)
            or not escrow.get("escrow_id")
            or not isinstance(escrow.get("issued_at"), str)
            or not escrow.get("issued_at")
            or not isinstance(escrow.get("verification_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", escrow["verification_sha256"])
            or not REQUIRED_ESCROW_COVERAGE <= set(escrow.get("coverage", []))
        ):
            raise P8RetirementRefusal("P8_SECRET_ESCROW_NOT_VERIFIED")
        output = audit / "backup-verification.json"
        if output.exists() or output.is_symlink():
            raise P8RetirementRefusal("P8_BACKUP_VERIFICATION_ALREADY_EXISTS")
        record = {
            "schema_version": SCHEMA,
            "run_id": run,
            "operation": "backup-verify",
            "inventory_sha256": inventory["inventory_sha256"],
            "backup_id": backup_id,
            "backup_root_sha256": _canonical_sha256(
                {
                    "backup_manifest": _file_sha256(backup / "backup-manifest.json"),
                    "restore_evidence": _file_sha256(backup / "restore-evidence.json"),
                }
            ),
            "secret_escrow": {
                "escrow_id": escrow["escrow_id"],
                "issued_at": escrow["issued_at"],
                "verification_sha256": escrow["verification_sha256"],
                "receipt_sha256": _file_sha256(receipt),
                "coverage": sorted(REQUIRED_ESCROW_COVERAGE),
            },
            "note": "Backup locations and secret values are intentionally not recorded.",
        }
        self._write_json(output, record)
        return {
            "status": "PASS",
            "operation": "runtime retire-p8 backup-verify",
            "run_id": run,
            "backup_id": backup_id,
            "secret_escrow_id": escrow["escrow_id"],
        }

    def _backup_verification(
        self, audit: Path, run: str, inventory: dict[str, Any]
    ) -> dict[str, Any]:
        record = _read_json(audit / "backup-verification.json", code="P8_BACKUP_VERIFICATION")
        if (
            record.get("schema_version") != SCHEMA
            or record.get("run_id") != run
            or record.get("inventory_sha256") != inventory.get("inventory_sha256")
        ):
            raise P8RetirementRefusal("P8_BACKUP_VERIFICATION_STALE")
        return record

    def _revalidate_runtime(self, inventory: dict[str, Any]) -> None:
        targets, instances, projects = self._runtime_targets()
        expected = {
            "metadata_targets": inventory.get("metadata_targets"),
            "instances": inventory.get("instances"),
        }
        current = {"metadata_targets": targets, "instances": instances}
        if _canonical_sha256(current) != _canonical_sha256(expected):
            raise P8RetirementRefusal("P8_RUNTIME_HASH_CHANGED")
        for project in sorted(set(projects.values())):
            state = self._docker_state(project)
            if state.get("status") != "PASS":
                raise P8RetirementRefusal("PROJECT_DOCKER_UNAVAILABLE")
            if state.get("active_containers"):
                raise P8RetirementRefusal("P8_ACTIVE_CONTAINER_PRESENT")

    @staticmethod
    def _safe_child(root: Path, relative: str, *, code: str) -> Path:
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts:
            raise P8RetirementRefusal(f"{code}_INVALID")
        target = root / path
        if target.is_symlink() or not target.is_relative_to(root):
            raise P8RetirementRefusal(f"{code}_INVALID")
        return target

    def stage(self, run_id: str | None) -> dict[str, Any]:
        run = _safe_run_id(run_id)
        audit, inventory = self._inventory(run)
        self._backup_verification(audit, run, inventory)
        self._assert_roots()
        self._revalidate_runtime(inventory)
        quarantine = self._quarantine(run)
        stage_path = audit / "stage.json"
        if (
            quarantine.exists()
            or quarantine.is_symlink()
            or stage_path.exists()
            or stage_path.is_symlink()
        ):
            raise P8RetirementRefusal("P8_RETIREMENT_STAGE_ALREADY_EXISTS")
        copied: list[dict[str, Any]] = []
        quarantine.mkdir(parents=True, mode=0o700)
        try:
            for target in inventory["metadata_targets"]:
                if not isinstance(target, dict) or not target.get("stageable"):
                    continue
                relative = target.get("relative")
                if not isinstance(relative, str):
                    raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
                source = self._safe_child(self.p8_root, relative, code="P8_SOURCE_TARGET")
                destination = self._safe_child(quarantine, relative, code="P8_QUARANTINE_TARGET")
                if source.is_symlink() or not source.is_dir():
                    raise P8RetirementRefusal("P8_SOURCE_TARGET_INVALID")
                shutil.copytree(source, destination, symlinks=False)
                copied_summary = _tree_summary(destination, code="P8_QUARANTINE_TARGET")
                if copied_summary.sha256 != target.get("tree_sha256"):
                    raise P8RetirementRefusal("P8_STAGE_COPY_HASH_MISMATCH")
                copied.append(
                    {
                        "source_relative": relative,
                        "archive_relative": relative,
                        "tree_sha256": copied_summary.sha256,
                    }
                )
            metadata_dir = quarantine / "instances"
            metadata_dir.mkdir(parents=True, exist_ok=True)
            for instance in inventory["instances"]:
                if not isinstance(instance, dict):
                    raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
                name = _safe_name(instance.get("instance"), code="P8_INSTANCE_NAME")
                target = metadata_dir / f"{name}.metadata.json"
                target.write_text(
                    json.dumps(instance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                os.chmod(target, 0o600)
                copied.append(
                    {
                        "source_relative": f"instances/{name}",
                        "archive_relative": str(target.relative_to(quarantine)),
                        "tree_sha256": _file_sha256(target),
                        "kind": "redacted-instance-metadata",
                    }
                )
            stage = {
                "schema_version": SCHEMA,
                "run_id": run,
                "operation": "stage",
                "inventory_sha256": inventory["inventory_sha256"],
                "copies": copied,
                "source_mutated": False,
            }
            self._write_json(stage_path, stage)
        except Exception:
            # Keep a complete quarantine for inspection if a copy was made;
            # never erase evidence of an interrupted retirement attempt.
            raise
        return {
            "status": "PASS",
            "operation": "runtime retire-p8 stage",
            "run_id": run,
            "copied_records": len(copied),
        }

    def verify(self, run_id: str | None) -> dict[str, Any]:
        run = _safe_run_id(run_id)
        audit, inventory = self._inventory(run)
        self._backup_verification(audit, run, inventory)
        stage = _read_json(audit / "stage.json", code="P8_RETIREMENT_STAGE")
        if (
            stage.get("schema_version") != SCHEMA
            or stage.get("run_id") != run
            or stage.get("inventory_sha256") != inventory.get("inventory_sha256")
            or not isinstance(stage.get("copies"), list)
        ):
            raise P8RetirementRefusal("P8_RETIREMENT_STAGE_INVALID")
        self._assert_roots()
        self._revalidate_runtime(inventory)
        quarantine = self._quarantine(run)
        if quarantine.is_symlink() or not quarantine.is_dir():
            raise P8RetirementRefusal("P8_QUARANTINE_MISSING")
        for record in stage["copies"]:
            if not isinstance(record, dict):
                raise P8RetirementRefusal("P8_RETIREMENT_STAGE_INVALID")
            archive_relative = record.get("archive_relative")
            if not isinstance(archive_relative, str):
                raise P8RetirementRefusal("P8_RETIREMENT_STAGE_INVALID")
            archive = self._safe_child(quarantine, archive_relative, code="P8_QUARANTINE_RECORD")
            if record.get("kind") == "redacted-instance-metadata":
                if (
                    archive.is_symlink()
                    or not archive.is_file()
                    or _file_sha256(archive) != record.get("tree_sha256")
                ):
                    raise P8RetirementRefusal("P8_QUARANTINE_HASH_CHANGED")
            else:
                if (
                    archive.is_symlink()
                    or not archive.is_dir()
                    or _tree_summary(archive, code="P8_QUARANTINE_RECORD").sha256
                    != record.get("tree_sha256")
                ):
                    raise P8RetirementRefusal("P8_QUARANTINE_HASH_CHANGED")
        verification = {
            "schema_version": SCHEMA,
            "run_id": run,
            "operation": "verify",
            "inventory_sha256": inventory["inventory_sha256"],
            "stage_sha256": _canonical_sha256(stage["copies"]),
            "status": "PASSED",
        }
        path = audit / "verification.json"
        if path.exists() or path.is_symlink():
            raise P8RetirementRefusal("P8_RETIREMENT_VERIFICATION_ALREADY_EXISTS")
        self._write_json(path, verification)
        return {
            "status": "PASS",
            "operation": "runtime retire-p8 verify",
            "run_id": run,
            "copied_records": len(stage["copies"]),
        }

    def _verification(self, audit: Path, run: str, inventory: dict[str, Any]) -> dict[str, Any]:
        record = _read_json(audit / "verification.json", code="P8_RETIREMENT_VERIFICATION")
        if (
            record.get("schema_version") != SCHEMA
            or record.get("run_id") != run
            or record.get("status") != "PASSED"
            or record.get("inventory_sha256") != inventory.get("inventory_sha256")
        ):
            raise P8RetirementRefusal("P8_RETIREMENT_VERIFICATION_STALE")
        return record

    def _docker_command(self, arguments: list[str]) -> None:
        """Run one exact V7-rootless deletion command; never apply a prune."""
        try:
            require_socket()
            result = subprocess.run(
                [docker_executable(), *arguments],
                check=False,
                capture_output=True,
                text=True,
                env=child_environment(),
            )
        except (EnvironmentRefusal, OSError) as error:
            raise P8RetirementRefusal("PROJECT_DOCKER_UNAVAILABLE") from error
        if result.returncode:
            raise P8RetirementRefusal("P8_DOCKER_DELETE_FAILED")

    def purge(self, run_id: str | None, scope: str | None) -> dict[str, Any]:
        run = _safe_run_id(run_id)
        if scope not in {"metadata", "docker-data"}:
            raise P8RetirementRefusal("P8_PURGE_SCOPE_INVALID")
        audit, inventory = self._inventory(run)
        self._backup_verification(audit, run, inventory)
        self._verification(audit, run, inventory)
        receipt = audit / f"purge-{scope}-receipt.json"
        if receipt.exists() or receipt.is_symlink():
            raise P8RetirementRefusal("P8_PURGE_ALREADY_EXISTS")
        self._assert_roots()
        if scope == "metadata":
            self._revalidate_runtime(inventory)
        removed: list[str] = []
        if scope == "metadata":
            # The plan is the only source of names.  Recheck each exact tree
            # digest before deletion; secrets are never copied locally but are
            # covered by the independently verified external escrow record.
            for target in inventory["metadata_targets"]:
                if not isinstance(target, dict) or not isinstance(target.get("relative"), str):
                    raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
                path = self._safe_child(self.p8_root, target["relative"], code="P8_PURGE_TARGET")
                if path.is_symlink() or not path.is_dir():
                    raise P8RetirementRefusal("P8_PURGE_TARGET_MISSING")
                if _tree_summary(path, code="P8_PURGE_TARGET").sha256 != target.get("tree_sha256"):
                    raise P8RetirementRefusal("P8_PURGE_TARGET_HASH_CHANGED")
            for target in inventory["metadata_targets"]:
                path = self._safe_child(
                    self.p8_root, str(target["relative"]), code="P8_PURGE_TARGET"
                )
                shutil.rmtree(path)
                removed.append(str(target["relative"]))
        else:
            for project, expected in sorted(inventory["docker"].items()):
                if not isinstance(expected, dict):
                    raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
                current = self._docker_state(project)
                if current.get("status") != "PASS" or current.get("active_containers"):
                    raise P8RetirementRefusal("P8_DOCKER_STATE_CHANGED")
                if current.get("stopped_containers") != expected.get(
                    "stopped_containers"
                ) or current.get("volumes") != expected.get("volumes"):
                    raise P8RetirementRefusal("P8_DOCKER_CANDIDATES_CHANGED")
                for identifier in expected.get("stopped_containers", []):
                    if not isinstance(identifier, str):
                        raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
                    self._docker_command(["container", "rm", identifier])
                    removed.append(f"container:{identifier}")
                for volume in expected.get("volumes", []):
                    if not isinstance(volume, str):
                        raise P8RetirementRefusal("P8_RETIREMENT_INVENTORY_INVALID")
                    self._docker_command(["volume", "rm", volume])
                    removed.append(f"volume:{volume}")
        self._write_json(
            receipt,
            {
                "schema_version": SCHEMA,
                "run_id": run,
                "operation": "purge",
                "scope": scope,
                "removed": removed,
                "images_removed": [],
                "note": (
                    "No Docker images, Docker data roots, global prune operations, "
                    "or secret values are removed by this receipt."
                ),
            },
        )
        return {
            "status": "PASS",
            "operation": "runtime retire-p8 purge",
            "run_id": run,
            "scope": scope,
            "removed": removed,
            "images_removed": [],
        }


def retire(
    action: str,
    run_id: str | None,
    *,
    backup_root: str | Path | None = None,
    secret_escrow_receipt: str | Path | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Public fixed-target dispatcher for ``bin/ops runtime retire-p8``."""
    service = P8Retirement()
    if action == "plan":
        return service.plan(run_id)
    if action == "backup-verify":
        return service.backup_verify(run_id, backup_root, secret_escrow_receipt)
    if action == "stage":
        return service.stage(run_id)
    if action == "verify":
        return service.verify(run_id)
    if action == "purge":
        return service.purge(run_id, scope)
    raise P8RetirementRefusal("P8_RETIREMENT_ACTION_INVALID")
