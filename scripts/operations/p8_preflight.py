#!/usr/bin/env python3
"""Read-only validation for one direct-Compose P8 control environment."""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

SERVICE_ENVIRONMENT_KEYS = (
    "V7_P8_POSTGRES_ENV_FILE",
    "V7_P8_MIGRATE_ENV_FILE",
    "V7_P8_PLATFORM_API_ENV_FILE",
    "V7_P8_DATA_API_ENV_FILE",
    "V7_P8_EXECUTION_API_ENV_FILE",
    "V7_P8_KNOWLEDGE_API_ENV_FILE",
    "V7_P8_GATEWAY_ENV_FILE",
    "V7_P8_PLATFORM_WORKER_PACKAGE_ENV_FILE",
    "V7_P8_PLATFORM_WORKER_ACTION_ENV_FILE",
    "V7_P8_PLATFORM_WORKER_ACCEPTANCE_ENV_FILE",
    "V7_P8_PLATFORM_WORKER_KNOWLEDGE_ENV_FILE",
    "V7_P8_AUTHORING_WORKER_ENV_FILE",
    "V7_P8_DATA_WORKER_ENV_FILE",
    "V7_P8_DATA_DOCUMENT_WORKER_ENV_FILE",
    "V7_P8_DATA_SPATIAL_WORKER_ENV_FILE",
    "V7_P8_EXECUTION_SUPERVISOR_ENV_FILE",
    "V7_P8_EXECUTION_NODE_1_ENV_FILE",
    "V7_P8_EXECUTION_NODE_2_ENV_FILE",
    "V7_P8_EXECUTION_PUBLICATION_ENV_FILE",
    "V7_P8_KNOWLEDGE_WORKER_ENV_FILE",
    "V7_P8_AGENT_WORKER_ENV_FILE",
    "V7_P8_BACKUP_ENV_FILE",
)

_SENSITIVE_CONTROL_SUFFIXES = ("_TOKEN", "_PASSWORD", "_DATABASE_URL", "_SECRET")
_SENSITIVE_CONTROL_KEYS = {"DEEPSEEK_API_KEY"}


class PreflightRefusal(ValueError):
    """A control/environment file is unsafe or incomplete."""


def _regular_private_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise PreflightRefusal(f"P8_{label}_INVALID")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PreflightRefusal(f"P8_{label}_PERMISSIONS_INVALID")
    return path.resolve(strict=True)


def _read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw or raw.lstrip().startswith("#"):
            continue
        key, separator, value = raw.partition("=")
        if not separator or not key or key in values or "\x00" in value:
            raise PreflightRefusal("P8_CONTROL_ENVIRONMENT_INVALID")
        values[key] = value
    return values


def validate_control_environment(project_root: Path, env_file: Path) -> dict[str, str]:
    """Validate only project-local, private control and service env files."""

    root = project_root.resolve(strict=True)
    control = _regular_private_file(env_file, label="CONTROL_ENVIRONMENT")
    try:
        control.relative_to(root / "runtime/p8/instances")
    except ValueError as error:
        raise PreflightRefusal("P8_CONTROL_ENVIRONMENT_OUTSIDE_PROJECT") from error
    values = _read_environment(control)
    if any(
        key in _SENSITIVE_CONTROL_KEYS or key.endswith(_SENSITIVE_CONTROL_SUFFIXES)
        for key in values
    ):
        raise PreflightRefusal("P8_CONTROL_ENVIRONMENT_CONTAINS_SECRET")
    for key in SERVICE_ENVIRONMENT_KEYS:
        raw = values.get(key)
        if not raw:
            raise PreflightRefusal(f"P8_SERVICE_ENVIRONMENT_MISSING:{key}")
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise PreflightRefusal(f"P8_SERVICE_ENVIRONMENT_INVALID:{key}")
        service = _regular_private_file(candidate, label="SERVICE_ENVIRONMENT")
        try:
            service.relative_to(root / "runtime/p8/instances")
        except ValueError as error:
            raise PreflightRefusal(f"P8_SERVICE_ENVIRONMENT_OUTSIDE_PROJECT:{key}") from error
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate_control_environment(args.project_root, args.env_file)
    except (OSError, UnicodeError, PreflightRefusal) as error:
        raise SystemExit(str(error)) from error
    print(
        json.dumps({"status": "PASS", "service_environment_count": len(SERVICE_ENVIRONMENT_KEYS)})
    )


if __name__ == "__main__":
    main()
