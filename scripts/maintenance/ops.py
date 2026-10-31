#!/usr/bin/env python3
"""Stable, human-oriented maintenance entrypoint for groundwater-v7.

The command is intentionally a dispatcher, not a second deployment system.
It preserves classified implementation scripts, never prints tokens, and
keeps direct-Compose maintenance explicit rather than importing a Release Lock.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:  # Supports both ``bin/ops`` script execution and package imports in tests.
    from .cleanup_core import CleanupCore, CleanupRefusal
    from .cleanup_policy import V7CleanupPolicy
except ImportError:  # pragma: no cover - exercised by the script entrypoint.
    from cleanup_core import CleanupCore, CleanupRefusal
    from cleanup_policy import V7CleanupPolicy


try:
    from . import check_runner, retire_p8_runtime
    from .exact_identity_migration import ExactIdentityMigration, ExactIdentityRefusal
    from .project_environment import EnvironmentRefusal, child_environment
except ImportError:
    import check_runner
    import retire_p8_runtime
    from exact_identity_migration import ExactIdentityMigration, ExactIdentityRefusal
    from project_environment import EnvironmentRefusal, child_environment

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SOURCE_ROOT.parent
PYTHON = sys.executable
GATES_PATH = SOURCE_ROOT / "docs/maintenance/maintenance-gates.v2.json"


def cleanup_service() -> CleanupCore:
    """Construct the cleanup core directly with the V7-owned policy."""

    return CleanupCore(
        PROJECT_ROOT,
        SOURCE_ROOT,
        project="groundwater-v7",
        policy=V7CleanupPolicy(PROJECT_ROOT, SOURCE_ROOT, project="groundwater-v7"),
    )


class OpsFailure(RuntimeError):
    """Expected safe refusal from the project maintenance boundary."""


def _command_label(command: Sequence[str]) -> str:
    values = [Path(value).name if index == 0 else value for index, value in enumerate(command)]
    return " ".join(values)


def _run(command: Sequence[str], *, execute: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"command": _command_label(command)}
    if not execute:
        return result | {"status": "PLANNED"}
    completed = subprocess.run(
        list(command),
        cwd=SOURCE_ROOT,
        env=child_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    # Internal commands may inspect local runtime configuration.  Keep the
    # summarized result safe for terminals and machine consumers.
    status = (
        "PASS"
        if completed.returncode == 0
        else "BLOCKED"
        if completed.returncode == 75
        else "FAILED"
    )
    return result | {"status": status, "exit_code": completed.returncode}


def _git_lines(*arguments: str) -> list[str]:
    completed = subprocess.run(
        ["git", *arguments], cwd=SOURCE_ROOT, capture_output=True, text=True, check=False
    )
    if completed.returncode:
        raise check_runner.CheckFailure("GIT_QUERY_FAILED")
    return completed.stdout.splitlines()


def _gate_names(level: str) -> list[str]:
    return check_runner.load_registry(GATES_PATH)["levels"][level]


def status() -> dict[str, Any]:
    layout = json.loads((PROJECT_ROOT / "project-layout.json").read_text(encoding="utf-8"))
    runtime = layout.get("runtime", {})
    return {
        "status": "PASS",
        "project": "groundwater-v7",
        "source": str(SOURCE_ROOT),
        "git_dirty_entries": len(_git_lines("status", "--porcelain")),
        "p8_deployment_mode": "direct-compose",
        "runtime": {
            name: (PROJECT_ROOT / relative).is_dir()
            for name, relative in {
                "work": runtime.get("work", "runtime/work"),
                "active": runtime.get("active", "runtime/active"),
                "archive": runtime.get("archive", "runtime/archive"),
                "docker": runtime.get("docker", "runtime/docker"),
                "p8": runtime.get("p8", "runtime/p8"),
            }.items()
        },
        "note": "Read-only status; direct Compose configuration is not started or modified.",
    }


def check(level: str, *, execute: bool, plan_only: bool) -> dict[str, Any]:
    return check_runner.run_checks(
        SOURCE_ROOT,
        GATES_PATH,
        level,
        execute_requested=execute,
        plan_only=plan_only,
    )


def identity_whoami(url: str | None) -> dict[str, Any]:
    endpoint = url or os.environ.get("V7_OPS_URL", "http://127.0.0.1:18080")
    target = endpoint.rstrip("/") + "/api/v1/me"
    headers = {"Accept": "application/json"}
    token = os.environ.get("V7_USER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        return {
            "status": "UNAVAILABLE",
            "operation": "identity whoami",
            "note": "Current identity could not be read from the configured local endpoint "
            f"({type(error).__name__}).",
        }
    # Project membership is useful identity context; never echo any request
    # credentials or arbitrary server fields.
    return {
        "status": "PASS",
        "operation": "identity whoami",
        "principal_id": payload.get("principal_id"),
        "display_name": payload.get("display_name"),
        "authentication_mode": payload.get("authentication_mode"),
        "subject": payload.get("subject"),
        "projects": payload.get("projects", []),
    }


def identity_bind(
    principal_id: str | None,
    subject: str | None,
    reviewer: str | None,
    reason: str | None,
    *,
    execute: bool,
) -> dict[str, Any]:
    if not execute:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "operation": "identity bind",
            "note": "Pass --execute with --principal-id, --subject, --reviewer, and --reason "
            "after verifying the existing Principal and project memberships.",
        }
    if not principal_id or not subject or not reviewer or not reason:
        raise OpsFailure(
            "identity bind requires --principal-id, --subject, --reviewer, and --reason"
        )
    if not os.environ.get("V7_DATABASE_URL"):
        raise OpsFailure(
            "identity bind requires V7_DATABASE_URL in the local administrator environment"
        )
    command = [
        PYTHON,
        "-m",
        "platform_infrastructure.admin",
        "principal-bind-subject",
        "--principal-id",
        principal_id,
        "--subject",
        subject,
        "--reviewer",
        reviewer,
        "--reason",
        reason,
    ]
    result = _run(command, execute=True)
    # Subject and reason are audit data, not general terminal output.
    result["command"] = "platform_infrastructure.admin principal-bind-subject"
    return {
        "status": result["status"],
        "operation": "identity bind",
        "principal_id": principal_id,
        "commands": [result],
    }


def _p8_env_file(value: str | None) -> Path:
    if not value:
        raise OpsFailure("P8_DIRECT_CONFIG_REQUIRED: pass --env-file within runtime/p8/instances")
    path = Path(value).expanduser().resolve()
    allowed = (PROJECT_ROOT / "runtime/p8/instances").resolve()
    if not path.is_file() or path.is_symlink() or not path.is_relative_to(allowed):
        raise OpsFailure("P8_DIRECT_CONFIG_INVALID: expected a regular project-local compose.env")
    return path


def runtime(
    action: str,
    env_file: str | None,
    backup_path: str | None,
    *,
    execute: bool,
    phase: str | None = None,
    run_id: str | None = None,
    backup_root: str | None = None,
    secret_escrow_receipt: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    if action == "retire-p8":
        if phase not in {"plan", "backup-verify", "stage", "verify", "purge"}:
            raise OpsFailure(
                "runtime retire-p8 requires plan, backup-verify, stage, verify, or purge"
            )
        if not run_id:
            raise OpsFailure("runtime retire-p8 requires --run-id")
        if phase in {"stage", "purge"} and not execute:
            return {
                "status": "CONFIRMATION_REQUIRED",
                "operation": f"runtime retire-p8 {phase}",
                "note": (
                    "Pass --execute only after the cold-backup, recovery, escrow, "
                    "and inventory records have been reviewed."
                ),
            }
        if phase in {"plan", "backup-verify", "verify"} and execute:
            raise OpsFailure(f"runtime retire-p8 {phase} is read-only and rejects --execute")
        return retire_p8_runtime.retire(
            phase,
            run_id,
            backup_root=backup_root,
            secret_escrow_receipt=secret_escrow_receipt,
            scope=scope,
        )
    if (
        phase is not None
        or run_id is not None
        or backup_root is not None
        or secret_escrow_receipt is not None
        or scope is not None
    ):
        raise OpsFailure("runtime options for retire-p8 cannot be used with direct-Compose actions")
    if not execute:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "operation": f"runtime {action}",
            "note": "Pass --execute only after a cold backup and recovery prechecks have passed.",
        }
    environment = _p8_env_file(env_file)
    command = [str(PROJECT_ROOT / "bin/p8"), action, str(environment)]
    if action in {"backup", "restore"}:
        if not backup_path:
            raise OpsFailure(f"runtime {action} requires --backup-path")
        command.append(str(Path(backup_path).expanduser()))
    result = _run(command, execute=True)
    return {"status": result["status"], "operation": f"runtime {action}", "commands": [result]}


def release(
    action: str,
    env_file: str | None,
    *,
    execute: bool,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Run the direct-Compose maintenance steps without a lock-era upgrade path."""

    environment = _p8_env_file(env_file)
    command_action = {"plan": "preflight", "verify": "status", "apply": "up"}[action]
    command = [str(PROJECT_ROOT / "bin/p8"), command_action, str(environment)]
    if action == "apply" and not execute:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "operation": "release apply",
            "note": "Pass --execute only after maintenance mode, a verified cold backup, "
            "and a recovery record are complete.",
        }
    result = _run(command, execute=execute and not plan_only)
    result["check_id"] = f"direct-compose-{command_action}"
    return {
        "status": check_runner.aggregate([result]),
        "operation": f"release {action}",
        "commands": [result],
        "pending_checks": [result["check_id"]] if result["status"] == "PLANNED" else [],
    }


