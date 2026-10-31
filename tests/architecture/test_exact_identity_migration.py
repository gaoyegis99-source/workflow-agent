from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/maintenance/exact_identity_migration.py"
SPEC = importlib.util.spec_from_file_location("exact_identity_migration_v7", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def row(bindings, *, mode="LEGACY"):
    return {
        "deliverable_id": "deliverable",
        "task_id": "task",
        "spec_revision": 3,
        "identity_mode": mode,
        "payload": {"role_bindings": bindings, "identity_mode": mode},
    }


def test_only_manifest_bound_exact_identity_is_eligible():
    binding = {
        "role_id": "trend_result",
        "output_ref": {
            "kind": "OPERATOR_OUTPUT",
            "schema_id": "p3-result-v1",
            "schema_source": "MANIFEST",
        },
    }
    decision = MODULE.classify_deliverable(row([binding]))
    assert decision["eligible"] is True
    assert decision["target_digest"].startswith("sha256:")


def test_compatibility_catalog_and_missing_roles_fail_closed():
    compatibility = {
        "role_id": "trend_result",
        "output_ref": {
            "kind": "OPERATOR_OUTPUT",
            "schema_id": "p3-result-v1",
            "schema_source": "COMPATIBILITY_CATALOG",
        },
    }
    assert MODULE.classify_deliverable(row([]))["reason"] == "ROLE_BINDING_NOT_DETERMINISTIC"
    assert MODULE.classify_deliverable(row([compatibility]))["eligible"] is False


def test_duplicate_roles_fail_closed():
    binding = {
        "role_id": "same",
        "output_ref": {
            "kind": "OPERATOR_OUTPUT",
            "schema_id": "schema",
            "schema_source": "MANIFEST",
        },
    }
    assert MODULE.classify_deliverable(row([binding, binding]))["eligible"] is False
