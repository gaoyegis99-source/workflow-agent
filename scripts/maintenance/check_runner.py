"""Executable check registry and truthful aggregation; no deployment ownership."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from .project_environment import (
        EnvironmentRefusal,
        child_environment,
        owned_path,
        require_socket,
    )
except ImportError:
    from project_environment import (
        EnvironmentRefusal,
        child_environment,
        owned_path,
        require_socket,
    )


class CheckFailureError(RuntimeError):
    """Invalid check declaration or unreadable change set."""


CheckFailure = CheckFailureError


def aggregate(results: list[dict[str, Any]]) -> str:
    states = [item["status"] for item in results]
    if "FAILED" in states:
        return "FAILED"
    if any(value in {"BLOCKED", "UNAVAILABLE"} for value in states):
        return "BLOCKED"
    if not states or all(value == "PLANNED" for value in states):
        return "PLANNED"
    if "PLANNED" in states or "PARTIAL" in states:
        return "PARTIAL"
    if all(value == "PASS" for value in states):
        return "PASS"
    raise CheckFailure("CHECK_STATUS_INVALID")


def exit_code(status: str, *, plan_only: bool) -> int:
    if status == "PASS" or (status == "PLANNED" and plan_only):
        return 0
    if status in {"BLOCKED", "UNAVAILABLE"}:
        return 75
    if status in {"PLANNED", "PARTIAL", "CONFIRMATION_REQUIRED"}:
        return 2
    return 1


def changed_paths(source: Path) -> list[str]:
    paths: set[str] = set()
    # Disable rename collapsing: both the deleted and added names own checks.
    for arguments in (
        ("diff", "--cached", "--name-only", "--no-renames", "-z"),
        ("diff", "--name-only", "--no-renames", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        completed = subprocess.run(
            ["git", *arguments],
            cwd=source,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise CheckFailure("GIT_CHANGE_QUERY_FAILED")
        paths.update(os.fsdecode(name) for name in completed.stdout.split(b"\0") if name)
    return sorted(paths)


def load_registry(path: Path) -> dict[str, Any]:
    try:
        registry = json.loads(path.read_text())
        if registry["schema_version"] != 2:
            raise ValueError
        checks = registry["checks"]
        if not isinstance(checks, dict) or not checks:
            raise ValueError
        for name, item in checks.items():
            if (
                not name
                or item["kind"] not in {"local", "integration", "release"}
                or not isinstance(item["command"], list)
                or not item["command"]
                or not all(isinstance(arg, str) and arg for arg in item["command"])
                or not isinstance(item["requires"], list)
                or not all(isinstance(arg, str) for arg in item["requires"])
            ):
                raise ValueError
        for level in ("fast", "changed", "release"):
            ids = registry["levels"][level]
            if not isinstance(ids, list) or not ids or any(name not in checks for name in ids):
                raise ValueError
        if any(checks[name]["kind"] != "local" for name in registry["levels"]["fast"]):
            raise ValueError
        capabilities = registry["capabilities"]
        if not isinstance(capabilities, dict) or not isinstance(registry["paths"], list):
            raise ValueError
        for name, ids in capabilities.items():
            if not name or not ids or any(check not in checks for check in ids):
                raise ValueError
        for item in registry["paths"]:
            if not isinstance(item, dict) or not isinstance(item.get("capabilities"), list):
                raise ValueError
            for field in ("prefixes", "exact", "suffixes"):
                values = item.get(field, [])
                if not isinstance(values, list) or not all(
                    isinstance(value, str) and value for value in values
                ):
                    raise ValueError
            if (
                item["classification"] not in {"active", "documentation", "historical"}
                or not (item.get("prefixes") or item.get("exact") or item.get("suffixes"))
                or any(name not in capabilities for name in item["capabilities"])
            ):
                raise ValueError
            if item["classification"] == "active" and not item["capabilities"]:
                raise ValueError
        if "relocation_manifest" in registry:
            relocation_path = path.parents[2] / registry["relocation_manifest"]
            relocation = json.loads(relocation_path.read_text())
            registry["relocated_paths"] = {
                item["from"]: item["to"]
                for item in relocation["execution"]["tracked_file_relocations"]
            }
        return registry
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CheckFailure("CHECK_REGISTRY_INVALID") from error


def select(registry: dict, paths: list[str]) -> tuple[list[str], list[str]]:
    capabilities: set[str] = set()
    unknown: list[str] = []
    for path in paths:
        current = registry.get("relocated_paths", {}).get(path, path)
        matches = [
            item
            for item in registry["paths"]
            if current in item.get("exact", [])
            or any(current.startswith(prefix) for prefix in item.get("prefixes", []))
            or any(current.endswith(suffix) for suffix in item.get("suffixes", []))
        ]
        if any(
            item.get("suffixes") and item["classification"] == "documentation" for item in matches
        ):
            continue
        if not matches:
            unknown.append(path)
        for item in matches:
            capabilities.update(item["capabilities"])
    return sorted(capabilities), unknown


def precondition(reason: str, source: Path) -> str | None:
    if reason == "p8-direct-config":
        instances = source.parent / "runtime/p8/instances"
        if not instances.is_dir() or not any(
            path.is_file() and not path.is_symlink() for path in instances.glob("*/compose.env")
        ):
            return "P8_DIRECT_CONFIG_REQUIRED"
    elif reason == "docker":
        require_socket()
    elif reason.startswith("module:"):
        if importlib.util.find_spec(reason.removeprefix("module:")) is None:
            return "PYTHON_DEPENDENCY_MISSING"
    elif reason.startswith("command:"):
        if shutil.which(reason.removeprefix("command:")) is None:
            return "COMMAND_DEPENDENCY_MISSING"
    elif reason.startswith("path:"):
        if not (source / reason.removeprefix("path:")).exists():
            return "LOCAL_DEPENDENCY_MISSING"
    else:
        raise CheckFailure("CHECK_PRECONDITION_UNKNOWN")
    return None


def execute(command: list[str], source: Path, *, testing: bool) -> dict[str, Any]:
    env = child_environment(testing=testing)
    root = owned_path(
        source.parent / "runtime/work/maintenance-checks", source.parent / "runtime/work"
    )
    root.mkdir(parents=True, exist_ok=True)
    # Rootless scientific containers can leave namespace-owned Toil job-store
    # entries below pytest's basetemp.  Cleanup must never replace the child
    # gate's truthful PASS/FAILED/BLOCKED result with an OPS_REFUSED error.
    with tempfile.TemporaryDirectory(
        prefix="check-", dir=root, ignore_cleanup_errors=True
    ) as directory:
        temp = Path(directory)
        env.update(
            {
                "PYTHONPYCACHEPREFIX": str(temp / "bytecode"),
                "PYTEST_ADDOPTS": (
                    f"-o cache_dir={temp / 'pytest-cache'} --basetemp={temp / 'pytest'}"
                ),
                "RUFF_CACHE_DIR": str(temp / "ruff"),
            }
        )
        if source.parent.name == "groundwater-v6" or "GROUNDWATER_GENERATED_ROOT" in env:
            env["GROUNDWATER_GENERATED_ROOT"] = str(temp / "generated")
        try:
            result = subprocess.run(command, cwd=source, env=env, capture_output=True, check=False)
        except OSError:
            return {"status": "BLOCKED", "reason": "COMMAND_UNAVAILABLE"}
        return {
            "status": "PASS"
            if result.returncode == 0
            else "BLOCKED"
            if result.returncode == 75
            else "FAILED",
            "exit_code": result.returncode,
        }


def run_checks(
    source: Path, registry_path: Path, level: str, *, execute_requested: bool, plan_only: bool
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    paths = changed_paths(source) if level == "changed" else []
    capabilities, unknown = select(registry, paths)
    ids = list(registry["levels"][level])
    if level == "changed":
        ids.extend(check for name in capabilities for check in registry["capabilities"][name])
    ids = list(dict.fromkeys(ids))
    results: list[dict[str, Any]] = []
    for check_id in ids:
        spec = registry["checks"][check_id]
        command = [sys.executable if part == "{python}" else part for part in spec["command"]]
        item: dict[str, Any] = {
            "check_id": check_id,
            "kind": spec["kind"],
            "command": " ".join(spec["command"]),
        }
        try:
            reasons = []
            for rule in spec["requires"]:
                reason = precondition(rule, source)
                if reason:
                    reasons.append(reason)
                    break
        except EnvironmentRefusal as error:
            reasons = [str(error)]
        if reasons:
            item.update(status="BLOCKED", reason=reasons[0])
        elif plan_only or (
            not execute_requested and (level == "release" or spec["kind"] != "local")
        ):
            item.update(status="PLANNED")
        else:
            item.update(execute(command, source, testing=spec["kind"] != "release"))
        results.append(item)
    if unknown:
        results.append(
            {
                "check_id": "change-coverage",
                "command": "classify changed paths",
                "status": "FAILED",
                "reason": "UNMAPPED_CHANGE",
            }
        )
    blocked_ids = {item["check_id"] for item in results if item["status"] == "BLOCKED"}
    return {
        "status": aggregate(results),
        "operation": f"check {level}",
        "gates": ids,
        "commands": results,
        "changed_path_count": len(paths),
        "changed_path_sample": paths[:20],
        "affected_capabilities": capabilities,
        "affected_scopes": capabilities,
        "blocked_capabilities": [
            name
            for name in capabilities
            if blocked_ids.intersection(registry["capabilities"][name])
        ],
        "pending_checks": [item["check_id"] for item in results if item["status"] == "PLANNED"],
        "blocked_checks": [item for item in results if item["status"] == "BLOCKED"],
        "unmapped_paths": unknown,
        "next": (
            "Integration/release checks require --execute and their prerequisites; "
            "PASS means all selected checks ran."
        ),
    }
