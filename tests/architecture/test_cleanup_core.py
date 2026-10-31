from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.maintenance.cleanup_core import CandidateRule, CleanupCore, CleanupRefusal


class _FixturePolicy:
    def __init__(self, *rules: CandidateRule) -> None:
        self._rules = rules

    def protected_paths(self) -> dict[str, str]:
        return {"runtime/docker": "DOCKER_DATA_ROOT"}

    def rules(self) -> tuple[CandidateRule, ...]:
        return self._rules

    def reference_roots(self) -> tuple[Path, ...]:
        return ()

    def excluded_reference_directories(self, root: Path, current: Path) -> frozenset[str]:
        return frozenset()

    def reconstruct_rule(
        self, candidate_id: str, planned: dict[str, Any] | None
    ) -> CandidateRule | None:
        return None

    def docker_block_reason(self) -> str | None:
        return None

    def docker_inventory_reason(self, block_reason: str | None) -> str:
        return "fixture"

    def protected_compose_projects(self) -> set[str]:
        return set()


def _rebuild_fixture(core, run_id: str, candidate_id: str) -> None:
    """Stub only costly recipe execution; exercise real proof binding and purge guards."""
    import subprocess

    source = core.source_root
    subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
    relative = core._planned_candidate(run_id, candidate_id)["source_relative"]
    ignore = source / ".gitignore"
    existing = ignore.read_text() if ignore.exists() else ""
    pattern = relative.removeprefix("source/") + "/\n"
    if pattern not in existing:
        ignore.write_text(existing + pattern)
    core.policy.rebuild_recipe = lambda rule: "python-caches-v1"
    core._execute_rebuild_recipe = lambda recipe, temp: {
        "status": "PASS",
        "gates": [{"id": "fixture", "status": "PASS", "exit_code": 0}],
        "outputs": {},
    }
    assert core.rebuild(run_id, candidate_id)["status"] == "PASS"


def test_core_owns_the_filesystem_cleanup_lifecycle(tmp_path: Path) -> None:
    source = tmp_path / "source"
    cache = source / "cache"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"rebuildable")
    (tmp_path / "runtime" / "archive").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    (tmp_path / "project-layout.json").write_text("{}\n", encoding="utf-8")
    rule = CandidateRule("fixture.cache", "source/cache", "cache", ("rebuild",))
    core = CleanupCore(tmp_path, source, project="fixture", policy=_FixturePolicy(rule))

    plan = core.plan("2026-10-23-9")
    assert plan["eligible_candidate_ids"] == ["fixture.cache"]
    core.stage("2026-10-23-9", "fixture.cache")
    core.verify("2026-10-23-9", "fixture.cache")
    _rebuild_fixture(core, "2026-10-23-9", "fixture.cache")
    receipt = core.purge("2026-10-23-9", "fixture.cache")

    assert receipt["logical_bytes_removed"] == len(b"rebuildable")
    assert not cache.exists()


def test_distinct_candidates_share_one_quarantine_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    rules = tuple(
        CandidateRule(f"fixture.{name}", f"source/{name}", "cache", ("rebuild",))
        for name in ("first", "second")
    )
    for rule in rules:
        cache = tmp_path / rule.relative_path
        cache.mkdir()
        (cache / "payload").write_bytes(rule.candidate_id.encode())
    core = CleanupCore(tmp_path, source, project="fixture", policy=_FixturePolicy(*rules))
    run_id = "2026-10-23-10"
    core.plan(run_id)

    for rule in rules:
        core.stage(run_id, rule.candidate_id)
        core.verify(run_id, rule.candidate_id)
    quarantine = core.quarantine_root / run_id
    assert {path.name for path in quarantine.iterdir()} == {rule.candidate_id for rule in rules}
    for rule in rules:
        _rebuild_fixture(core, run_id, rule.candidate_id)
        receipt = core.purge(run_id, rule.candidate_id)
        assert receipt["logical_bytes_removed"] == len(rule.candidate_id.encode())
        assert not (tmp_path / rule.relative_path).exists()
    assert not list(quarantine.iterdir())
    assert len(list((core.audit_root / run_id / "purge-receipt").glob("*.json"))) == 2


@pytest.mark.parametrize("collision_kind", ("directory", "file", "symlink"))
def test_existing_quarantine_destination_is_never_overwritten(
    tmp_path: Path, collision_kind: str
) -> None:
    source = tmp_path / "source"
    cache = source / "cache"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"source")
    rule = CandidateRule("fixture.cache", "source/cache", "cache", ("rebuild",))
    core = CleanupCore(tmp_path, source, project="fixture", policy=_FixturePolicy(rule))
    run_id = "2026-10-23-11"
    core.plan(run_id)
    destination = core.quarantine_root / run_id / rule.candidate_id
    destination.parent.mkdir(parents=True)
    if collision_kind == "directory":
        destination.mkdir()
        protected = destination / "payload"
        protected.write_bytes(b"keep")
    elif collision_kind == "file":
        protected = destination
        protected.write_bytes(b"keep")
    else:
        protected = tmp_path / "outside"
        protected.write_bytes(b"keep")
        destination.symlink_to(protected)

    with pytest.raises(CleanupRefusal, match="QUARANTINE_DESTINATION_ALREADY_EXISTS|SYMLINK"):
        core.stage(run_id, rule.candidate_id)

    assert (cache / "payload").read_bytes() == b"source"
    assert protected.read_bytes() == b"keep"
    assert not (core.audit_root / run_id / "stage-intent").exists()


