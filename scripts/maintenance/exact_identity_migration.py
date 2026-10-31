"""Fail-closed retirement workflow for legacy deliverable identities.

The maintenance command deliberately does not invent role mappings or rewrite
asset manifests.  A legacy row is eligible only when its immutable payload
already contains complete exact role bindings.  Anything else is reported and
blocks the whole apply operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

RUN_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]+")
BACKUP_ID = re.compile(r"sha256:[0-9a-f]{64}")


class ExactIdentityRefusal(RuntimeError):
    """A migration safety invariant was not satisfied."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _binding_is_exact(binding: Any) -> bool:
    if not isinstance(binding, dict) or set(binding) != {"role_id", "output_ref"}:
        return False
    ref = binding["output_ref"]
    return (
        isinstance(binding["role_id"], str)
        and bool(binding["role_id"])
        and isinstance(ref, dict)
        and ref.get("kind") == "OPERATOR_OUTPUT"
        and isinstance(ref.get("schema_id"), str)
        and bool(ref["schema_id"])
        and ref.get("schema_source") == "MANIFEST"
    )


def classify_deliverable(row: dict[str, Any]) -> dict[str, Any]:
    """Return a secret-free, deterministic migration decision for one row."""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    bindings = payload.get("role_bindings")
    exact = (
        isinstance(bindings, list)
        and bool(bindings)
        and all(_binding_is_exact(binding) for binding in bindings)
        and len({binding["role_id"] for binding in bindings}) == len(bindings)
    )
    return {
        "deliverable_id": row["deliverable_id"],
        "task_id": row["task_id"],
        "spec_revision": row["spec_revision"],
        "current_mode": row.get("identity_mode", payload.get("identity_mode", "LEGACY")),
        "eligible": exact,
        "reason": "EXACT_BINDINGS_PRESENT" if exact else "ROLE_BINDING_NOT_DETERMINISTIC",
        "target_digest": _digest({"role_bindings": bindings}) if exact else None,
    }


