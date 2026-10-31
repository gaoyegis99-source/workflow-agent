"""V7-owned candidate and protection policy for runtime cleanup."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:  # Supports direct ``bin/ops`` execution and package imports.
    from .cleanup_core import CandidateRule
except ImportError:  # pragma: no cover - exercised by the script entrypoint.
    from cleanup_core import CandidateRule


SKIPPED_DISCOVERY_NAMES = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
)
COMPOSE_PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class V7CleanupPolicy:
    """V7-specific cleanup rules, including fail-closed direct-P8 protection."""

    def __init__(
        self,
        project_root: Path,
        source_root: Path,
        *,
        project: str,
        rules: Iterable[CandidateRule] | None = None,
    ) -> None:
        self.project_root = project_root
        self.source_root = source_root
        self.project = project
        self._rules = tuple(rules) if rules is not None else None

    def rebuild_recipe(self, rule: CandidateRule) -> str | None:
        if rule.candidate_id in {
            "v7.cache.pytest",
            "v7.cache.ruff",
        } or rule.candidate_id.startswith("v7.cache.pycache."):
            return "python-caches-v1"
        if rule.candidate_id == "v7.cache.frontend-node-modules":
            return "v7-studio-v1"
        return None

    def protected_paths(self) -> dict[str, str]:
        return {
            "runtime/p8": "ACTIVE_P8_RUNTIME",
            "runtime/active": "ACTIVE_RUNTIME",
            "runtime/docker": "DOCKER_DATA_ROOT",
            "runtime/toolchain/v7": "CURRENT_MAINTENANCE_TOOLCHAIN",
        }

    def _pycache_paths(self) -> list[Path]:
        paths: list[Path] = []
        for directory, names, _ in os.walk(self.source_root, followlinks=False):
            current = Path(directory)
            paths.extend(current / name for name in names if name == "__pycache__")
            names[:] = [name for name in names if name not in SKIPPED_DISCOVERY_NAMES]
        return sorted(paths)

    def rules(self) -> tuple[CandidateRule, ...]:
        if self._rules is not None:
            return self._rules
        rules = [
            CandidateRule(
                "v7.cache.frontend-node-modules",
                "source/apps/capability-studio/node_modules",
                "frontend-dependency-tree",
                ("npm --prefix source/apps/capability-studio ci",),
                allow_internal_links=True,
            ),
            CandidateRule(
                "v7.cache.pytest",
                "source/.pytest_cache",
                "test-cache",
                ("runtime/toolchain/v7/bin/python -m pytest -q",),
            ),
            CandidateRule(
                "v7.cache.ruff",
                "source/.ruff_cache",
                "lint-cache",
                ("runtime/toolchain/v7/bin/python -m ruff check .",),
            ),
        ]
        for stage in ("p3", "p4", "p7"):
            rules.append(
                CandidateRule(
                    f"v7.work.{stage}",
                    f"runtime/work/{stage}",
                    "rebuildable-runtime-work",
                    (f"bash source/scripts/checks/check_{stage}.sh",),
                    eligibility="REBUILD_PROOF_REQUIRED",
                    reason="LOCKED_REBUILD_AND_CAPABILITY_ATTESTATION_REQUIRED",
                )
            )
        rules.append(
            CandidateRule(
                "v7.toolchain.p1-toil",
                "runtime/toolchain/p1-toil",
                "optional-toolchain",
                ("uv sync --locked --project source/experiments/p1/toil",),
                eligibility="REBUILD_PROOF_REQUIRED",
                reason="P1_LOCKED_REBUILD_AND_CHECK_REQUIRED",
            )
        )
        for path in self._pycache_paths():
            relative = path.relative_to(self.project_root).as_posix()
            suffix = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
            rules.append(
                CandidateRule(
                    f"v7.cache.pycache.{suffix}",
                    relative,
                    "python-bytecode-cache",
                    ("runtime/toolchain/v7/bin/python -m compileall -q source",),
                )
            )
        return tuple(rules)

    def reference_roots(self) -> tuple[Path, ...]:
        return (
            self.source_root / "apps",
            self.source_root / "packages",
            self.source_root / "runtimes",
            self.source_root / "deploy",
            self.source_root / "scripts",
            self.project_root / "bin",
        )

    def excluded_reference_directories(self, root: Path, current: Path) -> frozenset[str]:
        if root == self.source_root / "scripts" and current == root:
            return frozenset({"maintenance"})
        return frozenset()

    def reconstruct_rule(
        self, candidate_id: str, planned: dict[str, Any] | None
    ) -> CandidateRule | None:
        relative = planned.get("source_relative") if planned else None
        if not isinstance(relative, str) or not candidate_id.startswith("v7.cache.pycache."):
            return None
        path = Path(relative)
        expected = "v7.cache.pycache." + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
        if (
            candidate_id != expected
            or not path.parts
            or path.parts[0] != "source"
            or path.name != "__pycache__"
            or path.is_absolute()
            or ".." in path.parts
        ):
            return None
        return CandidateRule(
            candidate_id,
            relative,
            "python-bytecode-cache",
            ("runtime/toolchain/v7/bin/python -m compileall -q source",),
        )

    def _active_p8_project_names(self) -> set[str]:
        """Read only direct-Compose control files; never trust old lock pointers."""

        instances = self.project_root / "runtime/p8/instances"
        if not instances.is_dir() or instances.is_symlink():
            raise ValueError("P8_DIRECT_COMPOSE_METADATA_INVALID")
        projects: set[str] = set()
        for instance in sorted(instances.iterdir()):
            if not instance.is_dir() or instance.is_symlink():
                raise ValueError("P8_DIRECT_COMPOSE_METADATA_INVALID")
            control = instance / "compose.env"
            if not control.is_file() or control.is_symlink() or control.stat().st_mode & 0o077:
                raise ValueError("P8_DIRECT_COMPOSE_METADATA_INVALID")
            names = []
            for line in control.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("V7_P8_PROJECT_NAME="):
                    names.append(line.partition("=")[2].strip())
            if len(names) != 1 or not COMPOSE_PROJECT_PATTERN.fullmatch(names[0]):
                raise ValueError("P8_DIRECT_COMPOSE_METADATA_INVALID")
            projects.add(names[0])
        if not projects:
            raise ValueError("P8_DIRECT_COMPOSE_METADATA_INVALID")
        return projects

    def docker_block_reason(self) -> str | None:
        if self.project != "groundwater-v7":
            return None
        try:
            self._active_p8_project_names()
        except ValueError:
            return "P8_DIRECT_COMPOSE_METADATA_INVALID"
        return None

    def docker_inventory_reason(self, block_reason: str | None) -> str:
        if block_reason == "P8_DIRECT_COMPOSE_METADATA_INVALID":
            return "P8 direct-Compose metadata blocks Docker-object cleanup."
        if block_reason:
            return f"Docker-object cleanup is blocked by project policy: {block_reason}."
        return (
            "Only explicit candidates can later reach docker rm; Docker volumes and "
            "build-cache pruning stay excluded."
        )

    def protected_compose_projects(self) -> set[str]:
        if self.project != "groundwater-v7" or self.docker_block_reason() is not None:
            return set()
        try:
            return self._active_p8_project_names()
        except ValueError:
            return set()
