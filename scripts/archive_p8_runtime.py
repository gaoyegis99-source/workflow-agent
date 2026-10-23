#!/usr/bin/env python3
"""Plan, verify and archive stale P8 metadata without copying Docker volumes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("V7_P8_RUNTIME_ROOT", str(ROOT / ".p8-runtime"))
).expanduser().resolve()
DEFAULT_POINTER = ROOT / "docs/verification/p82-current-release.json"
DEFAULT_ARCHIVE_ROOT = (
    WORKSPACE_ROOT / "maintenance" / "v7-runtime-archive" / date.today().isoformat()
)
ARCHIVE_PARENT = WORKSPACE_ROOT / "maintenance" / "v7-runtime-archive"
RESTORE_EVIDENCE_SCHEMA = "v7-runtime-restore-evidence.v1"
EXCLUDED_INSTANCE_ENTRIES = {
    "control",
    "data",
    "execution",
    "knowledge",
    "postgres",
    "secrets",
    "spatial",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(value: str, *, label: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label}_PATH_INVALID")
    return path


def _safe_runtime_path(runtime_root: Path, value: str, *, label: str) -> Path:
    relative = _safe_relative(value, label=label)
    candidate = runtime_root / relative
    if candidate.is_symlink():
        raise ValueError(f"{label}_SYMLINK_INVALID")
    try:
        candidate.resolve().relative_to(runtime_root.resolve())
    except ValueError as error:
        raise ValueError(f"{label}_PATH_ESCAPE") from error
    return candidate


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label}_FILE_INVALID")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label}_JSON_INVALID")
    return payload


def _pointer_paths(runtime_root: Path, pointer_path: Path) -> tuple[dict[str, Any], Path, Path]:
    pointer = _read_json(pointer_path, label="CURRENT_POINTER")
    if pointer.get("status") != "ACTIVE":
        raise ValueError("CURRENT_POINTER_NOT_ACTIVE")
    release_value = pointer.get("release_directory")
    instance_value = pointer.get("instance_environment")
    if not isinstance(release_value, str) or not isinstance(instance_value, str):
        raise ValueError("CURRENT_POINTER_PATHS_INVALID")
    runtime_prefix = ".p8-runtime/"
    if not (release_value.startswith(runtime_prefix) and instance_value.startswith(runtime_prefix)):
        raise ValueError("CURRENT_POINTER_RUNTIME_PATHS_INVALID")
    release = _safe_runtime_path(
        runtime_root, release_value.removeprefix(runtime_prefix), label="ACTIVE_RELEASE"
    )
    environment = _safe_runtime_path(
        runtime_root, instance_value.removeprefix(runtime_prefix), label="ACTIVE_INSTANCE"
    )
    if not release.is_dir() or not environment.is_file():
        raise ValueError("CURRENT_POINTER_TARGET_MISSING")
    return pointer, release, environment.parent


def _readable_tree(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.exists():
        raise ValueError(f"{label}_MISSING_OR_SYMLINK")
    paths = [path] if path.is_file() else sorted(path.rglob("*"))
    for candidate in paths:
        if candidate.is_symlink():
            raise ValueError(f"{label}_SYMLINK_INVALID")
        if candidate.is_file():
            with candidate.open("rb"):
                pass


def _restore_evidence(evidence_path: Path, rollback_instance: str, rollback_release: str) -> None:
    evidence = _read_json(evidence_path, label="RESTORE_EVIDENCE")
    if (
        evidence.get("schema_version") != RESTORE_EVIDENCE_SCHEMA
        or evidence.get("status") != "PASSED"
        or evidence.get("rollback_instance") != rollback_instance
        or evidence.get("rollback_release") != rollback_release
    ):
        raise ValueError("RESTORE_EVIDENCE_NOT_VERIFIED")


def _instance_metadata(instance: Path) -> dict[str, Any]:
    environment = instance / "p8.env"
    if not environment.is_file() or environment.is_symlink():
        raise ValueError("ROLLBACK_INSTANCE_ENVIRONMENT_INVALID")
    keys = []
    for line in environment.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            keys.append(line.split("=", 1)[0].strip())
    excluded = sorted(
        item.name for item in instance.iterdir() if item.name in EXCLUDED_INSTANCE_ENTRIES
    )
    return {
        "instance": instance.name,
        "source_environment_sha256": _sha256(environment),
        "environment_keys": sorted(set(keys)),
        "excluded_runtime_entries": excluded,
        "note": "Docker volumes and secrets are intentionally neither copied nor purged.",
    }


def build_plan(
    runtime_root: Path,
    pointer_path: Path,
    *,
    rollback_instance: str | None = None,
    rollback_release: str | None = None,
    cold_backup: str | None = None,
    restore_evidence: Path | None = None,
    require_rollback: bool = False,
) -> dict[str, Any]:
    """Inspect the runtime tree without changing it and return an archive plan."""
    runtime_root = runtime_root.resolve()
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise ValueError("RUNTIME_ROOT_INVALID")
    pointer, active_release, active_instance = _pointer_paths(runtime_root, pointer_path)
    releases_dir = runtime_root / "releases"
    instances_dir = runtime_root / "instances"
    releases = sorted(path for path in releases_dir.iterdir() if path.is_dir())
    instances = sorted(path for path in instances_dir.iterdir() if path.is_dir())
    plan: dict[str, Any] = {
        "schema_version": "v7-runtime-archive-plan.v1",
        "active": {
            "instance": active_instance.name,
            "release": active_release.name,
            "release_digest": pointer.get("release_digest"),
        },
        "preserve": {
            "instances": [active_instance.name],
            "releases": [active_release.name],
        },
        "archive_candidates": {
            "instances": [path.name for path in instances if path != active_instance],
            "releases": [path.name for path in releases if path != active_release],
            "verification": (runtime_root / "verification").is_dir(),
        },
        "excluded": {
            "instance_entries": sorted(EXCLUDED_INSTANCE_ENTRIES),
            "reason": "Docker volumes and secrets are out of scope for workspace archival.",
        },
    }
    if not require_rollback:
        return plan
    if not all((rollback_instance, rollback_release, cold_backup, restore_evidence)):
        raise ValueError("ROLLBACK_SELECTION_REQUIRED")
    rollback_instance_path = _safe_runtime_path(
        runtime_root, f"instances/{rollback_instance}", label="ROLLBACK_INSTANCE"
    )
    rollback_release_path = _safe_runtime_path(
        runtime_root, f"releases/{rollback_release}", label="ROLLBACK_RELEASE"
    )
    if rollback_instance_path == active_instance or rollback_release_path == active_release:
        raise ValueError("ROLLBACK_SELECTION_MUST_DIFFER_FROM_ACTIVE")
    if not rollback_instance_path.is_dir() or not rollback_release_path.is_dir():
        raise ValueError("ROLLBACK_SELECTION_MISSING")
    backup_path = _safe_runtime_path(
        runtime_root,
        f"instances/{rollback_instance}/{cold_backup}",
        label="COLD_BACKUP",
    )
    _readable_tree(rollback_instance_path / "p8.env", label="ROLLBACK_ENVIRONMENT")
    _readable_tree(rollback_release_path, label="ROLLBACK_RELEASE")
    _readable_tree(backup_path, label="COLD_BACKUP")
    _restore_evidence(restore_evidence, rollback_instance, rollback_release)
    plan["preserve"] = {
        "instances": [active_instance.name, rollback_instance],
        "releases": [active_release.name, rollback_release],
    }
    plan["rollback"] = {
        "instance": rollback_instance,
        "release": rollback_release,
        "cold_backup": str(backup_path.relative_to(runtime_root)),
        "restore_evidence": str(restore_evidence),
    }
    plan["archive_candidates"] = {
        "instances": [
            path.name for path in instances if path.name not in plan["preserve"]["instances"]
        ],
        "releases": [
            path.name for path in releases if path.name not in plan["preserve"]["releases"]
        ],
        "verification": (runtime_root / "verification").is_dir(),
    }
    return plan


def _copy_tree(source: Path, destination: Path, *, label: str) -> list[dict[str, Any]]:
    _readable_tree(source, label=label)
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        pairs = [(source, destination)]
    else:
        shutil.copytree(source, destination)
        pairs = [
            (path, destination / path.relative_to(source))
            for path in sorted(source.rglob("*"))
            if path.is_file()
        ]
    records = []
    for original, copied in pairs:
        if original.stat().st_size != copied.stat().st_size or _sha256(original) != _sha256(copied):
            raise ValueError("ARCHIVE_COPY_CHECKSUM_MISMATCH")
        records.append(
            {
                "source": str(original),
                "archive": str(copied),
                "size_bytes": original.stat().st_size,
                "sha256": _sha256(original),
            }
        )
    return records


def archive(
    runtime_root: Path,
    pointer_path: Path,
    archive_root: Path,
    *,
    rollback_instance: str,
    rollback_release: str,
    cold_backup: str,
    restore_evidence: Path,
) -> dict[str, Any]:
    """Copy verified stale metadata and releases, never deleting runtime sources."""
    plan = build_plan(
        runtime_root,
        pointer_path,
        rollback_instance=rollback_instance,
        rollback_release=rollback_release,
        cold_backup=cold_backup,
        restore_evidence=restore_evidence,
        require_rollback=True,
    )
    archive_root = archive_root.resolve()
    try:
        archive_root.relative_to(ARCHIVE_PARENT.resolve())
    except ValueError as error:
        raise ValueError("ARCHIVE_ROOT_OUTSIDE_MAINTENANCE") from error
    if archive_root.exists():
        raise FileExistsError(f"ARCHIVE_ROOT_EXISTS:{archive_root}")
    archive_root.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    try:
        runtime_root = runtime_root.resolve()
        for release in plan["archive_candidates"]["releases"]:
            records.extend(
                _copy_tree(
                    runtime_root / "releases" / release,
                    archive_root / "releases" / release,
                    label="ARCHIVE_RELEASE",
                )
            )
        if plan["archive_candidates"]["verification"]:
            records.extend(
                _copy_tree(
                    runtime_root / "verification",
                    archive_root / "verification",
                    label="ARCHIVE_VERIFICATION",
                )
            )
        backup_source = runtime_root / plan["rollback"]["cold_backup"]
        records.extend(
            _copy_tree(
                backup_source,
                archive_root / "cold-backup" / backup_source.name,
                label="ARCHIVE_COLD_BACKUP",
            )
        )
        for instance in plan["archive_candidates"]["instances"]:
            source = runtime_root / "instances" / instance
            metadata = _instance_metadata(source)
            destination = archive_root / "instances" / instance / "instance-metadata.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            records.append(
                {
                    "source": str(source / "p8.env"),
                    "archive": str(destination),
                    "kind": "redacted-instance-metadata",
                    "size_bytes": destination.stat().st_size,
                    "sha256": _sha256(destination),
                }
            )
        manifest = {
            **plan,
            "files": records,
            "source_purge": "not performed",
            "purge_scope": {
                "allowed": ["stale-releases", "runtime-verification"],
                "never_allowed": [
                    "instances",
                    "cold-backup",
                    "docker-volumes",
                    "secrets",
                    "active-release",
                    "rollback-release",
                ],
            },
            "recovery": {
                "command": (
                    "Copy the selected release directory back under .p8-runtime/releases/ "
                    "and use the separately verified P8 restore procedure recorded in "
                    "restore_evidence."
                ),
                "note": (
                    "Runtime instances are never recovered from this archive; "
                    "use the verified cold backup."
                ),
            },
        }
        (archive_root / "archive-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    except Exception:
        shutil.rmtree(archive_root)
        raise


def _archive_path(archive_root: Path, raw_path: str, *, label: str) -> Path:
    """Resolve an archive path while rejecting links and path escapes."""
    path = Path(raw_path)
    if path.is_symlink():
        raise ValueError(f"{label}_SYMLINK_INVALID")
    try:
        path.resolve().relative_to(archive_root)
    except ValueError as error:
        raise ValueError(f"{label}_PATH_ESCAPE") from error
    return path


def _safe_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ValueError(f"{label}_INVALID")
    return value


def _purge_file_records(archive_root: Path, records: object) -> None:
    if not isinstance(records, list):
        raise ValueError("ARCHIVE_MANIFEST_FILES_INVALID")
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("ARCHIVE_MANIFEST_FILE_INVALID")
        archive_value = record.get("archive")
        checksum = record.get("sha256")
        if not isinstance(archive_value, str) or not isinstance(checksum, str):
            raise ValueError("ARCHIVE_MANIFEST_FILE_INVALID")
        archived = _archive_path(archive_root, archive_value, label="ARCHIVED_FILE")
        if not archived.is_file() or _sha256(archived) != checksum:
            raise ValueError("ARCHIVE_HASH_VERIFICATION_FAILED")
        if record.get("kind") == "redacted-instance-metadata":
            continue
        source_value = record.get("source")
        if not isinstance(source_value, str):
            raise ValueError("ARCHIVE_MANIFEST_SOURCE_INVALID")
        source = Path(source_value)
        if not source.is_file() or source.is_symlink() or _sha256(source) != checksum:
            raise ValueError("SOURCE_HASH_VERIFICATION_FAILED")


def purge(runtime_root: Path, pointer_path: Path, archive_root: Path) -> dict[str, Any]:
    """Remove only archived stale releases and verification logs after revalidation.

    Instance directories, cold backups, secrets, and service-volume paths are never
    deletion candidates. This command obtains every deletion target from the
    archive manifest rather than accepting arbitrary paths.
    """
    archive_root = archive_root.resolve()
    try:
        archive_root.relative_to(ARCHIVE_PARENT.resolve())
    except ValueError as error:
        raise ValueError("ARCHIVE_ROOT_OUTSIDE_MAINTENANCE") from error
    manifest_path = archive_root / "archive-manifest.json"
    manifest = _read_json(manifest_path, label="ARCHIVE_MANIFEST")
    if manifest.get("schema_version") != "v7-runtime-archive-plan.v1":
        raise ValueError("ARCHIVE_MANIFEST_SCHEMA_INVALID")
    runtime_root = runtime_root.resolve()
    rollback = manifest.get("rollback")
    if not isinstance(rollback, dict):
        raise ValueError("ARCHIVE_MANIFEST_ROLLBACK_INVALID")
    rollback_instance = _safe_name(rollback.get("instance"), label="ARCHIVE_ROLLBACK_INSTANCE")
    rollback_release = _safe_name(rollback.get("release"), label="ARCHIVE_ROLLBACK_RELEASE")
    cold_backup = rollback.get("cold_backup")
    restore_evidence_value = rollback.get("restore_evidence")
    if not isinstance(cold_backup, str) or not isinstance(restore_evidence_value, str):
        raise ValueError("ARCHIVE_MANIFEST_ROLLBACK_INVALID")
    live_plan = build_plan(
        runtime_root,
        pointer_path,
        rollback_instance=rollback_instance,
        rollback_release=rollback_release,
        cold_backup=cold_backup.removeprefix(f"instances/{rollback_instance}/"),
        restore_evidence=Path(restore_evidence_value),
        require_rollback=True,
    )
    preserve = manifest.get("preserve")
    if not isinstance(preserve, dict):
        raise ValueError("ARCHIVE_MANIFEST_PRESERVE_INVALID")
    protected_releases = preserve.get("releases")
    if not isinstance(protected_releases, list):
        raise ValueError("ARCHIVE_MANIFEST_PRESERVE_INVALID")
    candidates = manifest.get("archive_candidates")
    if not isinstance(candidates, dict):
        raise ValueError("ARCHIVE_MANIFEST_CANDIDATES_INVALID")
    if preserve != live_plan["preserve"] or candidates != live_plan["archive_candidates"]:
        raise ValueError("ARCHIVE_MANIFEST_NO_LONGER_MATCHES_RUNTIME")
    _purge_file_records(archive_root, manifest.get("files"))
    active_release = live_plan["active"]["release"]
    if active_release not in protected_releases:
        raise ValueError("ACTIVE_RELEASE_NOT_PRESERVED")
    releases = candidates.get("releases")
    if not isinstance(releases, list):
        raise ValueError("ARCHIVE_MANIFEST_RELEASES_INVALID")
    removed: list[str] = []
    for raw_release in releases:
        release = _safe_name(raw_release, label="ARCHIVE_RELEASE")
        if release in protected_releases or release == active_release:
            raise ValueError("PURGE_PROTECTED_RELEASE_REFUSED")
        source = _safe_runtime_path(runtime_root, f"releases/{release}", label="PURGE_RELEASE")
        archived = archive_root / "releases" / release
        if not source.is_dir() or not archived.is_dir():
            raise ValueError("PURGE_RELEASE_MISSING")
        shutil.rmtree(source)
        removed.append(f"releases/{release}")
    if candidates.get("verification"):
        verification = _safe_runtime_path(runtime_root, "verification", label="PURGE_VERIFICATION")
        archived_verification = archive_root / "verification"
        if not verification.is_dir() or not archived_verification.is_dir():
            raise ValueError("PURGE_VERIFICATION_MISSING")
        shutil.rmtree(verification)
        removed.append("verification")
    return {
        "schema_version": "v7-runtime-purge-result.v1",
        "archive_root": str(archive_root),
        "removed": removed,
        "retained": [
            "all instances",
            "cold backup",
            "active release",
            "rollback release",
            "Docker volumes and secrets",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("plan", "preflight", "archive", "purge"))
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--pointer", type=Path, default=DEFAULT_POINTER)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--rollback-instance")
    parser.add_argument("--rollback-release")
    parser.add_argument("--cold-backup")
    parser.add_argument("--restore-evidence", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.phase in {"archive", "purge"} and not args.execute:
        parser.error(f"{args.phase} requires --execute")
    require_rollback = args.phase in {"preflight", "archive"}
    try:
        if args.phase == "archive":
            result = archive(
                args.runtime_root,
                args.pointer,
                args.archive_root,
                rollback_instance=args.rollback_instance or "",
                rollback_release=args.rollback_release or "",
                cold_backup=args.cold_backup or "",
                restore_evidence=args.restore_evidence or Path(),
            )
        elif args.phase == "purge":
            result = purge(args.runtime_root, args.pointer, args.archive_root)
        else:
            result = build_plan(
                args.runtime_root,
                args.pointer,
                rollback_instance=args.rollback_instance,
                rollback_release=args.rollback_release,
                cold_backup=args.cold_backup,
                restore_evidence=args.restore_evidence,
                require_rollback=require_rollback,
            )
    except (FileExistsError, ValueError) as error:
        parser.exit(1, f"ARCHIVE_ACTION_REFUSED:{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
