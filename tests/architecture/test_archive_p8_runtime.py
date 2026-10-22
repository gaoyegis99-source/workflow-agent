from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "archive_p8_runtime.py"
SPEC = importlib.util.spec_from_file_location("archive_p8_runtime", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _runtime(tmp_path: Path) -> tuple[Path, Path, Path]:
    runtime = tmp_path / ".p8-runtime"
    current_release = runtime / "releases" / "0.15.3-current"
    rollback_release = runtime / "releases" / "0.15.1-rollback"
    stale_release = runtime / "releases" / "0.14.0-stale"
    current_release.mkdir(parents=True)
    rollback_release.mkdir(parents=True)
    stale_release.mkdir(parents=True)
    (current_release / "release-lock.json").write_text("{}\n", encoding="utf-8")
    (rollback_release / "release-lock.json").write_text("{}\n", encoding="utf-8")
    (stale_release / "release-lock.json").write_text("{}\n", encoding="utf-8")
    active = runtime / "instances" / "production-final"
    rollback = runtime / "instances" / "rollback"
    stale = runtime / "instances" / "stale"
    for instance in (active, rollback, stale):
        instance.mkdir(parents=True)
        (instance / "p8.env").write_text("TOKEN_FILE=/run/secret\n", encoding="utf-8")
    backup = rollback / "backups" / "cold.json"
    backup.parent.mkdir()
    backup.write_text('{"backup": true}\n', encoding="utf-8")
    (runtime / "verification").mkdir()
    (runtime / "verification" / "run.json").write_text("{}\n", encoding="utf-8")
    pointer = tmp_path / "current.json"
    pointer.write_text(
        json.dumps(
            {
                "status": "ACTIVE",
                "release_directory": ".p8-runtime/releases/0.15.3-current",
                "instance_environment": ".p8-runtime/instances/production-final/p8.env",
                "release_digest": "sha256:current",
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "restore.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": MODULE.RESTORE_EVIDENCE_SCHEMA,
                "status": "PASSED",
                "rollback_instance": "rollback",
                "rollback_release": "0.15.1-rollback",
            }
        ),
        encoding="utf-8",
    )
    return runtime, pointer, evidence


def test_plan_preserves_the_pointer_targets(tmp_path: Path) -> None:
    runtime, pointer, _ = _runtime(tmp_path)

    plan = MODULE.build_plan(runtime, pointer)

    assert plan["preserve"] == {
        "instances": ["production-final"],
        "releases": ["0.15.3-current"],
    }
    assert plan["archive_candidates"]["instances"] == ["rollback", "stale"]


def test_preflight_requires_verified_rollback_inputs(tmp_path: Path) -> None:
    runtime, pointer, _ = _runtime(tmp_path)

    with pytest.raises(ValueError, match="ROLLBACK_SELECTION_REQUIRED"):
        MODULE.build_plan(runtime, pointer, require_rollback=True)


def test_archive_copies_reviewable_material_without_instance_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, pointer, evidence = _runtime(tmp_path)
    archive_parent = tmp_path / "maintenance" / "v7-runtime-archive"
    monkeypatch.setattr(MODULE, "ARCHIVE_PARENT", archive_parent)

    manifest = MODULE.archive(
        runtime,
        pointer,
        archive_parent / "2026-10-22",
        rollback_instance="rollback",
        rollback_release="0.15.1-rollback",
        cold_backup="backups/cold.json",
        restore_evidence=evidence,
    )

    archive = archive_parent / "2026-10-22"
    assert (archive / "releases" / "0.14.0-stale" / "release-lock.json").is_file()
    assert (archive / "verification" / "run.json").is_file()
    assert (archive / "cold-backup" / "cold.json").is_file()
    assert (archive / "instances" / "stale" / "instance-metadata.json").is_file()
    assert not (archive / "instances" / "stale" / "p8.env").exists()
    assert manifest["source_purge"] == "not performed"
    assert (runtime / "instances" / "stale").is_dir()


def test_purge_removes_only_revalidated_release_and_verification_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, pointer, evidence = _runtime(tmp_path)
    archive_parent = tmp_path / "maintenance" / "v7-runtime-archive"
    monkeypatch.setattr(MODULE, "ARCHIVE_PARENT", archive_parent)
    archive = archive_parent / "2026-10-22"
    MODULE.archive(
        runtime,
        pointer,
        archive,
        rollback_instance="rollback",
        rollback_release="0.15.1-rollback",
        cold_backup="backups/cold.json",
        restore_evidence=evidence,
    )

    result = MODULE.purge(runtime, pointer, archive)

    assert result["removed"] == ["releases/0.14.0-stale", "verification"]
    assert not (runtime / "releases" / "0.14.0-stale").exists()
    assert not (runtime / "verification").exists()
    assert (runtime / "releases" / "0.15.3-current").is_dir()
    assert (runtime / "releases" / "0.15.1-rollback").is_dir()
    assert (runtime / "instances" / "stale").is_dir()