def cleanup(
    action: str, run_id: str | None, candidate: str | None, *, execute: bool
) -> dict[str, Any]:
    """Dispatch the project-local staged cleanup flow without touching P8."""
    runner = cleanup_service()
    if action == "plan":
        return runner.plan(run_id)
    if action == "verify":
        if not run_id or not candidate:
            raise OpsFailure("cleanup verify requires --run-id and --candidate from a prior plan")
        return runner.verify(run_id, candidate)
    if not execute:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "operation": f"cleanup {action}",
            "note": (
                "Pass --execute with a planned --run-id and --candidate; "
                "arbitrary paths and P8 are never accepted."
            ),
        }
    if not run_id or not candidate:
        raise OpsFailure(f"cleanup {action} requires --run-id and --candidate from a prior plan")
    if action == "rebuild":
        return runner.rebuild(run_id, candidate)
    return runner.stage(run_id, candidate) if action == "stage" else runner.purge(run_id, candidate)


def migrate_exact_identity(
    action: str,
    run_id: str | None,
    backup_id: str | None,
    *,
    execute: bool,
) -> dict[str, Any]:
    """Run the fail-closed exact-identity retirement workflow."""
    if action == "apply" and not execute:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "operation": "migrate exact-identity apply",
            "note": "Pass --execute only during the write-frozen maintenance window after "
            "a verified cold backup and deterministic migration plan.",
        }
    migration = ExactIdentityMigration(SOURCE_ROOT)
    if action == "plan":
        if execute:
            raise OpsFailure("migrate exact-identity plan is read-only and rejects --execute")
        return migration.plan(run_id)
    if not run_id:
        raise OpsFailure(f"migrate exact-identity {action} requires --run-id")
    if action == "verify":
        if execute:
            raise OpsFailure("migrate exact-identity verify is read-only and rejects --execute")
        return migration.verify(run_id)
    return migration.apply(run_id, backup_id)


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    title = payload.get("operation", payload.get("project", "V7 ops"))
    print(f"[{payload.get('status', 'UNKNOWN')}] {title}")
    for key in ("note", "next", "code", "reason"):
        if payload.get(key):
            print(f"{key}: {payload[key]}")
    if payload.get("affected_capabilities"):
        print("affected capabilities: " + ", ".join(payload["affected_capabilities"]))
    for command in payload.get("commands", []):
        label = command.get("check_id", command["command"])
        detail = f" ({command['reason']})" if command.get("reason") else ""
        print(f"- {command['status']}: {label}{detail}")
    if payload.get("pending_checks"):
        print("pending checks: " + ", ".join(payload["pending_checks"]))


