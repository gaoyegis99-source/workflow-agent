import hashlib
import json

import pytest
from platform_execution.adapters.docker import CwlDocker
from platform_execution.application.workflow_runner import WorkflowRunner
from platform_infrastructure.cold_backup import is_backup_safe
from platform_infrastructure.upgrade import verify_park


def canonical(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_parked_backup_only_relaxes_durable_waiting_facts():
    counts = {
        "platform": {
            "outbox": 2,
            "active_actions": 2,
            "workflow_packages": 0,
            "authoring_validations": 0,
            "workflow_trials": 1,
            "agent_advancing": 1,
            "agent_tools": 1,
            "agent_model_calls": 0,
        },
        "data": {"imports": 0, "publications": 0},
        "execution": {"executions": 3},
        "knowledge": {"imports": 0, "outbox": 0},
    }
    assert is_backup_safe(counts, {"state": "PARKED"})
    counts["data"]["publications"] = 1
    assert not is_backup_safe(counts, {"state": "PARKED"})
    counts["data"]["publications"] = 0
    counts["platform"]["agent_model_calls"] = 1
    assert not is_backup_safe(counts, {"state": "PARKED"})


def test_maintenance_restart_is_exactly_bound_to_action_epoch_and_jobstore(tmp_path, monkeypatch):
    root = tmp_path / "execution-1"
    key = root / "jobstore" / "shared" / "rootJobStoreID"
    key.parent.mkdir(parents=True)
    key.write_bytes(b"root-job")
    item = {
        "path": "shared/rootJobStoreID",
        "size": len(b"root-job"),
        "digest": "sha256:" + hashlib.sha256(b"root-job").hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "state": "PARKED",
        "source_release_digest": "sha256:" + "1" * 64,
        "jobstores": [
            {
                "action_id": "action-1",
                "execution_id": "execution-1",
                "engine_epoch": 4,
                "jobstore": str((root / "jobstore").resolve()),
                "restartable": True,
                "key_files": [item],
            }
        ],
    }
    manifest["manifest_digest"] = canonical(manifest)
    path = tmp_path / "park-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("V7_PARK_MANIFEST", str(path))
    row = {"action_id": "action-1", "execution_id": "execution-1"}
    assert WorkflowRunner.maintenance_restart(row, root, 4)
    assert not WorkflowRunner.maintenance_restart(row, root, 5)
    key.write_bytes(b"changed")
    assert not WorkflowRunner.maintenance_restart(row, root, 4)


def test_park_manifest_tamper_is_rejected(tmp_path):
    value = {
        "schema_version": 1,
        "state": "PARKED",
        "source_release_digest": "sha256:" + "1" * 64,
        "jobstores": [],
    }
    value["manifest_digest"] = canonical(value)
    path = tmp_path / "park.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert verify_park(path)["state"] == "PARKED"
    value["state"] = "COMMITTED"
    path.write_text(json.dumps(value), encoding="utf-8")
    try:
        verify_park(path)
    except RuntimeError as error:
        assert "digest differs" in str(error)
    else:
        raise AssertionError("tampered manifest was accepted")


def test_partial_p82_container_identity_is_never_treated_as_legacy(monkeypatch):
    monkeypatch.setenv("V7_DEPLOYMENT_ID", "deployment-a")
    monkeypatch.setenv("V7_RELEASE_DIGEST", "sha256:" + "1" * 64)
    runtime = CwlDocker("sha256:" + "2" * 64)
    row = {
        "action_id": "action",
        "execution_id": "execution",
        "digest": "digest",
        "attempt_id": "attempt",
        "payload": {},
        "runtime": {},
    }
    labels = {
        "v7.action": "action",
        "v7.digest": "digest",
        "v7.attempt": "attempt",
        "v7.epoch": "1",
        "v7.execution": "execution",
    }
    info = {"Image": runtime.image, "Config": {"Labels": labels}}
    with pytest.raises(ValueError, match="labels differ"):
        runtime.check(row, info, 1)
