#!/usr/bin/env python3
"""Policy-neutral implementation of the auditable runtime-cleanup lifecycle.

This module deliberately knows nothing about a project's candidate list or
release model.  A project-local policy supplies those decisions while this
core owns the safe plan, stage, verify, and purge mechanics.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

try:  # Support both ``python -m`` and the documented direct maintenance entrypoint.
    from .cleanup_protocol import CLEANUP_PROTOCOL_VERSION
except ImportError:  # pragma: no cover - exercised by the direct CLI entrypoint
    from cleanup_protocol import CLEANUP_PROTOCOL_VERSION

RUN_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-\d{1,3})?$")
CANDIDATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DOCKER_OBJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{11,127}")
TEXT_SUFFIXES = frozenset({".json", ".py", ".sh", ".toml", ".yaml", ".yml"})
SKIPPED_REFERENCE_NAMES = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
)


class CleanupRefusalError(RuntimeError):
    """Raised when a cleanup boundary cannot be proved safe."""


CleanupRefusal = CleanupRefusalError


@dataclass(frozen=True)
class CandidateRule:
    candidate_id: str
    relative_path: str
    category: str
    rebuild_commands: tuple[str, ...]
    eligibility: str = "ELIGIBLE"
    reason: str | None = None
    allow_internal_links: bool = False


class CleanupPolicy(Protocol):
    """Project-owned decisions consumed by :class:`CleanupCore`."""

    def protected_paths(self) -> dict[str, str]: ...

    def rebuild_recipe(self, rule: CandidateRule) -> str | None: ...

    def rules(self) -> tuple[CandidateRule, ...]: ...

    def reference_roots(self) -> tuple[Path, ...]: ...

    def excluded_reference_directories(self, root: Path, current: Path) -> frozenset[str]: ...

    def reconstruct_rule(
        self, candidate_id: str, planned: dict[str, Any] | None
    ) -> CandidateRule | None: ...

    def docker_block_reason(self) -> str | None: ...

    def docker_inventory_reason(self, block_reason: str | None) -> str: ...

    def protected_compose_projects(self) -> set[str]: ...


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _safe_run_id(value: str | None, *, default: bool) -> str:
    run_id = value or (date.today().isoformat() if default else "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise CleanupRefusal("RUN_ID_INVALID: use YYYY-MM-DD or YYYY-MM-DD-N")
    return run_id


def _safe_candidate_id(value: str | None) -> str:
    if not value or not CANDIDATE_ID_PATTERN.fullmatch(value):
        raise CleanupRefusal("CANDIDATE_ID_INVALID: use an ID from this run's plan")
    return value


class CleanupCore:
    """Safe mechanics for one project-local cleanup policy."""

    protocol_version = CLEANUP_PROTOCOL_VERSION

    def __init__(
        self,
        project_root: Path,
        source_root: Path,
        *,
        project: str,
        policy: CleanupPolicy,
    ) -> None:
        self.project_root = project_root.resolve()
        self.source_root = source_root.resolve()
        self.project = project
        self.policy = policy
        if not self.source_root.is_relative_to(self.project_root):
            raise CleanupRefusal("SOURCE_ROOT_OUTSIDE_PROJECT")

    @property
    def audit_root(self) -> Path:
        return self.project_root / "operations/maintenance/cleanup"

    @property
    def quarantine_root(self) -> Path:
        return self.project_root / "runtime/archive/cleanup-quarantine"

    def protected_paths(self) -> dict[str, str]:
        return self.policy.protected_paths()

    def rules(self) -> tuple[CandidateRule, ...]:
        return self.policy.rules()

    def _path(self, relative: str) -> Path:
        raw = Path(relative)
        if not relative or raw.is_absolute() or ".." in raw.parts:
            raise CleanupRefusal("PATH_ESCAPE_REJECTED")
        candidate = self.project_root / raw
        try:
            candidate.relative_to(self.project_root)
        except ValueError as error:
            raise CleanupRefusal("PATH_ESCAPE_REJECTED") from error
        self._assert_no_symlink_ancestor(candidate)
        return candidate

    def _assert_no_symlink_ancestor(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError as error:
            raise CleanupRefusal("PATH_ESCAPE_REJECTED") from error
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if not current.exists() and not current.is_symlink():
                break
            if current.is_symlink():
                raise CleanupRefusal(f"SYMLINK_PATH_REJECTED:{relative.as_posix()}")

    def _protected_reason(self, path: Path) -> str | None:
        relative = path.relative_to(self.project_root).as_posix()
        for protected, reason in self.protected_paths().items():
            if relative == protected or relative.startswith(protected + "/"):
                return reason
        return None

    def _tree_manifest(self, path: Path, *, allow_internal_links: bool) -> dict[str, Any]:
        self._assert_no_symlink_ancestor(path)
        root_stat = path.lstat()
        if stat.S_ISLNK(root_stat.st_mode):
            raise CleanupRefusal("SYMLINK_CANDIDATE_REJECTED")
        if not (stat.S_ISDIR(root_stat.st_mode) or stat.S_ISREG(root_stat.st_mode)):
            raise CleanupRefusal("UNSUPPORTED_CANDIDATE_TYPE")
        if stat.S_ISREG(root_stat.st_mode) and root_stat.st_nlink != 1:
            raise CleanupRefusal("HARDLINK_CANDIDATE_REJECTED")

        entries: list[dict[str, Any]] = []
        total_bytes = 0
        file_count = 0
        root_resolved = path.resolve()

        def add_file(candidate: Path, relative: Path, metadata: os.stat_result) -> None:
            nonlocal total_bytes, file_count
            if metadata.st_nlink != 1:
                raise CleanupRefusal(f"HARDLINK_REJECTED:{relative.as_posix()}")
            size = metadata.st_size
            entries.append(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "size_bytes": size,
                    "sha256": _sha256_file(candidate),
                }
            )
            total_bytes += size
            file_count += 1

        def walk(directory: Path) -> None:
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                relative = child.relative_to(path)
                metadata = child.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    if not allow_internal_links:
                        raise CleanupRefusal(f"SYMLINK_REJECTED:{relative.as_posix()}")
                    target = (child.parent / os.readlink(child)).resolve(strict=False)
                    if not target.is_relative_to(root_resolved):
                        raise CleanupRefusal(f"SYMLINK_ESCAPE_REJECTED:{relative.as_posix()}")
                    entries.append(
                        {
                            "path": relative.as_posix(),
                            "type": "internal-symlink",
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "target_sha256": hashlib.sha256(
                                os.readlink(child).encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                elif stat.S_ISDIR(metadata.st_mode):
                    entries.append(
                        {
                            "path": relative.as_posix(),
                            "type": "directory",
                            "mode": stat.S_IMODE(metadata.st_mode),
                        }
                    )
                    walk(child)
                elif stat.S_ISREG(metadata.st_mode):
                    add_file(child, relative, metadata)
                else:
                    raise CleanupRefusal(f"UNSUPPORTED_ENTRY_REJECTED:{relative.as_posix()}")

        if stat.S_ISREG(root_stat.st_mode):
            add_file(path, Path(path.name), root_stat)
        else:
            entries.append(
                {"path": ".", "type": "directory", "mode": stat.S_IMODE(root_stat.st_mode)}
            )
            walk(path)
        return {
            "tree_sha256": _digest_json(entries),
            "file_count": file_count,
            "size_bytes": total_bytes,
            "entries": entries,
        }

    def _reference_status(self, candidate: Path) -> tuple[str, int]:
        """Inspect active executable/configuration text without recording content."""
        needle = candidate.relative_to(self.project_root).as_posix()
        count = 0
        for root in self.policy.reference_roots():
            if not root.exists() or root.is_symlink():
                continue
            for directory, names, filenames in os.walk(root, followlinks=False):
                current = Path(directory)
                excluded = self.policy.excluded_reference_directories(root, current)
                names[:] = [
                    name
                    for name in names
                    if name not in SKIPPED_REFERENCE_NAMES and name not in excluded
                ]
                for filename in filenames:
                    path = current / filename
                    if path.suffix not in TEXT_SUFFIXES or path.is_symlink():
                        continue
                    try:
                        if needle in path.read_text(encoding="utf-8", errors="replace"):
                            count += 1
                    except OSError:
                        count += 1
        layout = self.project_root / "project-layout.json"
        if layout.is_file() and not layout.is_symlink():
            if needle in layout.read_text(encoding="utf-8", errors="replace"):
                count += 1
        return ("REFERENCED" if count else "UNREFERENCED", count)

    def _run_root(self, run_id: str) -> Path:
        return self.audit_root / run_id

    def _quarantine_path(self, run_id: str, candidate_id: str) -> Path:
        _safe_candidate_id(candidate_id)
        path = self.quarantine_root / run_id / candidate_id
        self._assert_no_symlink_ancestor(path)
        return path

    @staticmethod
    def _write_new(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.exists() or path.is_symlink():
            raise CleanupRefusal(f"AUDIT_RECORD_EXISTS_OR_UNSAFE:{path.name}")
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            finally:
                raise

    @staticmethod
    def _read_record(path: Path) -> dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            raise CleanupRefusal(f"AUDIT_RECORD_MISSING_OR_UNSAFE:{path.name}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise CleanupRefusal(f"AUDIT_RECORD_INVALID:{path.name}")
        return payload

    def _candidate_records(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        hash_records: dict[str, Any] = {}
        seen: set[str] = set()
        for rule in self.rules():
            if rule.candidate_id in seen:
                raise CleanupRefusal(f"DUPLICATE_CANDIDATE_ID:{rule.candidate_id}")
            seen.add(rule.candidate_id)
            record: dict[str, Any] = {
                "candidate_id": rule.candidate_id,
                "category": rule.category,
                "source_relative": rule.relative_path,
                "rebuild_commands": list(rule.rebuild_commands),
                "allows_internal_links": rule.allow_internal_links,
                "eligible": False,
                "reference_status": "NOT_CHECKED",
                "reference_file_count": 0,
                "protected_reason": None,
            }
            try:
                path = self._path(rule.relative_path)
            except CleanupRefusal as error:
                record["eligibility_reason"] = str(error)
                candidates.append(record)
                continue
            if not path.exists() and not path.is_symlink():
                continue
            record["protected_reason"] = self._protected_reason(path)
            if record["protected_reason"]:
                record["eligibility_reason"] = "PROTECTED_PATH"
                candidates.append(record)
                continue
            try:
                manifest = self._tree_manifest(path, allow_internal_links=rule.allow_internal_links)
            except CleanupRefusal as error:
                record["eligibility_reason"] = str(error)
                candidates.append(record)
                continue
            reference_status, reference_count = self._reference_status(path)
            record.update(
                {
                    "size_bytes": manifest["size_bytes"],
                    "file_count": manifest["file_count"],
                    "tree_sha256": manifest["tree_sha256"],
                    "reference_status": reference_status,
                    "reference_file_count": reference_count,
                }
            )
            hash_records[rule.candidate_id] = {
                "source_relative": rule.relative_path,
                "tree_sha256": manifest["tree_sha256"],
                "entries": manifest["entries"],
            }
            if rule.eligibility != "ELIGIBLE":
                record["eligibility_reason"] = rule.reason or rule.eligibility
            elif reference_status != "UNREFERENCED":
                record["eligibility_reason"] = "ACTIVE_REFERENCE_REJECTED"
            else:
                record["eligible"] = True
                record["eligibility_reason"] = "ELIGIBLE"
            candidates.append(record)
        return candidates, hash_records

    def _docker_wrapper(self) -> Path:
        layout_path = self.project_root / "project-layout.json"
        try:
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            socket = Path(str(layout["runtime"]["docker_socket"]))
        except (KeyError, OSError, TypeError, json.JSONDecodeError):
            raise CleanupRefusal("DOCKER_LAYOUT_INVALID") from None
        docker = self.project_root / "bin/docker"
        if docker.is_symlink() or not docker.is_file() or not os.access(docker, os.X_OK):
            raise CleanupRefusal("PROJECT_DOCKER_WRAPPER_MISSING")
        if socket.is_symlink():
            raise CleanupRefusal("PROJECT_DOCKER_SOCKET_SYMLINK")
        try:
            socket_mode = socket.stat().st_mode
        except OSError:
            raise CleanupRefusal("PROJECT_DOCKER_SOCKET_UNAVAILABLE") from None
        if not stat.S_ISSOCK(socket_mode):
            raise CleanupRefusal("PROJECT_DOCKER_SOCKET_INVALID")
        return docker

    def _run_docker(
        self, arguments: list[str], *, timeout: int = 15
    ) -> subprocess.CompletedProcess[str]:
        docker = self._docker_wrapper()
        try:
            return subprocess.run(
                [str(docker), *arguments],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CleanupRefusal("PROJECT_DOCKER_COMMAND_UNAVAILABLE") from error

    def _docker_is_frozen(self) -> bool:
        """Compatibility helper for callers; policy owns the actual decision."""
        return self.policy.docker_block_reason() is not None

    @staticmethod
    def _safe_docker_object_id(value: object) -> str:
        if not isinstance(value, str) or not DOCKER_OBJECT_ID_PATTERN.fullmatch(value):
            raise CleanupRefusal("DOCKER_OBJECT_ID_INVALID")
        return value

    @staticmethod
    def _docker_candidate_id(kind: str, object_id: str) -> str:
        digest = hashlib.sha256(object_id.encode("utf-8")).hexdigest()[:16]
        return f"docker.{kind}.{digest}"

    def _protected_compose_projects(self) -> set[str]:
        return self.policy.protected_compose_projects()

    def _docker_container_candidate(
        self,
        object_id: str,
        status: str,
        mount_types: list[str],
        release_reference: bool,
    ) -> dict[str, Any]:
        state = status.lower()
        protected_reason = self.policy.docker_block_reason()
        if protected_reason is None and state not in {"exited", "created", "dead"}:
            protected_reason = "CONTAINER_NOT_STOPPED"
        elif protected_reason is None and mount_types:
            protected_reason = "CONTAINER_MOUNTED_STATE"
        elif protected_reason is None and release_reference:
            protected_reason = "CANDIDATE_OR_RELEASE_REFERENCE"
        snapshot = {
            "kind": "container",
            "object_id": object_id,
            "state": state,
            "mount_types": mount_types,
            "release_reference": release_reference,
        }
        return {
            "candidate_id": self._docker_candidate_id("container", object_id),
            "storage_kind": "docker",
            "docker_kind": "container",
            "docker_object_id": object_id,
            "category": "stopped-container",
            "eligible": protected_reason is None,
            "eligibility_reason": "ELIGIBLE" if protected_reason is None else protected_reason,
            "protected_reason": protected_reason,
            "reference_status": "UNREFERENCED" if not mount_types else "REFERENCED",
            "reference_file_count": 0,
            "state_sha256": _digest_json(snapshot),
            "rebuild_commands": [],
        }

    def _docker_image_candidate(self, object_id: str) -> dict[str, Any]:
        protected_reason = self.policy.docker_block_reason()
        snapshot = {"kind": "image", "object_id": object_id, "dangling": True}
        return {
            "candidate_id": self._docker_candidate_id("image", object_id),
            "storage_kind": "docker",
            "docker_kind": "image",
            "docker_object_id": object_id,
            "category": "unreferenced-dangling-image",
            "eligible": protected_reason is None,
            "eligibility_reason": "ELIGIBLE" if protected_reason is None else protected_reason,
            "protected_reason": protected_reason,
            "reference_status": "UNREFERENCED",
            "reference_file_count": 0,
            "state_sha256": _digest_json(snapshot),
            "rebuild_commands": [],
        }

    def _docker_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        protected_projects = self._protected_compose_projects()
        containers = self._run_docker(
            ["ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Status}}"]
        )
        if containers.returncode == 0:
            for line in containers.stdout.splitlines():
                object_id, separator, raw_status = line.partition("\t")
                if not separator:
                    continue
                try:
                    object_id = self._safe_docker_object_id(object_id)
                except CleanupRefusal:
                    continue
                inspect = self._run_docker(
                    [
                        "container",
                        "inspect",
                        "--format",
                        "{{range .Mounts}}{{.Type}},{{end}}",
                        object_id,
                    ]
                )
                if inspect.returncode != 0:
                    continue
                mount_types = sorted(
                    {value for value in inspect.stdout.strip().split(",") if value}
                )
                label = self._run_docker(
                    [
                        "container",
                        "inspect",
                        "--format",
                        '{{ index .Config.Labels "com.docker.compose.project" }}',
                        object_id,
                    ]
                )
                release_reference = (
                    label.returncode == 0 and label.stdout.strip() in protected_projects
                )
                candidates.append(
                    self._docker_container_candidate(
                        object_id,
                        raw_status.split(maxsplit=1)[0],
                        mount_types,
                        release_reference,
                    )
                )
        images = self._run_docker(
            ["image", "ls", "--filter", "dangling=true", "--quiet", "--no-trunc"]
        )
        if images.returncode == 0:
            for value in images.stdout.splitlines():
                try:
                    candidates.append(
                        self._docker_image_candidate(self._safe_docker_object_id(value))
                    )
                except CleanupRefusal:
                    continue
        return candidates

    def _docker_object_manifest(self) -> dict[str, list[dict[str, Any]]]:
        manifest: dict[str, list[dict[str, Any]]] = {
            "containers": [],
            "images": [],
            "volumes": [],
        }
        containers = self._run_docker(
            ["ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Image}}\t{{.Status}}"]
        )
        if containers.returncode == 0:
            for line in containers.stdout.splitlines():
                object_id, separator, remainder = line.partition("\t")
                image, separator, raw_status = remainder.partition("\t")
                if not separator:
                    continue
                try:
                    object_id = self._safe_docker_object_id(object_id)
                except CleanupRefusal:
                    continue
                manifest["containers"].append(
                    {
                        "container_id": object_id,
                        "image_reference_sha256": hashlib.sha256(image.encode("utf-8")).hexdigest(),
                        "status": raw_status.split(maxsplit=1)[0].lower(),
                    }
                )
        images = self._run_docker(
            [
                "image",
                "ls",
                "--digests",
                "--no-trunc",
                "--format",
                "{{.ID}}\t{{.Digest}}",
            ]
        )
        if images.returncode == 0:
            for line in images.stdout.splitlines():
                object_id, separator, digest = line.partition("\t")
                if not separator:
                    continue
                try:
                    object_id = self._safe_docker_object_id(object_id)
                except CleanupRefusal:
                    continue
                manifest["images"].append(
                    {"image_id": object_id, "digest": None if digest == "<none>" else digest}
                )
        volumes = self._run_docker(["volume", "ls", "--format", "{{.Name}}\t{{.Driver}}"])
        if volumes.returncode == 0:
            for line in volumes.stdout.splitlines():
                name, separator, driver = line.partition("\t")
                if separator:
                    manifest["volumes"].append(
                        {
                            "volume_name_sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                            "driver": driver,
                        }
                    )
        return manifest

    def _docker_inventory(self) -> dict[str, Any]:
        try:
            self._docker_wrapper()
        except CleanupRefusal as error:
            return {"status": "UNAVAILABLE", "reason": str(error), "candidates": []}
        commands = (
            ("system-df", ["system", "df", "-v"]),
            ("containers", ["ps", "-a", "--no-trunc"]),
            ("images", ["image", "ls", "--digests", "--no-trunc"]),
            ("volumes", ["volume", "ls"]),
        )
        inventory: dict[str, Any] = {"status": "INVENTORIED", "commands": {}}
        for name, command in commands:
            try:
                completed = self._run_docker(command)
            except CleanupRefusal:
                inventory["commands"][name] = {"status": "UNAVAILABLE"}
                continue
            inventory["commands"][name] = {
                "status": "PASS" if completed.returncode == 0 else "FAILED",
                "exit_code": completed.returncode,
                "line_count": len(completed.stdout.splitlines()),
                "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
            }
        try:
            inventory["candidates"] = self._docker_candidates()
            inventory["object_manifest"] = self._docker_object_manifest()
        except CleanupRefusal:
            inventory["candidates"] = []
            inventory["object_manifest"] = {"containers": [], "images": [], "volumes": []}
            inventory["cleanup_status"] = "DEFERRED"
            inventory["reason"] = "PROJECT_DOCKER_CANDIDATE_SCAN_UNAVAILABLE"
            inventory["status"] = "PARTIAL"
            return inventory
        blocked = self.policy.docker_block_reason()
        inventory["candidate_count"] = len(inventory["candidates"])
        inventory["cleanup_status"] = "BLOCKED" if blocked else "REVIEW_REQUIRED"
        inventory["reason"] = self.policy.docker_inventory_reason(blocked)
        if any(item.get("status") != "PASS" for item in inventory["commands"].values()):
            inventory["status"] = "PARTIAL"
        return inventory

    def plan(self, run_id: str | None = None) -> dict[str, Any]:
        safe_run_id = _safe_run_id(run_id, default=True)
        run_root = self._run_root(safe_run_id)
        if run_root.exists() or run_root.is_symlink():
            raise CleanupRefusal("RUN_ID_ALREADY_EXISTS: choose a new YYYY-MM-DD-N run ID")
        self._assert_no_symlink_ancestor(run_root)
        run_root.mkdir(parents=True, mode=0o700)
        candidates, hashes = self._candidate_records()
        docker = self._docker_inventory()
        candidates.extend(docker.get("candidates", []))
        inventory = {
            "schema_version": "runtime-cleanup-inventory.v1",
            "project": self.project,
            "run_id": safe_run_id,
            "created_at": _now(),
            "candidates": candidates,
            "protected_paths": self.protected_paths(),
            "docker": docker,
            "note": "No source, runtime, Docker object, volume, or release state was changed.",
        }
        reference = {
            "schema_version": "runtime-cleanup-references.v1",
            "project": self.project,
            "run_id": safe_run_id,
            "created_at": _now(),
            "candidates": [
                {
                    key: candidate[key]
                    for key in (
                        "candidate_id",
                        "source_relative",
                        "reference_status",
                        "reference_file_count",
                        "protected_reason",
                        "eligibility_reason",
                    )
                    if key in candidate
                }
                for candidate in candidates
            ],
        }
        hashes_payload = {
            "schema_version": "runtime-cleanup-hashes.v1",
            "project": self.project,
            "run_id": safe_run_id,
            "created_at": _now(),
            "candidates": hashes,
        }
        self._write_new(run_root / "inventory.json", inventory)
        self._write_new(run_root / "reference-check.json", reference)
        self._write_new(run_root / "hashes.json", hashes_payload)
        return {
            "status": "PASS" if docker.get("status") == "INVENTORIED" else "PARTIAL",
            "operation": "cleanup plan",
            "run_id": safe_run_id,
            "audit_directory": str(run_root),
            "eligible_candidate_ids": [
                item["candidate_id"] for item in candidates if item.get("eligible")
            ],
            "candidate_count": len(candidates),
            "next": (
                "Review inventory.json, then use cleanup stage --run-id RUN "
                "--candidate ID --execute."
            ),
        }

    def _planned_candidate(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        inventory = self._read_record(self._run_root(run_id) / "inventory.json")
        if inventory.get("project") != self.project or inventory.get("run_id") != run_id:
            raise CleanupRefusal("AUDIT_PLAN_PROJECT_OR_RUN_MISMATCH")
        candidates = inventory.get("candidates")
        if not isinstance(candidates, list):
            raise CleanupRefusal("AUDIT_PLAN_CANDIDATES_INVALID")
        for item in candidates:
            if isinstance(item, dict) and item.get("candidate_id") == candidate_id:
                return item
        raise CleanupRefusal("CANDIDATE_NOT_IN_PLAN")

    def _live_rule(self, candidate_id: str, planned: dict[str, Any] | None = None) -> CandidateRule:
        for rule in self.rules():
            if rule.candidate_id == candidate_id:
                return rule
        rule = self.policy.reconstruct_rule(candidate_id, planned)
        if rule is None:
            raise CleanupRefusal("CANDIDATE_NOT_CURRENTLY_DEFINED")
        return rule

    def _revalidate_docker_candidate(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not plan.get("eligible") or plan.get("protected_reason"):
            raise CleanupRefusal("DOCKER_CANDIDATE_NOT_ELIGIBLE")
        blocked = self.policy.docker_block_reason()
        if blocked:
            raise CleanupRefusal(blocked)
        kind = plan.get("docker_kind")
        object_id = self._safe_docker_object_id(plan.get("docker_object_id"))
        if kind == "container":
            inspect = self._run_docker(
                [
                    "container",
                    "inspect",
                    "--format",
                    "{{.State.Status}}\t{{range .Mounts}}{{.Type}},{{end}}",
                    object_id,
                ]
            )
            if inspect.returncode != 0:
                raise CleanupRefusal("DOCKER_CONTAINER_NO_LONGER_AVAILABLE")
            state, separator, mounts = inspect.stdout.rstrip("\r\n").partition("\t")
            if not separator or state.lower() not in {"exited", "created", "dead"}:
                raise CleanupRefusal("DOCKER_CONTAINER_NO_LONGER_STOPPED")
            mount_types = sorted({value for value in mounts.split(",") if value})
            if mount_types:
                raise CleanupRefusal("DOCKER_CONTAINER_MOUNTED_STATE")
            label = self._run_docker(
                [
                    "container",
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "com.docker.compose.project" }}',
                    object_id,
                ]
            )
            if label.returncode == 0 and label.stdout.strip() in self._protected_compose_projects():
                raise CleanupRefusal("DOCKER_CONTAINER_RELEASE_REFERENCE")
            return {
                "kind": kind,
                "object_id": object_id,
                "state_sha256": _digest_json(
                    {
                        "kind": kind,
                        "object_id": object_id,
                        "state": state.lower(),
                        "mount_types": [],
                        "release_reference": False,
                    }
                ),
            }
        if kind == "image":
            dangling = self._run_docker(
                ["image", "ls", "--filter", "dangling=true", "--quiet", "--no-trunc"]
            )
            if dangling.returncode != 0 or object_id not in set(dangling.stdout.splitlines()):
                raise CleanupRefusal("DOCKER_IMAGE_NO_LONGER_DANGLING")
            inspect = self._run_docker(["image", "inspect", "--format", "{{.Id}}", object_id])
            if inspect.returncode != 0:
                raise CleanupRefusal("DOCKER_IMAGE_NO_LONGER_AVAILABLE")
            return {
                "kind": kind,
                "object_id": object_id,
                "state_sha256": _digest_json(
                    {"kind": kind, "object_id": object_id, "dangling": True}
                ),
            }
        raise CleanupRefusal("DOCKER_CANDIDATE_KIND_INVALID")

    def _stage_docker(self, run_id: str, candidate_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        state = self._revalidate_docker_candidate(plan)
        run_root = self._run_root(run_id)
        intent = {
            "schema_version": "runtime-cleanup-docker-stage-intent.v1",
            "project": self.project,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "docker_kind": state["kind"],
            "object_id_sha256": hashlib.sha256(state["object_id"].encode("utf-8")).hexdigest(),
            "state_sha256": state["state_sha256"],
            "created_at": _now(),
        }
        self._write_new(run_root / "stage-intent" / f"{candidate_id}.json", intent)
        isolation = intent | {
            "schema_version": "runtime-cleanup-docker-isolation.v1",
            "isolated_at": _now(),
            "isolation": "metadata-freeze; Docker object remains stopped and unchanged until purge",
        }
        self._write_new(run_root / "isolation" / f"{candidate_id}.json", isolation)
        return {
            "status": "PASS",
            "operation": "cleanup stage",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "logical_bytes_quarantined": 0,
            "note": "Docker objects are not moved or deleted during stage; volumes are excluded.",
            "next": "Run cleanup verify before the explicit Docker purge.",
        }

    def _verify_docker(
        self, run_id: str, candidate_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        isolation = self._read_record(self._run_root(run_id) / "isolation" / f"{candidate_id}.json")
        state = self._revalidate_docker_candidate(plan)
        if isolation.get("state_sha256") != state["state_sha256"]:
            raise CleanupRefusal("DOCKER_CANDIDATE_CHANGED_SINCE_STAGE")
        report = {
            "schema_version": "runtime-cleanup-docker-verification.v1",
            "project": self.project,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "verified_at": _now(),
            "status": "PASS",
            "docker_kind": state["kind"],
            "state_sha256": state["state_sha256"],
            "volume_check": "PASS_NO_VOLUME_OR_BIND_MOUNTS"
            if state["kind"] == "container"
            else "NOT_APPLICABLE",
        }
        self._write_new(self._run_root(run_id) / "verification" / f"{candidate_id}.json", report)
        return {
            "status": "PASS",
            "operation": "cleanup verify",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "next": "Review the verification record before the explicit Docker purge.",
        }

    def _purge_docker(self, run_id: str, candidate_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        verification = self._read_record(
            self._run_root(run_id) / "verification" / f"{candidate_id}.json"
        )
        if verification.get("status") != "PASS":
            raise CleanupRefusal("VERIFICATION_NOT_PASSED")
        state = self._revalidate_docker_candidate(plan)
        if verification.get("state_sha256") != state["state_sha256"]:
            raise CleanupRefusal("DOCKER_CANDIDATE_CHANGED_SINCE_VERIFY")
        docker_root = self.project_root / "runtime/docker"
        available_before = os.statvfs(docker_root).f_bavail * os.statvfs(docker_root).f_frsize
        intent = {
            "schema_version": "runtime-cleanup-docker-purge-intent.v1",
            "project": self.project,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "docker_kind": state["kind"],
            "state_sha256": state["state_sha256"],
            "created_at": _now(),
        }
        self._write_new(self._run_root(run_id) / "purge-intent" / f"{candidate_id}.json", intent)
        command = (
            ["container", "rm", state["object_id"]]
            if state["kind"] == "container"
            else ["image", "rm", state["object_id"]]
        )
        result = self._run_docker(command, timeout=60)
        if result.returncode != 0:
            raise CleanupRefusal("DOCKER_PURGE_FAILED")
        available_after = os.statvfs(docker_root).f_bavail * os.statvfs(docker_root).f_frsize
        receipt = intent | {
            "schema_version": "runtime-cleanup-docker-purge-receipt.v1",
            "purged_at": _now(),
            "logical_bytes_removed": None,
            "available_bytes_before": available_before,
            "available_bytes_after": available_after,
            "available_bytes_delta": available_after - available_before,
            "note": (
                "Docker volume removal and build-cache pruning are intentionally not "
                "part of this command."
            ),
        }
        self._write_new(self._run_root(run_id) / "purge-receipt" / f"{candidate_id}.json", receipt)
        return {
            "status": "PASS",
            "operation": "cleanup purge",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "logical_bytes_removed": None,
            "available_bytes_delta": receipt["available_bytes_delta"],
        }

    def stage(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        run_id = _safe_run_id(run_id, default=False)
        candidate_id = _safe_candidate_id(candidate_id)
        plan = self._planned_candidate(run_id, candidate_id)
        if plan.get("storage_kind") == "docker":
            return self._stage_docker(run_id, candidate_id, plan)
        rule = self._live_rule(candidate_id, plan)
        if not plan.get("eligible") or rule.eligibility != "ELIGIBLE":
            raise CleanupRefusal("CANDIDATE_NOT_ELIGIBLE")
        if plan.get("source_relative") != rule.relative_path:
            raise CleanupRefusal("CANDIDATE_PLAN_PATH_MISMATCH")
        source = self._path(rule.relative_path)
        if not source.exists() or source.is_symlink():
            raise CleanupRefusal("CANDIDATE_SOURCE_MISSING_OR_SYMLINK")
        if self._protected_reason(source):
            raise CleanupRefusal("PROTECTED_PATH_REJECTED")
        manifest = self._tree_manifest(source, allow_internal_links=rule.allow_internal_links)
        if manifest["tree_sha256"] != plan.get("tree_sha256"):
            raise CleanupRefusal("CANDIDATE_HASH_CHANGED_SINCE_PLAN")
        reference_status, _ = self._reference_status(source)
        if reference_status != "UNREFERENCED":
            raise CleanupRefusal("CANDIDATE_BECAME_REFERENCED")
        destination = self._quarantine_path(run_id, candidate_id)
        if destination.exists() or destination.is_symlink():
            raise CleanupRefusal("QUARANTINE_DESTINATION_ALREADY_EXISTS")
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._assert_no_symlink_ancestor(destination)
        if source.parent.stat().st_dev != destination.parent.stat().st_dev:
            raise CleanupRefusal("QUARANTINE_CROSS_DEVICE_MOVE_REJECTED")
        run_root = self._run_root(run_id)
        intent = {
            "schema_version": "runtime-cleanup-stage-intent.v1",
            "inventory_sha256": _sha256_file(run_root / "inventory.json"),
            "project": self.project,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "source_relative": rule.relative_path,
            "quarantine_relative": str(destination.relative_to(self.project_root)),
            "tree_sha256": manifest["tree_sha256"],
            "created_at": _now(),
        }
        self._write_new(run_root / "stage-intent" / f"{candidate_id}.json", intent)
        os.replace(source, destination)
        moved = self._tree_manifest(destination, allow_internal_links=rule.allow_internal_links)
        if moved["tree_sha256"] != manifest["tree_sha256"]:
            raise CleanupRefusal("QUARANTINE_HASH_MISMATCH_AFTER_MOVE")
        isolation = intent | {
            "schema_version": "runtime-cleanup-isolation.v1",
            "isolated_at": _now(),
            "size_bytes": moved["size_bytes"],
            "file_count": moved["file_count"],
            "restore_command": (
                "restore only by a reviewed manual rename after checking this manifest"
            ),
        }
        self._write_new(run_root / "isolation" / f"{candidate_id}.json", isolation)
        return {
            "status": "PASS",
            "operation": "cleanup stage",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "logical_bytes_quarantined": moved["size_bytes"],
            "note": "A same-filesystem move does not claim released disk space.",
            "next": "Run cleanup verify with the same run ID and candidate ID before purge.",
        }

    def verify(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        run_id = _safe_run_id(run_id, default=False)
        candidate_id = _safe_candidate_id(candidate_id)
        plan = self._planned_candidate(run_id, candidate_id)
        if plan.get("storage_kind") == "docker":
            return self._verify_docker(run_id, candidate_id, plan)
        isolation = self._read_record(self._run_root(run_id) / "isolation" / f"{candidate_id}.json")
        rule = self._live_rule(candidate_id, plan)
        source = self._path(rule.relative_path)
        if source.exists() or source.is_symlink():
            raise CleanupRefusal("SOURCE_REAPPEARED_AFTER_STAGE")
        destination = self._quarantine_path(run_id, candidate_id)
        if not destination.exists() or destination.is_symlink():
            raise CleanupRefusal("QUARANTINE_OBJECT_MISSING_OR_SYMLINK")
        manifest = self._tree_manifest(destination, allow_internal_links=rule.allow_internal_links)
        if (
            isolation.get("tree_sha256") != manifest["tree_sha256"]
            or plan.get("tree_sha256") != manifest["tree_sha256"]
        ):
            raise CleanupRefusal("QUARANTINE_HASH_MISMATCH")
        report = {
            "schema_version": "runtime-cleanup-verification.v1",
            "project": self.project,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "verified_at": _now(),
            "status": "PASS",
            "tree_sha256": manifest["tree_sha256"],
            "file_count": manifest["file_count"],
            "size_bytes": manifest["size_bytes"],
            "rebuild_commands": plan.get("rebuild_commands", []),
            "rebuild_validation": "AUTOMATED_PROOF_REQUIRED_BEFORE_PURGE",
            "note": "Integrity is verified without recreating the quarantined artifact in-place.",
        }
        self._write_new(self._run_root(run_id) / "verification" / f"{candidate_id}.json", report)
        return {
            "status": "PASS",
            "operation": "cleanup verify",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "logical_bytes_ready_for_purge": manifest["size_bytes"],
            "next": (
                "Run cleanup rebuild --execute, then separately review cleanup purge --execute."
            ),
        }

    def _revalidate_staged_file(
        self, run_id: str, candidate_id: str, plan: dict[str, Any]
    ) -> tuple[CandidateRule, Path, dict[str, Any]]:
        rule = self._live_rule(candidate_id, plan)
        if not plan.get("eligible") or rule.eligibility != "ELIGIBLE":
            raise CleanupRefusal("CANDIDATE_NOT_ELIGIBLE")
        if plan.get("source_relative") != rule.relative_path:
            raise CleanupRefusal("CANDIDATE_PLAN_PATH_MISMATCH")
        source = self._path(rule.relative_path)
        if self._protected_reason(source):
            raise CleanupRefusal("PROTECTED_PATH_REJECTED")
        if source.exists() or source.is_symlink():
            raise CleanupRefusal("SOURCE_REAPPEARED_AFTER_STAGE")
        if self._reference_status(source)[0] != "UNREFERENCED":
            raise CleanupRefusal("CANDIDATE_BECAME_REFERENCED")
        if source.is_relative_to(self.source_root):
            relative = source.relative_to(self.source_root).as_posix()
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", "--", relative + "/"],
                cwd=self.source_root,
                capture_output=True,
                check=False,
            )
            tracked = subprocess.run(
                ["git", "ls-files", "-z", "--", relative],
                cwd=self.source_root,
                capture_output=True,
                check=False,
            )
            if ignored.returncode != 0 or tracked.returncode != 0 or tracked.stdout:
                raise CleanupRefusal("CANDIDATE_NOT_GIT_IGNORED")
        destination = self._quarantine_path(run_id, candidate_id)
        if not destination.exists() or destination.is_symlink():
            raise CleanupRefusal("QUARANTINE_OBJECT_MISSING_OR_SYMLINK")
        manifest = self._tree_manifest(destination, allow_internal_links=rule.allow_internal_links)
        if manifest["tree_sha256"] != plan.get("tree_sha256"):
            raise CleanupRefusal("QUARANTINE_HASH_MISMATCH")
        return rule, destination, manifest

    def _execute_rebuild_recipe(self, recipe: str, temp: Path) -> dict[str, Any]:
        try:
            from .cleanup_rebuild import execute_recipe
        except ImportError:
            from cleanup_rebuild import execute_recipe
        return execute_recipe(self, recipe, temp)

    def rebuild(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        try:
            from .cleanup_rebuild import rebuild
        except ImportError:
            from cleanup_rebuild import rebuild
        run_id = _safe_run_id(run_id, default=False)
        candidate_id = _safe_candidate_id(candidate_id)
        plan = self._planned_candidate(run_id, candidate_id)
        if plan.get("storage_kind") == "docker":
            raise CleanupRefusal("DOCKER_REBUILD_NOT_SUPPORTED")
        rule, _, _ = self._revalidate_staged_file(run_id, candidate_id, plan)
        return rebuild(self, run_id, candidate_id, plan, rule)

    def purge(self, run_id: str, candidate_id: str) -> dict[str, Any]:
        run_id = _safe_run_id(run_id, default=False)
        candidate_id = _safe_candidate_id(candidate_id)
        plan = self._planned_candidate(run_id, candidate_id)
        if plan.get("storage_kind") == "docker":
            return self._purge_docker(run_id, candidate_id, plan)
        verification = self._read_record(
            self._run_root(run_id) / "verification" / f"{candidate_id}.json"
        )
        if verification.get("status") != "PASS":
            raise CleanupRefusal("VERIFICATION_NOT_PASSED")
        rule, destination, manifest = self._revalidate_staged_file(run_id, candidate_id, plan)
        try:
            from .cleanup_rebuild import require_proof
        except ImportError:
            from cleanup_rebuild import require_proof
        proof_sha256 = require_proof(self, run_id, candidate_id, plan, rule)
        # Recheck just before recording intent; no rebuild can authorize new references.
        self._revalidate_staged_file(run_id, candidate_id, plan)
        available_before = (
            os.statvfs(destination.parent).f_bavail * os.statvfs(destination.parent).f_frsize
        )
        intent = {
            "schema_version": "runtime-cleanup-purge-intent.v1",
            "rebuild_proof_sha256": proof_sha256,
            "project": self.project,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "tree_sha256": manifest["tree_sha256"],
            "created_at": _now(),
        }
        self._write_new(self._run_root(run_id) / "purge-intent" / f"{candidate_id}.json", intent)
        if destination.is_dir():
            if not shutil.rmtree.avoids_symlink_attacks:
                raise CleanupRefusal("SAFE_DIRECTORY_PURGE_UNAVAILABLE")
            shutil.rmtree(destination)
        else:
            destination.unlink()
        available_after = (
            os.statvfs(destination.parent).f_bavail * os.statvfs(destination.parent).f_frsize
        )
        receipt = intent | {
            "schema_version": "runtime-cleanup-purge-receipt.v1",
            "purged_at": _now(),
            "logical_bytes_removed": manifest["size_bytes"],
            "available_bytes_before": available_before,
            "available_bytes_after": available_after,
            "available_bytes_delta": available_after - available_before,
            "note": (
                "available_bytes_delta is the filesystem-observed release, not the "
                "earlier quarantine move."
            ),
        }
        self._write_new(self._run_root(run_id) / "purge-receipt" / f"{candidate_id}.json", receipt)
        return {
            "status": "PASS",
            "operation": "cleanup purge",
            "run_id": run_id,
            "candidate_id": candidate_id,
            "logical_bytes_removed": manifest["size_bytes"],
            "available_bytes_delta": receipt["available_bytes_delta"],
        }