def _parse(argv: list[str]) -> tuple[argparse.Namespace, bool, bool, bool]:
    json_output = "--json" in argv
    execute = "--execute" in argv
    plan = "--plan" in argv
    filtered = [item for item in argv if item not in {"--json", "--execute", "--plan"}]
    parser = argparse.ArgumentParser(prog="bin/ops", description=__doc__)
    if plan and execute:
        parser.error("--plan and --execute are mutually exclusive")
    subcommands = parser.add_subparsers(dest="area", required=True)
    subcommands.add_parser("status")
    check_parser = subcommands.add_parser("check")
    check_parser.add_argument("level", choices=("fast", "changed", "release"))
    identity_parser = subcommands.add_parser("identity")
    identity_parser.add_argument("action", choices=("whoami", "bind"))
    identity_parser.add_argument("--url")
    identity_parser.add_argument("--principal-id")
    identity_parser.add_argument("--subject")
    identity_parser.add_argument("--reviewer")
    identity_parser.add_argument("--reason")
    runtime_parser = subcommands.add_parser("runtime")
    runtime_parser.add_argument(
        "action", choices=("backup", "restore", "start", "stop", "retire-p8")
    )
    runtime_parser.add_argument("phase", nargs="?")
    runtime_parser.add_argument("--env-file")
    runtime_parser.add_argument("--backup-path")
    runtime_parser.add_argument("--run-id")
    runtime_parser.add_argument("--backup-root")
    runtime_parser.add_argument("--secret-escrow-receipt")
    runtime_parser.add_argument("--scope", choices=("metadata", "docker-data"))
    release_parser = subcommands.add_parser("release")
    release_parser.add_argument("action", choices=("plan", "verify", "apply"))
    release_parser.add_argument("--env-file")
    cleanup_parser = subcommands.add_parser("cleanup")
    cleanup_parser.add_argument("action", choices=("plan", "stage", "verify", "rebuild", "purge"))
    cleanup_parser.add_argument("--run-id")
    cleanup_parser.add_argument("--candidate")
    migrate_parser = subcommands.add_parser("migrate")
    migrate_parser.add_argument("migration", choices=("exact-identity",))
    migrate_parser.add_argument("action", choices=("plan", "apply", "verify"))
    migrate_parser.add_argument("--run-id")
    migrate_parser.add_argument("--backup-id")
    args = parser.parse_args(filtered)
    if plan and not (args.area == "check" or args.area == "release"):
        parser.error("--plan is supported only by check and release")
    return args, json_output, execute, plan


