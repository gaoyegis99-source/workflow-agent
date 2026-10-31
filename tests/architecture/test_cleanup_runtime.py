from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from scripts.maintenance.cleanup_core import CandidateRule, CleanupCore, CleanupRefusal
from scripts.maintenance.cleanup_policy import V7CleanupPolicy

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "groundwater-v7"


def _cleanup_service_at(
    project_root: Path,
    source_root: Path,
    *,
    project: str = PROJECT,
    rules: tuple[CandidateRule, ...] | None = None,
) -> CleanupCore:
    return CleanupCore(
        project_root,
        source_root,
        project=project,
        policy=V7CleanupPolicy(project_root, source_root, project=project, rules=rules),
    )


def _service(tmp_path: Path, *rules: object):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (tmp_path / "runtime" / "archive").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "project-layout.json").write_text("{}\n", encoding="utf-8")
    return _cleanup_service_at(
        tmp_path,
        source,
        project="cleanup-fixture",
        rules=tuple(rules),
    )


def _rule(path: str = "source/cache", *, links: bool = False):
    return CandidateRule(
        "fixture.cache",
        path,
        "test-cache",
        ("rebuild-fixture-cache",),
        allow_internal_links=links,
    )


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


def test_stage_verify_purge_is_explicit_and_auditable(tmp_path: Path) -> None:
    service = _service(tmp_path, _rule())
    cache = tmp_path / "source" / "cache"
    cache.mkdir()
    (cache / "payload.bin").write_bytes(b"rebuildable")

    plan = service.plan("2026-10-23-1")

    assert plan["status"] == "PARTIAL"  # The fixture intentionally has no Docker socket.
    assert plan["eligible_candidate_ids"] == ["fixture.cache"]
    stage = service.stage("2026-10-23-1", "fixture.cache")
    assert stage["logical_bytes_quarantined"] == len(b"rebuildable")
    assert not cache.exists()
    staged = tmp_path / "runtime/archive/cleanup-quarantine/2026-10-23-1/fixture.cache"
    assert (staged / "payload.bin").read_bytes() == b"rebuildable"
    with pytest.raises(CleanupRefusal):
        service.stage("2026-10-23-1", "fixture.cache")

    service.verify("2026-10-23-1", "fixture.cache")
    _rebuild_fixture(service, "2026-10-23-1", "fixture.cache")
    purge = service.purge("2026-10-23-1", "fixture.cache")

    assert purge["logical_bytes_removed"] == len(b"rebuildable")
    assert not staged.exists()
    receipt = (
        tmp_path / "operations/maintenance/cleanup/2026-10-23-1/purge-receipt/fixture.cache.json"
    )
    assert json.loads(receipt.read_text(encoding="utf-8"))["candidate_id"] == "fixture.cache"


def test_plan_rejects_symlink_hardlink_reference_and_protected_paths(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "value").write_text("outside", encoding="utf-8")
    symlink = tmp_path / "source" / "symlink-cache"
    symlink.symlink_to(outside, target_is_directory=True)
    linked = tmp_path / "source" / "linked-cache"
    linked.mkdir()
    (linked / "one").write_text("same inode", encoding="utf-8")
    os.link(linked / "one", linked / "two")
    referenced = tmp_path / "runtime" / "referenced-cache"
    referenced.mkdir(parents=True)
    (referenced / "value").write_text("keep", encoding="utf-8")
    code = tmp_path / "source" / "apps"
    code.mkdir()
    (code / "consumer.py").write_text("PATH = 'runtime/referenced-cache'\n", encoding="utf-8")
    protected = tmp_path / "runtime" / "docker" / "cache"
    protected.mkdir(parents=True)
    (protected / "value").write_text("protected", encoding="utf-8")
    service = _service(
        tmp_path,
        _rule("source/symlink-cache"),
        CandidateRule("fixture.linked", "source/linked-cache", "test", ("rebuild",)),
        CandidateRule("fixture.referenced", "runtime/referenced-cache", "test", ("rebuild",)),
        CandidateRule("fixture.protected", "runtime/docker/cache", "test", ("rebuild",)),
    )

    service.plan("2026-10-23-2")
    inventory = json.loads(
        (tmp_path / "operations/maintenance/cleanup/2026-10-23-2/inventory.json").read_text(
            encoding="utf-8"
        )
    )
    records = {item["candidate_id"]: item for item in inventory["candidates"]}

    assert records["fixture.cache"]["eligible"] is False
    assert "SYMLINK" in records["fixture.cache"]["eligibility_reason"]
    assert "HARDLINK" in records["fixture.linked"]["eligibility_reason"]
    assert records["fixture.referenced"]["reference_status"] == "REFERENCED"
    assert records["fixture.protected"]["protected_reason"] == "DOCKER_DATA_ROOT"


