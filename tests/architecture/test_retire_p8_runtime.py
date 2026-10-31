from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.maintenance.retire_p8_runtime import (
    BACKUP_SCHEMA,
    ESCROW_SCHEMA,
    RESTORE_SCHEMA,
    P8Retirement,
    P8RetirementRefusal,
)


class FakeDocker:
    def __init__(self, *, active: bool = False, label_match: bool = True) -> None:
        self.active = active
        self.label_match = label_match
        self.calls: list[list[str]] = []

    def __call__(self, project: str) -> dict:
        return {
            "status": "PASS",
            "containers": (
                [
                    {
                        "id": "container-1",
                        "state": "running",
                        "image": "sha256:protected-image",
                    }
                ]
                if self.active
                else [
                    {
                        "id": "container-1",
                        "state": "exited",
                        "image": "sha256:protected-image",
                    }
                ]
            ),
            "volumes": [
                {
                    "name": "v7-p8-data",
                    "compose_project": project if self.label_match else "another-project",
                }
            ],
            "images": ["sha256:protected-image"],
        }


def _fixture(tmp_path: Path, *, active: bool = False, label_match: bool = True):
    project = tmp_path / "groundwater-v7"
    (project / "source").mkdir(parents=True)
    p8 = project / "runtime/p8"
    release = p8 / "releases" / "release-a"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text('{"artifact":"release-a"}', encoding="utf-8")
    verification = p8 / "verification"
    verification.mkdir(parents=True)
    (verification / "safe.log").write_text("readiness passed", encoding="utf-8")
    instance = p8 / "instances" / "old-instance"
    instance.mkdir(parents=True)
    (instance / "p8.env").write_text(
        "V7_P8_PROJECT_NAME=v7-p8-fixture\nV7_SERVICE_TOKEN=DO-NOT-RECORD\n",
        encoding="utf-8",
    )
    secrets = p8 / "secrets"
    secrets.mkdir()
    (secrets / "database-password").write_text("DO-NOT-RECORD", encoding="utf-8")
    docker = FakeDocker(active=active, label_match=label_match)
    return P8Retirement(project, p8_root=p8, docker_probe=docker), project, p8, docker


def _backup(tmp_path: Path, run_id: str) -> tuple[Path, Path]:
    root = tmp_path / "external-cold-backup"
    root.mkdir()
    (root / "backup-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": BACKUP_SCHEMA,
                "status": "PASSED",
                "run_id": run_id,
                "backup_id": "backup-1",
                "coverage": ["databases", "object-storage", "artifacts", "configuration"],
            }
        ),
        encoding="utf-8",
    )
    (root / "restore-evidence.json").write_text(
        json.dumps(
            {
                "schema_version": RESTORE_SCHEMA,
                "status": "PASSED",
                "run_id": run_id,
                "backup_id": "backup-1",
            }
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "external-secret-escrow-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": ESCROW_SCHEMA,
                "status": "AVAILABLE",
                "backup_id": "backup-1",
                "escrow_id": "escrow-1",
                "issued_at": "2026-10-24T00:00:00Z",
                "verification_sha256": "a" * 64,
                "coverage": ["p8-service-secrets"],
            }
        ),
        encoding="utf-8",
    )
    return root, receipt


def test_plan_redacts_p8_secret_values_and_never_offers_images_for_removal(tmp_path: Path) -> None:
    service, project, _, _ = _fixture(tmp_path)
    plan = service.plan("2026-10-24-1")
    inventory = project / "operations/maintenance/p8-retirement/2026-10-24-1/inventory.json"
    text = inventory.read_text(encoding="utf-8")

    assert plan["status"] == "PASS"
    assert "DO-NOT-RECORD" not in text
    assert json.loads(text)["protected_images"] == ["sha256:protected-image"]
    assert any(
        item["relative"] == "secrets" and not item["stageable"]
        for item in json.loads(text)["metadata_targets"]
    )


def test_secret_marker_in_a_log_is_not_staged(tmp_path: Path) -> None:
    service, project, _, _ = _fixture(tmp_path)
    log = project / "runtime/p8/verification/safe.log"
    log.write_text("TOKEN=must-not-be-archived", encoding="utf-8")

    service.plan("2026-10-24-1b")
    inventory = json.loads(
        (project / "operations/maintenance/p8-retirement/2026-10-24-1b/inventory.json").read_text(
            encoding="utf-8"
        )
    )
    verification = next(
        item for item in inventory["metadata_targets"] if item["relative"] == "verification"
    )
    assert verification["stageable"] is False
    assert "must-not-be-archived" not in json.dumps(inventory)


def test_backup_stage_verify_and_metadata_purge_use_only_registered_targets(tmp_path: Path) -> None:
    service, project, p8, docker = _fixture(tmp_path)
    run_id = "2026-10-24-2"
    service.plan(run_id)
    backup, receipt = _backup(tmp_path, run_id)
    assert service.backup_verify(run_id, backup, receipt)["status"] == "PASS"
    assert service.stage(run_id)["status"] == "PASS"
    quarantine = project / "runtime/archive/p8-retirement" / run_id
    assert (quarantine / "releases/release-a/manifest.json").is_file()
    assert (quarantine / "instances/old-instance.metadata.json").is_file()
    assert not (quarantine / "secrets").exists()
    assert service.verify(run_id)["status"] == "PASS"

    # No real Docker command is possible through the fake probe.  The metadata
    # scope is enough to verify that only inventory paths are removed.
    result = service.purge(run_id, "metadata")
    assert set(result["removed"]) == {"instances", "releases", "secrets", "verification"}
    assert not any(p8.iterdir())
    assert docker.calls == []
    with pytest.raises(P8RetirementRefusal, match="PURGE_ALREADY_EXISTS"):
        service.purge(run_id, "metadata")