def main(argv: list[str] | None = None) -> None:
    args, json_output, execute, plan = _parse(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.area == "status":
            payload = status()
        elif args.area == "check":
            payload = check(args.level, execute=execute, plan_only=plan)
        elif args.area == "identity":
            payload = (
                identity_whoami(args.url)
                if args.action == "whoami"
                else identity_bind(
                    args.principal_id,
                    args.subject,
                    args.reviewer,
                    args.reason,
                    execute=execute,
                )
            )
        elif args.area == "runtime":
            payload = runtime(
                args.action,
                args.env_file,
                args.backup_path,
                execute=execute,
                phase=args.phase,
                run_id=args.run_id,
                backup_root=args.backup_root,
                secret_escrow_receipt=args.secret_escrow_receipt,
                scope=args.scope,
            )
        elif args.area == "cleanup":
            payload = cleanup(args.action, args.run_id, args.candidate, execute=execute)
        elif args.area == "migrate":
            payload = migrate_exact_identity(
                args.action,
                args.run_id,
                args.backup_id,
                execute=execute,
            )
        else:
            payload = release(
                args.action,
                args.env_file,
                execute=execute,
                plan_only=plan,
            )
    except (
        OpsFailure,
        CleanupRefusal,
        check_runner.CheckFailure,
        EnvironmentRefusal,
        ExactIdentityRefusal,
        retire_p8_runtime.P8RetirementRefusal,
        OSError,
        json.JSONDecodeError,
    ) as error:
        message = str(error)
        blocked = message.startswith(
            (
                "P8_DIRECT_CONFIG_REQUIRED",
                "PROJECT_DOCKER_UNAVAILABLE",
                "P8_ACTIVE_CONTAINER_PRESENT",
                "P8_RUNTIME_UNREADABLE",
            )
        )
        payload = {
            "status": "BLOCKED" if blocked else "FAILED",
            "code": "P8_DIRECT_CONFIG_REQUIRED" if blocked else "OPS_REFUSED",
            "note": message,
        }
    _emit(payload, json_output=json_output)
    if args.area == "check" or (args.area == "release" and args.action == "verify"):
        raise SystemExit(check_runner.exit_code(payload["status"], plan_only=plan))
    if payload["status"] in {"FAILED", "REFUSED", "UNAVAILABLE"}:
        raise SystemExit(1)
    if payload["status"] == "BLOCKED":
        raise SystemExit(75)
    if payload["status"] == "CONFIRMATION_REQUIRED":
        raise SystemExit(2)
    if (
        payload["status"] in {"PARTIAL", "PLANNED"}
        and args.area == "runtime"
        and args.action == "retire-p8"
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
