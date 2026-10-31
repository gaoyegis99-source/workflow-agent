#!/usr/bin/env python3
"""Reject retired compatibility branches in active V7 code."""

from __future__ import annotations

import json
import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ROOTS = (
    Path("apps/execution-service"),
    Path("packages/contracts"),
    Path("packages/platform-core"),
    Path("scripts/maintenance"),
)
PRESERVED_PREFIXES = (
    Path("apps/execution-service/src/platform_execution/migrations"),
    Path("scripts/maintenance/exact_identity_migration.py"),
)
FORBIDDEN = {
    "MISSING_RELEASE_DIGEST_BYPASS": "LEGACY_WORKFLOW_BRIDGE_PREFIX",
    "UNLABELED_ENGINE_PROFILE": "_engine_profile_legacy_unlabeled",
    "LEGACY_ENGINE_STRATEGY": "legacy_v1",
    "SOURCE_CATALOG_IMAGE_FALLBACK": "source-catalogue image",
    "MERGED_IMPLEMENTATION_COMPAT": "SPECS = COMPUTE_IMPLEMENTATIONS",
}


def _preserved(relative: Path) -> bool:
    return any(
        relative == prefix or relative.is_relative_to(prefix) for prefix in PRESERVED_PREFIXES
    )


def scan(source_root: Path = SOURCE_ROOT) -> list[dict[str, str]]:
    findings = []
    self_path = Path(__file__).resolve()
    for root in ACTIVE_ROOTS:
        absolute = source_root / root
        if not absolute.exists():
            continue
        for directory, names, filenames in os.walk(absolute, followlinks=False):
            names[:] = [name for name in names if name not in {"__pycache__", ".venv"}]
            for filename in filenames:
                path = Path(directory) / filename
                if path.suffix not in {".py", ".sh"} or path.resolve() == self_path:
                    continue
                relative = path.relative_to(source_root)
                if _preserved(relative):
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                for code, marker in FORBIDDEN.items():
                    if marker in content:
                        findings.append({"code": code, "path": relative.as_posix()})
    retired = source_root / "scripts/maintenance/cleanup_runtime.py"
    if retired.exists() or retired.is_symlink():
        findings.append(
            {"code": "RETIRED_CLEANUP_FACADE", "path": retired.relative_to(source_root).as_posix()}
        )
    return sorted(findings, key=lambda item: (item["path"], item["code"]))


def main() -> None:
    findings = scan()
    print(json.dumps({"status": "PASS" if not findings else "FAILED", "findings": findings}))
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
