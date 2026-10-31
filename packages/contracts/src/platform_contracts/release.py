"""Historical Release Lock validation retained for read-only audit evidence.

Direct Compose deployments deliberately do not import this module at startup,
authorize requests with it, or use it to select a deployment.  The functions
remain here solely so archived P8 evidence and old cold-backup manifests can be
inspected without changing their bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ReleaseContext:
    """Explicit, read-only context for historical Release Lock inspection.

    This type is intentionally separate from ``DeploymentContext``: it is
    accepted only by audit/restore helpers in this module and is never loaded
    by active application composition.  Keeping the legacy values explicit
    preserves the public audit API without restoring an environment-driven
    startup gate.
    """

    deployment_mode: str = "development"
    release_lock_path: Path | None = None
    release_digest: str | None = None

    def __post_init__(self) -> None:
        if self.deployment_mode not in {"development", "production"}:
            raise ValueError("V7_LEGACY_RELEASE_MODE_INVALID")
        if self.release_lock_path is not None and not isinstance(self.release_lock_path, Path):
            raise ValueError("V7_LEGACY_RELEASE_LOCK_INVALID")
        if self.release_digest is not None and not _sha256(self.release_digest):
            raise ValueError("V7_LEGACY_RELEASE_DIGEST_INVALID")
        if self.is_production and (self.release_lock_path is None or self.release_digest is None):
            raise ValueError("V7_LEGACY_RELEASE_CONTEXT_INCOMPLETE")

    @property
    def is_production(self) -> bool:
        return self.deployment_mode == "production"


class LegacyReleaseContext(Protocol):
    """Minimal historical shape accepted by the audit-only helpers below."""

    is_production: bool
    release_lock_path: Path | None
    release_digest: str | None


REQUIRED_WHEELS = {
    "groundwater_agent_runtime",
    "groundwater_checker_pack",
    "groundwater_data_service",
    "groundwater_execution_service",
    "groundwater_knowledge_service",
    "groundwater_operator_pack",
    "groundwater_platform_api",
    "groundwater_platform_contracts",
    "groundwater_platform_core",
    "groundwater_platform_infrastructure",
    "groundwater_tabular_operator_pack",
}

SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
P8_DATABASE_SCHEMAS = {
    "platform": "0026_worker_lane_leases",
    "data": "0004_document_assets",
    "execution": "0006_remote_reliability",
    "knowledge": "0002_lifecycle_outbox",
}
PRE_WORKER_LANE_LEASE_P8_DATABASE_SCHEMAS = {
    "platform": "0025_exact_identity_only",
    "data": "0004_document_assets",
    "execution": "0006_remote_reliability",
    "knowledge": "0002_lifecycle_outbox",
}
# Existing, signed P8.2 release locks remain readable for audit and N-1
# recovery.  Only a *target* release may use ``P8_DATABASE_SCHEMAS``; the
# upgrade planner enforces that distinction before any runtime action.
LEGACY_P8_DATABASE_SCHEMAS = {
    "platform": "0023_workflow_release_lifecycle",
    "data": "0004_document_assets",
    "execution": "0006_remote_reliability",
    "knowledge": "0002_lifecycle_outbox",
}
SUPPORTED_P8_DATABASE_SCHEMAS = frozenset(
    {
        tuple(sorted(P8_DATABASE_SCHEMAS.items())),
        tuple(sorted(PRE_WORKER_LANE_LEASE_P8_DATABASE_SCHEMAS.items())),
        tuple(sorted(LEGACY_P8_DATABASE_SCHEMAS.items())),
    }
)


def _sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def validate_release_lock(value: object) -> dict:
    """Validate the release envelope without trusting a filename or caller.

    Schemas 1 and 2 remain readable for historical backup verification.  New
    P8.2 production releases use schema 3 and carry their N-1 recovery inputs.
    """

    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2, 3}:
        raise ValueError("Unsupported release lock")
    if value.get("schema_version") != 3:
        return value
    required = {
        "schema_version",
        "version",
        "operator_catalog",
        "checker_catalog",
        "tool_catalog",
        "model_catalog",
        "runtime_profile",
        "images",
        "image_refs",
        "source_tree",
        "wheels",
        "web_assets",
        "dependency_wheels",
        "compatibility",
        "runtime_requirements",
        "release_digest",
    }
    if set(value) != required:
        raise ValueError("Release lock v3 fields differ")
    compatibility = value.get("compatibility")
    if not (
        isinstance(value.get("version"), str)
        and isinstance(compatibility, dict)
        and set(compatibility)
        == {
            "upgrade_from",
            "api_contract",
            "backup_formats",
            "database_schemas",
            "recovery_releases",
        }
        and isinstance(compatibility.get("upgrade_from"), list)
        and all(isinstance(item, str) and item for item in compatibility["upgrade_from"])
        and compatibility.get("api_contract") == "api/v1"
        and compatibility.get("backup_formats") == [1, 2]
        and isinstance(compatibility.get("database_schemas"), dict)
        and tuple(sorted(compatibility["database_schemas"].items()))
        in SUPPORTED_P8_DATABASE_SCHEMAS
        and isinstance(compatibility.get("recovery_releases"), dict)
    ):
        raise ValueError("Release compatibility contract is invalid")
    for version, record in compatibility["recovery_releases"].items():
        if not (
            version in compatibility["upgrade_from"]
            and isinstance(record, dict)
            and set(record)
            == {
                "release_digest",
                "catalogs",
                "images",
                "model_catalog",
                "runtime_profile",
            }
            and _sha256(record.get("release_digest"))
            and isinstance(record.get("catalogs"), dict)
            and all(_sha256(item) for item in record["catalogs"].values())
            and isinstance(record.get("images"), dict)
            and all(_sha256(item) for item in record["images"].values())
            and _sha256(record.get("model_catalog"))
            and _sha256(record.get("runtime_profile"))
        ):
            raise ValueError("Release recovery compatibility is invalid")
    requirements = value.get("runtime_requirements")
    if not (
        isinstance(requirements, dict)
        and set(requirements) == {"docker", "compose", "postgresql", "python"}
        and all(isinstance(item, str) and item for item in requirements.values())
    ):
        raise ValueError("Release runtime requirements are invalid")
    if not (
        isinstance(value.get("images"), dict)
        and value["images"]
        and all(_sha256(item) for item in value["images"].values())
        and isinstance(value.get("image_refs"), dict)
        and set(value["image_refs"]) == set(value["images"])
        and all(
            isinstance(item, str) and "@" not in item and ":" in item
            for item in value["image_refs"].values()
        )
    ):
        raise ValueError("Release image records are invalid")
    return value


def digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_digest(path: str | Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def production_mode(release: LegacyReleaseContext) -> bool:
    """Return the historical audit mode; never use this for active deployment."""

    return release.is_production


def load_release_lock(path: str | Path, expected_digest: str) -> dict:
    if not isinstance(expected_digest, str) or not expected_digest.startswith("sha256:"):
        raise ValueError("A pinned V7_RELEASE_DIGEST is required")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Release lock cannot be read") from error
    value = validate_release_lock(value)
    supplied = value.get("release_digest")
    actual = digest({key: item for key, item in value.items() if key != "release_digest"})
    if supplied != actual or supplied != expected_digest:
        raise ValueError("Release lock digest differs from the pinned release")
    return value


def verify_catalog(lock: dict, name: str, path: str | Path, catalog: dict) -> None:
    record = lock.get(name)
    if not isinstance(record, dict):
        raise ValueError(f"Release lock has no {name}")
    if record.get("digest") != catalog.get("catalog_digest"):
        raise ValueError(f"{name} digest differs from the release lock")
    if record.get("file_sha256") != file_digest(path):
        raise ValueError(f"{name} file differs from the release lock")


def production_release(
    *,
    release: LegacyReleaseContext,
    operator_path: str | Path,
    operator_catalog: dict,
    tool_path: str | Path | None = None,
    tool_catalog: dict | None = None,
    catalogs: dict[str, tuple[str | Path, dict]] | None = None,
    images: dict[str, str | None] | None = None,
    version: str | None = None,
) -> dict | None:
    """Verify a historical lock for audit or restore inspection only.

    Active V7 composition uses :class:`DeploymentContext` and never calls this
    function.  Keeping the validator separate avoids rewriting signed evidence
    while ensuring it cannot become an implicit startup gate again.
    """

    if not production_mode(release):
        return None
    if release.release_lock_path is None or release.release_digest is None:
        raise ValueError("Production requires V7_RELEASE_LOCK and V7_RELEASE_DIGEST")
    lock = load_release_lock(release.release_lock_path, release.release_digest)
    verify_catalog(lock, "operator_catalog", operator_path, operator_catalog)
    if tool_path is not None or tool_catalog is not None:
        if tool_path is None or tool_catalog is None:
            raise ValueError("Tool catalogue path and value must be supplied together")
        verify_catalog(lock, "tool_catalog", tool_path, tool_catalog)
        if lock["tool_catalog"].get("catalog_id") != tool_catalog.get("catalog_id"):
            raise ValueError("Tool catalogue identity differs from the release lock")
    for name, (path, catalog) in (catalogs or {}).items():
        verify_catalog(lock, name, path, catalog)
    if version is not None and lock.get("version") != version:
        raise ValueError("Installed service version differs from the release lock")
    if version is not None:
        wheels = lock.get("wheels")
        if not isinstance(wheels, dict) or not wheels:
            raise ValueError("Release lock has no wheel evidence")
        marker = f"-{version}-"
        found = {
            name.split(marker, 1)[0]
            for name, wheel_digest in wheels.items()
            if marker in name
            and isinstance(wheel_digest, str)
            and wheel_digest.startswith("sha256:")
        }
        if found != REQUIRED_WHEELS or len(found) != len(wheels):
            raise ValueError("Release wheel evidence is incomplete")
    for name, installed in (images or {}).items():
        expected_image = lock.get("images", {}).get(name)
        if installed is None or installed != expected_image:
            raise ValueError(f"Runtime image {name} differs from the release lock")
    return lock