def _prepared_rebuild(tmp_path):
    import subprocess

    source = tmp_path / "source"
    cache = source / "cache"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"keep until proof passes")
    (source / ".gitignore").write_text("cache/\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
    rule = CandidateRule("fixture.cache", "source/cache", "cache", ("NOT_EXECUTABLE",))
    policy = _FixturePolicy(rule)
    policy.rebuild_recipe = lambda candidate: "python-caches-v1"
    policy.reference_roots = lambda: (source / "apps",)
    core = CleanupCore(tmp_path, source, project="fixture", policy=policy)
    core._execute_rebuild_recipe = lambda recipe, temp: {
        "status": "PASS",
        "gates": [{"id": "fixture", "status": "PASS", "exit_code": 0}],
        "outputs": {},
    }
    run = "2026-10-24-1"
    core.plan(run)
    core.stage(run, rule.candidate_id)
    core.verify(run, rule.candidate_id)
    return core, run, rule


def test_integrity_alone_cannot_authorize_purge(tmp_path):
    core, run, rule = _prepared_rebuild(tmp_path)
    with pytest.raises(CleanupRefusal, match="REBUILD_PROOF_REQUIRED"):
        core.purge(run, rule.candidate_id)
    assert core._quarantine_path(run, rule.candidate_id).exists()
    assert not (core._run_root(run) / "purge-intent").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "reference",
        "protected",
        "eligibility",
        "tracked",
        "ignore",
        "inputs",
        "quarantine",
        "failed-proof",
        "missing-proof",
        "inventory",
        "interrupted",
    ],
)
def test_purge_rechecks_all_live_protection_and_proof_bindings(tmp_path, mutation):
    import json
    import subprocess
    from dataclasses import replace

    core, run, rule = _prepared_rebuild(tmp_path)
    assert core.rebuild(run, rule.candidate_id)["status"] == "PASS"
    proof_dir = core._run_root(run) / "rebuild" / rule.candidate_id
    quarantine = core._quarantine_path(run, rule.candidate_id)
    if mutation == "source":
        (core.project_root / rule.relative_path).mkdir()
    elif mutation == "reference":
        apps = core.source_root / "apps"
        apps.mkdir()
        (apps / "consumer.py").write_text("CACHE = 'source/cache'\n")
    elif mutation == "protected":
        core.policy.protected_paths = lambda: {"source/cache": "PROTECTED_NOW"}
    elif mutation == "eligibility":
        core.policy._rules = (replace(rule, eligibility="REBUILD_PROOF_REQUIRED"),)
    elif mutation == "tracked":
        subprocess.run(
            [
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                subprocess.check_output(
                    ["git", "hash-object", "-w", "--stdin"], cwd=core.source_root, input=b"tracked"
                )
                .decode()
                .strip(),
                "cache/payload",
            ],
            cwd=core.source_root,
            check=True,
            capture_output=True,
        )
    elif mutation == "ignore":
        (core.source_root / ".gitignore").write_text("")
    elif mutation == "inputs":
        (core.source_root / "input.py").write_text("VALUE = 2\n")
    elif mutation == "quarantine":
        (quarantine / "payload").write_bytes(b"tampered")
    elif mutation == "failed-proof":
        file = proof_dir / "0001.json"
        value = json.loads(file.read_text())
        value["status"] = "FAILED"
        file.write_text(json.dumps(value))
    elif mutation == "missing-proof":
        (proof_dir / "0001.json").unlink()
    elif mutation == "interrupted":
        (proof_dir / "0002.intent.json").write_text("{}")
    elif mutation == "inventory":
        path = core._run_root(run) / "inventory.json"
        path.write_text(path.read_text() + " ")
    with pytest.raises(CleanupRefusal):
        core.purge(run, rule.candidate_id)
    assert quarantine.exists()
    assert not (core._run_root(run) / "purge-intent").exists()


def test_failed_rebuild_is_append_only_and_never_authorizes_purge(tmp_path):
    core, run, rule = _prepared_rebuild(tmp_path)
    core._execute_rebuild_recipe = lambda recipe, temp: {
        "status": "FAILED",
        "gates": [{"id": "fixture", "status": "FAILED", "exit_code": 1}],
    }
    assert core.rebuild(run, rule.candidate_id)["status"] == "FAILED"
    assert core.rebuild(run, rule.candidate_id)["status"] == "FAILED"
    records = core._run_root(run) / "rebuild" / rule.candidate_id
    assert (records / "0001.json").is_file() and (records / "0002.json").is_file()
    with pytest.raises(CleanupRefusal, match="REBUILD_NOT_PASSED"):
        core.purge(run, rule.candidate_id)


def test_unregistered_recipe_does_not_execute_or_delete(tmp_path):
    core, run, rule = _prepared_rebuild(tmp_path)
    core.policy.rebuild_recipe = lambda candidate: None
    with pytest.raises(CleanupRefusal, match="REBUILD_RECIPE_NOT_REGISTERED"):
        core.rebuild(run, rule.candidate_id)
    assert core._quarantine_path(run, rule.candidate_id).exists()