class ExactIdentityMigration:
    def __init__(self, source_root: Path, database_url: str | None = None) -> None:
        self.source_root = source_root.resolve()
        self.project_root = self.source_root.parent
        self.database_url = database_url or os.environ.get("V7_DATABASE_URL")
        if not self.database_url:
            raise ExactIdentityRefusal("V7_DATABASE_URL_REQUIRED")
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ExactIdentityRefusal("POSTGRESQL_PSYCOG_URL_REQUIRED")
        self.engine = sa.create_engine(
            self.database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 3},
            hide_parameters=True,
        )

    @property
    def audit_root(self) -> Path:
        return self.project_root / "operations/maintenance/exact-identity"

    def _run_id(self, requested: str | None) -> str:
        if requested is not None:
            if RUN_ID.fullmatch(requested) is None:
                raise ExactIdentityRefusal("INVALID_MIGRATION_RUN_ID")
            return requested
        day = datetime.now(UTC).date().isoformat()
        self.audit_root.mkdir(parents=True, exist_ok=True)
        sequence = 1
        while (self.audit_root / f"{day}-{sequence}").exists():
            sequence += 1
        return f"{day}-{sequence}"

    def _directory(self, run_id: str) -> Path:
        if RUN_ID.fullmatch(run_id) is None:
            raise ExactIdentityRefusal("INVALID_MIGRATION_RUN_ID")
        directory = (self.audit_root / run_id).resolve()
        if not directory.is_relative_to(self.audit_root.resolve()):
            raise ExactIdentityRefusal("MIGRATION_PATH_ESCAPE")
        return directory

    def _inventory(self) -> dict[str, Any]:
        with self.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT deliverable_id,task_id,spec_revision,digest,payload,identity_mode "
                        "FROM deliverables ORDER BY deliverable_id"
                    )
                ).mappings()
            ]
            active_legacy_packages = int(
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM outbox WHERE done IS FALSE AND "
                        "(CAST(payload AS TEXT) LIKE '%legacy_v1%' OR "
                        "CAST(payload AS TEXT) LIKE '%deferred_templates%')"
                    )
                )
                or 0
            )
        decisions = [classify_deliverable(row) for row in rows]
        blocked = [item for item in decisions if not item["eligible"]]
        return {
            "schema_version": 1,
            "project": "groundwater-v7",
            "deliverables": decisions,
            "summary": {
                "deliverable_count": len(decisions),
                "eligible_count": len(decisions) - len(blocked),
                "blocked_count": len(blocked),
                "active_legacy_engine_packages": active_legacy_packages,
            },
            "status": "BLOCKED" if blocked or active_legacy_packages else "READY",
        }

    def plan(self, run_id: str | None = None) -> dict[str, Any]:
        selected = self._run_id(run_id)
        directory = self._directory(selected)
        if directory.exists():
            raise ExactIdentityRefusal("MIGRATION_RUN_ALREADY_EXISTS")
        directory.mkdir(parents=True)
        inventory = self._inventory() | {"run_id": selected}
        inventory["inventory_digest"] = _digest(inventory)
        (directory / "inventory.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "status": inventory["status"],
            "operation": "migrate exact-identity plan",
            "run_id": selected,
            "inventory_digest": inventory["inventory_digest"],
            "summary": inventory["summary"],
        }

    def _load(self, run_id: str) -> tuple[Path, dict[str, Any]]:
        directory = self._directory(run_id)
        path = directory / "inventory.json"
        if not path.is_file() or path.is_symlink():
            raise ExactIdentityRefusal("MIGRATION_INVENTORY_MISSING")
        inventory = json.loads(path.read_text(encoding="utf-8"))
        supplied = inventory.pop("inventory_digest", None)
        if supplied != _digest(inventory):
            raise ExactIdentityRefusal("MIGRATION_INVENTORY_CHANGED")
        inventory["inventory_digest"] = supplied
        return directory, inventory

    def apply(self, run_id: str, backup_id: str | None) -> dict[str, Any]:
        if not backup_id or BACKUP_ID.fullmatch(backup_id) is None:
            raise ExactIdentityRefusal("VERIFIED_BACKUP_ID_REQUIRED")
        backup_value = os.environ.get("V7_EXACT_IDENTITY_BACKUP_DIR")
        if not backup_value:
            raise ExactIdentityRefusal("VERIFIED_BACKUP_DIRECTORY_REQUIRED")
        backup = Path(backup_value).expanduser().resolve()
        runtime = (self.project_root / "runtime").resolve()
        if not backup.is_dir() or backup.is_symlink() or not backup.is_relative_to(runtime):
            raise ExactIdentityRefusal("BACKUP_DIRECTORY_OUTSIDE_PROJECT_RUNTIME")
        from platform_infrastructure.cold_backup import verified_manifest

        try:
            backup_manifest = verified_manifest(backup)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            raise ExactIdentityRefusal("COLD_BACKUP_VERIFICATION_FAILED") from error
        if backup_manifest.get("manifest_digest") != backup_id:
            raise ExactIdentityRefusal("COLD_BACKUP_ID_MISMATCH")
        directory, inventory = self._load(run_id)
        if inventory["status"] != "READY":
            raise ExactIdentityRefusal("LEGACY_MIGRATION_NOT_DETERMINISTIC")
        current = self._inventory()
        if current["status"] != "READY" or current["summary"] != inventory["summary"]:
            raise ExactIdentityRefusal("MIGRATION_SOURCE_CHANGED")
        planned = {item["deliverable_id"]: item for item in inventory["deliverables"]}
        with self.engine.begin() as connection:
            connection.execute(sa.text("SELECT pg_advisory_xact_lock(770025)"))
            locked = [
                dict(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT deliverable_id,task_id,spec_revision,digest,payload,identity_mode "
                        "FROM deliverables ORDER BY deliverable_id FOR UPDATE"
                    )
                ).mappings()
            ]
            for row in locked:
                decision = classify_deliverable(row)
                if decision != planned.get(row["deliverable_id"]):
                    raise ExactIdentityRefusal("MIGRATION_SOURCE_CHANGED")
                payload = dict(row["payload"])
                payload["identity_mode"] = "EXACT"
                connection.execute(
                    sa.text(
                        "UPDATE deliverables SET identity_mode='EXACT',digest=:digest,"
                        "payload=CAST(:payload AS jsonb) "
                        "WHERE deliverable_id=:deliverable_id"
                    ),
                    {
                        "deliverable_id": row["deliverable_id"],
                        "digest": decision["target_digest"],
                        "payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    },
                )
        receipt = {
            "schema_version": 1,
            "status": "APPLIED",
            "run_id": run_id,
            "backup_id": backup_id,
            "inventory_digest": inventory["inventory_digest"],
            "updated_count": inventory["summary"]["deliverable_count"],
        }
        receipt["receipt_digest"] = _digest(receipt)
        path = directory / "apply-receipt.json"
        if path.exists():
            raise ExactIdentityRefusal("MIGRATION_ALREADY_APPLIED")
        path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"status": "PASS", "operation": "migrate exact-identity apply"} | receipt

    def verify(self, run_id: str) -> dict[str, Any]:
        directory, inventory = self._load(run_id)
        receipt_path = directory / "apply-receipt.json"
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise ExactIdentityRefusal("MIGRATION_APPLY_RECEIPT_MISSING")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        supplied = receipt.pop("receipt_digest", None)
        if (
            supplied != _digest(receipt)
            or receipt.get("inventory_digest") != inventory["inventory_digest"]
        ):
            raise ExactIdentityRefusal("MIGRATION_APPLY_RECEIPT_CHANGED")
        current = self._inventory()
        if current["status"] != "READY" or any(
            item["current_mode"] != "EXACT" for item in current["deliverables"]
        ):
            raise ExactIdentityRefusal("LEGACY_IDENTITY_REMAINS")
        report = {
            "schema_version": 1,
            "status": "PASS",
            "run_id": run_id,
            "inventory_digest": inventory["inventory_digest"],
            "summary": current["summary"],
        }
        report["report_digest"] = _digest(report)
        (directory / "verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"operation": "migrate exact-identity verify"} | report