def test_tampered_quarantine_fails_verification_and_preserves_the_object(tmp_path: Path) -> None:
    service = _service(tmp_path, _rule())
    cache = tmp_path / "source" / "cache"
    cache.mkdir()
    (cache / "payload.bin").write_bytes(b"original")
    service.plan("2026-10-23-3")
    service.stage("2026-10-23-3", "fixture.cache")
    staged = tmp_path / "runtime/archive/cleanup-quarantine/2026-10-23-3/fixture.cache"
    (staged / "payload.bin").write_bytes(b"tampered")

    with pytest.raises(CleanupRefusal, match="QUARANTINE_HASH_MISMATCH"):
        service.verify("2026-10-23-3", "fixture.cache")
    with pytest.raises(CleanupRefusal):
        service.purge("2026-10-23-3", "fixture.cache")
    assert (staged / "payload.bin").read_bytes() == b"tampered"


def test_discovered_pycache_can_be_verified_by_a_fresh_service(tmp_path: Path) -> None:
    source = tmp_path / "source"
    cache = source / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"bytecode")
    (tmp_path / "runtime" / "archive").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    (tmp_path / "project-layout.json").write_text("{}\n", encoding="utf-8")
    first = _cleanup_service_at(tmp_path, source, project="cleanup-fixture")

    planned = first.plan("2026-10-23-6")
    candidate_id = planned["eligible_candidate_ids"][0]
    first.stage("2026-10-23-6", candidate_id)

    second = _cleanup_service_at(tmp_path, source, project="cleanup-fixture")
    second.verify("2026-10-23-6", candidate_id)
    _rebuild_fixture(second, "2026-10-23-6", candidate_id)
    second.purge("2026-10-23-6", candidate_id)
    assert not cache.exists()


