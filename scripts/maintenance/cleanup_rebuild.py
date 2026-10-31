"""Fixed, isolated rebuild recipes and append-only proof for filesystem cleanup.

No recipe is read from an audit command string. Docker cleanup has its own
state machine and cannot use these filesystem proofs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

try:
    from .cleanup_core import CleanupRefusal, _digest_json, _sha256_file
    from .project_environment import EnvironmentRefusal, child_environment
except ImportError:
    from cleanup_core import CleanupRefusal, _digest_json, _sha256_file
    from project_environment import EnvironmentRefusal, child_environment

RECIPES = frozenset({"python-caches-v1", "v7-studio-v1"})
EXCLUDED = frozenset(
    {
        ".git",
        ".venv",
        ".generated",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "payload",
    }
)
SENSITIVE = frozenset({".npmrc", ".pypirc", "credentials.json"})


def source_inputs(source: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=source,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise CleanupRefusal("REBUILD_GIT_QUERY_FAILED")
    files = {}
    for raw in sorted(set(result.stdout.split(b"\0"))):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise CleanupRefusal("REBUILD_INPUT_PATH_INVALID")
        if (
            any(part in EXCLUDED or part.startswith(".env") for part in relative.parts)
            or relative.name in SENSITIVE
            or relative.suffix in {".pem", ".key", ".p12"}
        ):
            continue
        path = source / relative
        for ancestor in [path, *path.parents]:
            if ancestor == source:
                break
            if ancestor.is_symlink():
                raise CleanupRefusal("REBUILD_INPUT_SYMLINK")
        if path.is_file():
            files[relative.as_posix()] = _sha256_file(path)
    return files


def binding(core, run_id: str, candidate_id: str, plan: dict, rule) -> dict:
    recipe = core.policy.rebuild_recipe(rule)
    if recipe not in RECIPES:
        raise CleanupRefusal("REBUILD_RECIPE_NOT_REGISTERED")
    root = core._run_root(run_id)
    isolation = core._read_record(root / "isolation" / f"{candidate_id}.json")
    verification = core._read_record(root / "verification" / f"{candidate_id}.json")
    inventory_hash = _sha256_file(root / "inventory.json")
    if isolation.get("inventory_sha256", inventory_hash) != inventory_hash:
        raise CleanupRefusal("INVENTORY_CHANGED_SINCE_STAGE")
    for record in (isolation, verification):
        if any(
            record.get(key) != value
            for key, value in {
                "project": core.project,
                "run_id": run_id,
                "candidate_id": candidate_id,
                "tree_sha256": plan["tree_sha256"],
            }.items()
        ):
            raise CleanupRefusal("REBUILD_AUDIT_BINDING_INVALID")
    if verification.get("status") != "PASS":
        raise CleanupRefusal("VERIFICATION_NOT_PASSED")
    return {
        "project": core.project,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "recipe": recipe,
        "tree_sha256": plan["tree_sha256"],
        "inventory_sha256": inventory_hash,
        "isolation_sha256": _sha256_file(root / "isolation" / f"{candidate_id}.json"),
        "verification_sha256": _sha256_file(root / "verification" / f"{candidate_id}.json"),
        "inputs_sha256": _digest_json(source_inputs(core.source_root)),
    }


def protected_fingerprints(core) -> dict[str, str]:
    paths = [core.project_root / "project-layout.json"]
    return {
        path.relative_to(core.project_root).as_posix(): core._tree_manifest(
            path, allow_internal_links=False
        )["tree_sha256"]
        for path in paths
        if path.exists()
    }


def execute_recipe(core, recipe: str, temp: Path) -> dict:
    source = core.source_root
    env = child_environment(testing=True)
    env.update(
        {
            "PYTHONPYCACHEPREFIX": str(temp / "bytecode"),
            "PYTEST_ADDOPTS": f"-o cache_dir={temp / 'pytest-cache'} --basetemp={temp / 'pytest'}",
            "RUFF_CACHE_DIR": str(temp / "ruff"),
            "UV_CACHE_DIR": str(temp / "uv-cache"),
            "npm_config_cache": str(temp / "npm-cache"),
        }
    )
    gates = []

    def run(name: str, command: list[str], cwd: Path = source) -> None:
        try:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, check=False)
            code = result.returncode
        except OSError:
            code = 127
        gates.append({"id": name, "exit_code": code, "status": "PASS" if code == 0 else "FAILED"})
        if code:
            raise CleanupRefusal("REBUILD_GATE_FAILED:" + name)

    try:
        if recipe == "v7-studio-v1":
            studio = source / "apps/capability-studio"
            copy = temp / "studio"

            def ignore(directory, names):
                return [
                    name
                    for name in names
                    if name in EXCLUDED
                    or name in SENSITIVE
                    or name.startswith(".env")
                    or Path(name).suffix in {".pem", ".key", ".p12"}
                ]

            shutil.copytree(studio, copy, ignore=ignore, symlinks=True)
            # Check all copied input entries before invoking package tooling.
            core._tree_manifest(copy, allow_internal_links=False)
            run("npm-ci", ["npm", "ci", "--no-audit", "--no-fund"], copy)
            run("studio-types", ["npm", "run", "typecheck"], copy)
            run("studio-tests", ["npm", "test"], copy)
            run(
                "studio-build",
                ["npm", "run", "build", "--", "--outDir", str(temp / "studio-output")],
                copy,
            )
        python_files = [
            str(source / name)
            for name in source_inputs(source)
            if name.endswith(".py") and Path(name).parts[0] not in {"archive", "evidence", "docs"}
        ]
        if python_files:
            run("python-bytecode", [sys.executable, "-m", "compileall", "-q", *python_files])
        fixture = temp / "cache_probe.py"
        fixture.write_text("VALUE = 1\n")
        run("ruff-cache", [sys.executable, "-m", "ruff", "check", str(fixture)])
        run(
            "cleanup-regression",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/architecture/test_cleanup_core.py",
                "tests/architecture/test_cleanup_runtime.py",
            ],
        )
        run("fast", [sys.executable, "scripts/maintenance/ops.py", "--json", "check", "fast"])
    except CleanupRefusal as error:
        return {"status": "FAILED", "gates": gates, "reason": str(error)}
    outputs = {}
    for name in ("generated", "studio-output", "bytecode", "pytest-cache", "ruff"):
        path = temp / name
        if path.exists():
            manifest = core._tree_manifest(path, allow_internal_links=False)
            outputs[name] = {
                key: manifest[key] for key in ("tree_sha256", "size_bytes", "file_count")
            }
    return {"status": "PASS", "gates": gates, "outputs": outputs}


def rebuild(core, run_id: str, candidate_id: str, plan: dict, rule) -> dict:
    expected = binding(core, run_id, candidate_id, plan, rule)
    records = core._run_root(run_id) / "rebuild" / candidate_id
    core._assert_no_symlink_ancestor(records)
    records.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while (records / f"{attempt:04d}.json").exists() or (
        records / f"{attempt:04d}.intent.json"
    ).exists():
        attempt += 1
    intent = records / f"{attempt:04d}.intent.json"
    core._write_new(intent, expected | {"created_at": datetime.now(UTC).isoformat()})
    work = core.project_root / "runtime/work/cleanup-validation"
    core._assert_no_symlink_ancestor(work)
    work.mkdir(parents=True, exist_ok=True)
    before = protected_fingerprints(core)
    try:
        with tempfile.TemporaryDirectory(prefix="rebuild-", dir=work) as directory:
            outcome = core._execute_rebuild_recipe(expected["recipe"], Path(directory))
        if (
            binding(core, run_id, candidate_id, plan, rule) != expected
            or protected_fingerprints(core) != before
        ):
            raise CleanupRefusal("REBUILD_INPUT_OR_PROTECTED_OUTPUT_CHANGED")
        core._revalidate_staged_file(run_id, candidate_id, plan)
    except (OSError, ValueError, CleanupRefusal, EnvironmentRefusal) as error:
        outcome = {"status": "FAILED", "reason": type(error).__name__}
    report = (
        expected
        | outcome
        | {
            "schema_version": "runtime-cleanup-rebuild.v1",
            "completed_at": datetime.now(UTC).isoformat(),
            "attempt": attempt,
        }
    )
    core._write_new(records / f"{attempt:04d}.json", report)
    return {
        "status": report["status"],
        "operation": "cleanup rebuild",
        "run_id": run_id,
        "candidate_id": candidate_id,
        "attempt": attempt,
        "next": "Purge is a separate explicit command and rechecks current inputs and references.",
    }


def require_proof(core, run_id: str, candidate_id: str, plan: dict, rule) -> str:
    expected = binding(core, run_id, candidate_id, plan, rule)
    directory = core._run_root(run_id) / "rebuild" / candidate_id
    core._assert_no_symlink_ancestor(directory)
    intents = sorted(directory.glob("*.intent.json"))
    if not intents:
        raise CleanupRefusal("REBUILD_PROOF_REQUIRED")
    record = intents[-1].with_name(intents[-1].name.replace(".intent.json", ".json"))
    proof = core._read_record(record)
    if proof.get("schema_version") != "runtime-cleanup-rebuild.v1" or proof.get("status") != "PASS":
        raise CleanupRefusal("REBUILD_NOT_PASSED")
    if any(proof.get(key) != value for key, value in expected.items()):
        raise CleanupRefusal("REBUILD_PROOF_STALE")
    if not proof.get("gates") or any(
        gate.get("status") != "PASS" or gate.get("exit_code") != 0 for gate in proof["gates"]
    ):
        raise CleanupRefusal("REBUILD_GATES_NOT_PASSED")
    return _sha256_file(record)