def test_verify_refuses_a_tampered_quarantine_copy(tmp_path: Path) -> None:
    service, project, _, _ = _fixture(tmp_path)
    run_id = "2026-10-24-3"
    service.plan(run_id)
    backup, receipt = _backup(tmp_path, run_id)
    service.backup_verify(run_id, backup, receipt)
    service.stage(run_id)
    copied = project / "runtime/archive/p8-retirement" / run_id / "releases/release-a/manifest.json"
    copied.write_text("tampered", encoding="utf-8")

    with pytest.raises(P8RetirementRefusal, match="QUARANTINE_HASH_CHANGED"):
        service.verify(run_id)


def test_missing_backup_or_receipt_and_active_instances_fail_closed(tmp_path: Path) -> None:
    service, _, _, _ = _fixture(tmp_path)
    run_id = "2026-10-24-4"
    service.plan(run_id)
    with pytest.raises(P8RetirementRefusal, match="EXTERNAL_BACKUP_OR_ESCROW_REQUIRED"):
        service.backup_verify(run_id, None, None)

    active_service, _, _, _ = _fixture(tmp_path / "active", active=True)
    active_run = "2026-10-24-5"
    assert active_service.plan(active_run)["status"] == "BLOCKED"
    backup, receipt = _backup(tmp_path / "active", active_run)
    active_service.backup_verify(active_run, backup, receipt)
    with pytest.raises(P8RetirementRefusal, match="ACTIVE_CONTAINER_PRESENT"):
        active_service.stage(active_run)


def test_volume_label_mismatch_is_refused_during_inventory(tmp_path: Path) -> None:
    service, _, _, _ = _fixture(tmp_path, label_match=False)
    with pytest.raises(P8RetirementRefusal, match="VOLUME_LABEL_MISMATCH"):
        service.plan("2026-10-24-6")


def test_backup_paths_inside_project_and_p8_links_are_refused(tmp_path: Path) -> None:
    service, project, p8, _ = _fixture(tmp_path)
    run_id = "2026-10-24-6b"
    service.plan(run_id)
    outside_backup, receipt = _backup(tmp_path, run_id)
    inside_backup = project / "runtime" / "backup"
    inside_backup.mkdir(parents=True)
    for source in ("backup-manifest.json", "restore-evidence.json"):
        (inside_backup / source).write_text(
            (outside_backup / source).read_text(encoding="utf-8"), encoding="utf-8"
        )
    with pytest.raises(P8RetirementRefusal, match="BACKUP_ROOT_INSIDE_PROJECT"):
        service.backup_verify(run_id, inside_backup, receipt)

    link = p8 / "releases" / "escaped"
    try:
        link.symlink_to(tmp_path / "outside")
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(P8RetirementRefusal, match="P8_RUNTIME_ENTRY_LINK_OR_TYPE"):
        service.plan("2026-10-24-6c")


def test_docker_purge_revalidates_exact_candidates_without_global_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, docker = _fixture(tmp_path)
    run_id = "2026-10-24-7"
    service.plan(run_id)
    backup, receipt = _backup(tmp_path, run_id)
    service.backup_verify(run_id, backup, receipt)
    service.stage(run_id)
    service.verify(run_id)
    monkeypatch.setattr(
        service, "_docker_command", lambda arguments: docker.calls.append(arguments)
    )

    result = service.purge(run_id, "docker-data")
    assert result["removed"] == ["container:container-1", "volume:v7-p8-data"]
    assert docker.calls == [["container", "rm", "container-1"], ["volume", "rm", "v7-p8-data"]]
    assert all("prune" not in item for call in docker.calls for item in call)


def test_ops_exposes_only_the_fixed_p8_retirement_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.maintenance import ops

    args, _, execute, _ = ops._parse(
        ["runtime", "retire-p8", "backup-verify", "--run-id", "2026-10-24-8"]
    )
    assert (args.action, args.phase, args.run_id, execute) == (
        "retire-p8",
        "backup-verify",
        "2026-10-24-8",
        False,
    )
    assert (
        ops.runtime(
            "retire-p8",
            None,
            None,
            phase="purge",
            run_id="2026-10-24-8",
            scope="metadata",
            execute=False,
        )["status"]
        == "CONFIRMATION_REQUIRED"
    )
    monkeypatch.setattr(
        ops.retire_p8_runtime,
        "retire",
        lambda action, run_id, **kwargs: {
            "status": "PASS",
            "action": action,
            "run_id": run_id,
            "kwargs": kwargs,
        },
    )
    result = ops.runtime(
        "retire-p8",
        None,
        None,
        phase="verify",
        run_id="2026-10-24-8",
        execute=False,
    )
    assert result["action"] == "verify"
