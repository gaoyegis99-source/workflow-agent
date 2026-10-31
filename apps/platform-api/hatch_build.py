"""Attach the externally built Capability Studio bundle to the API wheel."""

from __future__ import annotations

import os
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _studio_source(project_root: Path) -> Path:
    configured = os.environ.get("V7_STUDIO_DIST")
    if configured:
        return Path(configured).expanduser().resolve(strict=True)
    return (project_root.parents[2] / "runtime/work/capability-studio").resolve(strict=True)


def _validate_bundle(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("V7_STUDIO_DIST_INVALID")
    index = root / "index.html"
    assets = root / "assets"
    if not index.is_file() or index.is_symlink() or not assets.is_dir() or assets.is_symlink():
        raise RuntimeError("V7_STUDIO_DIST_INCOMPLETE")
    files = [path for path in root.rglob("*") if path.is_file()]
    if not files or not any(path.is_relative_to(assets) for path in files):
        raise RuntimeError("V7_STUDIO_DIST_INCOMPLETE")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise RuntimeError("V7_STUDIO_DIST_SYMLINK")
    if any(path.suffix == ".map" for path in files):
        raise RuntimeError("V7_STUDIO_DIST_SOURCEMAP")


class StudioBuildHook(BuildHookInterface):
    """Force an already verified bundle into the wheel without touching source."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version
        source = _studio_source(Path(self.root))
        _validate_bundle(source)
        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise RuntimeError("V7_STUDIO_BUILD_DATA_INVALID")
        force_include[str(source)] = "platform_api/studio_static"