def test_ops_cli_cleans_its_own_bytecode_across_processes(tmp_path: Path) -> None:
    maintenance = tmp_path / "source" / "scripts" / "maintenance"
    maintenance.mkdir(parents=True)
    for name in (
        "ops.py",
        "cleanup_core.py",
        "cleanup_protocol.py",
        "cleanup_policy.py",
        "cleanup_rebuild.py",
        "check_runner.py",
        "project_environment.py",
        "bootstrap_project.py",
        "exact_identity_migration.py",
        "retire_p8_runtime.py",
    ):
        shutil.copy2(ROOT / "scripts" / "maintenance" / name, maintenance / name)
    launcher = tmp_path / "bin/ops"
    shutil.copytree(ROOT / "scripts/maintenance/bootstrap-assets", maintenance / "bootstrap-assets")
    subprocess.run(
        [sys.executable, "-B", str(maintenance / "bootstrap_project.py"), "--execute"],
        check=True,
        capture_output=True,
    )
    version = PROJECT.rsplit("-", 1)[-1]
    interpreter = tmp_path / f"runtime/toolchain/{version}/bin/python"
    interpreter.parent.mkdir(parents=True)
    # Reuse only the test interpreter through a fixture launcher, not a deployment venv.
    interpreter.write_text("#!/usr/bin/env bash\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    interpreter.chmod(0o755)
    (tmp_path / "bin/docker").unlink()  # A file-only CLI fixture must never contact a real socket.
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
    (source / ".gitignore").write_text("__pycache__/\n")
    tests = source / "tests/architecture"
    tests.mkdir(parents=True)
    for name in ("core", "runtime"):
        (tests / f"test_cleanup_{name}.py").write_text("def test_fixture():\n    assert True\n")
    gates = source / "docs/maintenance"
    gates.mkdir(parents=True)
    (gates / "maintenance-gates.v2.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "checks": {
                    "git": {"kind": "local", "command": ["git", "diff", "--check"], "requires": []}
                },
                "levels": {name: ["git"] for name in ("fast", "changed", "release")},
                "capabilities": {},
                "paths": [],
            }
        )
    )
    cache = maintenance / "__pycache__"
    cache.mkdir()
    (cache / "sentinel.pyc").write_bytes(b"existing cache")
    env = dict(os.environ)
    for name in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH"):
        env.pop(name, None)
    version = PROJECT.rsplit("-", 1)[-1]
    for key in tuple(env):
        if key.startswith(("V6_", "V7_")) or key in {
            "UV_PROJECT_ENVIRONMENT",
            "GROUNDWATER_GENERATED_ROOT",
        }:
            env.pop(key)
    env[f"{version.upper()}_PYTHON"] = str(interpreter)

    def cli(*arguments: str) -> dict:
        result = subprocess.run(
            ["bash", str(launcher), "--json", "cleanup", *arguments],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    run_id = "2026-10-23-12"
    plan = cli("plan", "--run-id", run_id)
    candidate_id = next(
        value
        for value in plan["eligible_candidate_ids"]
        if value.startswith(f"{version}.cache.pycache.")
    )
    cli("stage", "--run-id", run_id, "--candidate", candidate_id, "--execute")
    assert not cache.exists()
    cli("verify", "--run-id", run_id, "--candidate", candidate_id)
    assert not cache.exists()
    cli("rebuild", "--run-id", run_id, "--candidate", candidate_id, "--execute")
    cli("purge", "--run-id", run_id, "--candidate", candidate_id, "--execute")
    assert not cache.exists()
    quarantine = tmp_path / "runtime/archive/cleanup-quarantine" / run_id / candidate_id
    assert not quarantine.exists()


def test_internal_managed_link_is_never_followed_but_an_escape_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    managed = tmp_path / "source" / "managed-cache"
    managed.mkdir()
    (managed / "payload").write_text("safe", encoding="utf-8")
    (managed / "inside").symlink_to("payload")
    service = _service(tmp_path, _rule("source/managed-cache", links=True))
    assert service.plan("2026-10-23-4")["eligible_candidate_ids"] == ["fixture.cache"]

    escaped = tmp_path / "source" / "escaped-cache"
    escaped.mkdir()
    (escaped / "outside").symlink_to(tmp_path / "outside-target")
    service = _service(tmp_path, _rule("source/escaped-cache", links=True))
    service.plan("2026-10-23-5")
    inventory = json.loads(
        (tmp_path / "operations/maintenance/cleanup/2026-10-23-5/inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert "SYMLINK_ESCAPE_REJECTED" in inventory["candidates"][0]["eligibility_reason"]


def test_p8_is_hard_protected_even_if_a_rule_is_injected(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    held = tmp_path / "runtime" / "p8" / "held-release"
    held.mkdir(parents=True)
    (held / "historical-metadata.json").write_text("{}\n", encoding="utf-8")
    service = _service(
        tmp_path,
        CandidateRule(
            "fixture.p8",
            "runtime/p8/held-release",
            "test",
            ("never",),
        ),
    )

    service.plan("2026-10-23-7")
    inventory = json.loads(
        (tmp_path / "operations/maintenance/cleanup/2026-10-23-7/inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert inventory["candidates"][0]["protected_reason"] == "ACTIVE_P8_RUNTIME"


def test_missing_direct_compose_metadata_also_protects_docker_candidates(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "runtime" / "archive").mkdir(parents=True)
    service = _cleanup_service_at(tmp_path, source, project="groundwater-v7", rules=())

    candidate = service._docker_container_candidate("a" * 64, "Exited", [], False)

    assert candidate["eligible"] is False
    assert candidate["protected_reason"] == "P8_DIRECT_COMPOSE_METADATA_INVALID"


def test_direct_compose_policy_protects_every_configured_project(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    environment = tmp_path / "runtime" / "p8" / "instances" / "production" / "compose.env"
    environment.parent.mkdir(parents=True)
    environment.write_text("V7_P8_PROJECT_NAME=v7-production\n", encoding="utf-8")
    environment.chmod(0o600)
    service = _cleanup_service_at(tmp_path, source, project="groundwater-v7", rules=())

    assert service._docker_is_frozen() is False
    assert service._protected_compose_projects() == {"v7-production"}
    candidate = service._docker_container_candidate("a" * 64, "Exited", [], True)
    assert candidate["protected_reason"] == "CANDIDATE_OR_RELEASE_REFERENCE"


def test_direct_compose_policy_fails_closed_for_invalid_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    environment = tmp_path / "runtime" / "p8" / "instances" / "production" / "compose.env"
    environment.parent.mkdir(parents=True)
    environment.write_text("V7_P8_PROJECT_NAME=not a valid Compose project\n", encoding="utf-8")
    environment.chmod(0o600)
    service = _cleanup_service_at(tmp_path, source, project="groundwater-v7", rules=())

    candidate = service._docker_container_candidate("a" * 64, "Exited", [], False)

    assert candidate["eligible"] is False
    assert candidate["protected_reason"] == "P8_DIRECT_COMPOSE_METADATA_INVALID"


def test_docker_candidate_never_deletes_a_volume_and_requires_all_three_stages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    docker_root = tmp_path / "runtime" / "docker"
    docker_root.mkdir(parents=True)
    (tmp_path / "runtime" / "archive").mkdir()
    identifier = "a" * 64
    state = tmp_path / "docker-state"
    state.write_text("present", encoding="utf-8")
    command_log = tmp_path / "docker-command-log"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            from pathlib import Path
            import sys

            state = Path({str(state)!r})
            command_log = Path({str(command_log)!r})
            identifier = {identifier!r}
            arguments = sys.argv[1:]
            previous = command_log.read_text() if command_log.exists() else ""
            command_log.write_text(previous + repr(arguments) + "\\n")
            present = state.read_text().strip() == "present"
            if arguments[:2] == ["container", "rm"]:
                if not present or arguments[-1] != identifier:
                    raise SystemExit(1)
                state.write_text("removed")
                print(identifier)
            elif arguments[:2] == ["container", "inspect"]:
                if not present:
                    raise SystemExit(1)
                template = arguments[arguments.index("--format") + 1]
                print("exited\\t" if ".State.Status" in template else "")
            elif arguments[:2] == ["ps", "-a"]:
                if "--format" in arguments and present:
                    template = arguments[arguments.index("--format") + 1]
                    if ".Image" in template:
                        print(identifier + "\\tfake-image\\tExited (0) 1 minute ago")
                    else:
                        print(identifier + "\\tExited (0) 1 minute ago")
            elif arguments[:3] == ["image", "ls", "--filter"]:
                pass
            elif arguments[:2] == ["image", "inspect"]:
                raise SystemExit(1)
            elif (
                arguments[:2] == ["system", "df"]
                or arguments[:2] == ["image", "ls"]
                or arguments[:2] == ["volume", "ls"]
            ):
                pass
            else:
                raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    fake.chmod(0o700)
    # Keep the Unix socket path below the OS length limit, even with long pytest roots.
    with (
        tempfile.TemporaryDirectory(prefix="sock-", dir=ROOT.parent / "runtime/work") as socket_dir,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as daemon,
    ):
        socket_path = Path(socket_dir) / "d"
        daemon.bind(str(socket_path))
        (tmp_path / "project-layout.json").write_text(
            json.dumps({"runtime": {"docker_socket": str(socket_path)}}),
            encoding="utf-8",
        )
        service = _cleanup_service_at(tmp_path, source, project="cleanup-fixture", rules=())
        plan = service.plan("2026-10-23-8")
        inventory = json.loads(
            (tmp_path / "operations/maintenance/cleanup/2026-10-23-8/inventory.json").read_text(
                encoding="utf-8"
            )
        )
        docker_id = next(
            item["candidate_id"]
            for item in inventory["candidates"]
            if item.get("storage_kind") == "docker"
        )
        assert plan["status"] == "PASS"
        service.stage("2026-10-23-8", docker_id)
        assert state.read_text(encoding="utf-8") == "present"
        service.verify("2026-10-23-8", docker_id)
        service.purge("2026-10-23-8", docker_id)

    assert state.read_text(encoding="utf-8") == "removed"
    command_text = command_log.read_text(encoding="utf-8")
    assert "volume', 'rm" not in command_text
    assert "builder', 'prune" not in command_text
