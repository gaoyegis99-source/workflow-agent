#!/usr/bin/env python3
"""Build the API wheel and prove it contains the external Studio artifact exactly."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SOURCE_ROOT.parent
DEFAULT_STUDIO = PROJECT_ROOT / "runtime/work/capability-studio"
WORK_ROOT = PROJECT_ROOT / "runtime/work/studio-distribution-check"
PREFIX = "platform_api/studio_static/"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("UV_REQUIRED")
    studio = Path(os.environ.get("V7_STUDIO_DIST", DEFAULT_STUDIO)).resolve(strict=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wheel-", dir=WORK_ROOT) as directory:
        output = Path(directory)
        env = dict(os.environ)
        env["V7_STUDIO_DIST"] = str(studio)
        subprocess.run(
            [
                uv,
                "build",
                "--package",
                "groundwater-platform-api",
                "--wheel",
                "--no-build-isolation",
                "--python",
                sys.executable,
                "--out-dir",
                str(output),
            ],
            cwd=SOURCE_ROOT,
            env=env,
            check=True,
        )
        wheels = list(output.glob("groundwater_platform_api-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("V7_PLATFORM_API_WHEEL_MISSING")
        expected = {
            path.relative_to(studio).as_posix(): _digest(path.read_bytes())
            for path in studio.rglob("*")
            if path.is_file()
        }
        with zipfile.ZipFile(wheels[0]) as archive:
            bundled = {
                item.filename.removeprefix(PREFIX): _digest(archive.read(item))
                for item in archive.infolist()
                if item.filename.startswith(PREFIX) and not item.is_dir()
            }
            links = [
                item.filename
                for item in archive.infolist()
                if item.filename.startswith(PREFIX) and stat.S_ISLNK(item.external_attr >> 16)
            ]
        if bundled != expected or links:
            raise RuntimeError("V7_STUDIO_WHEEL_CONTENT_MISMATCH")
        if "index.html" not in bundled or not any(name.startswith("assets/") for name in bundled):
            raise RuntimeError("V7_STUDIO_WHEEL_INCOMPLETE")
        if any(name.endswith(".map") for name in bundled):
            raise RuntimeError("V7_STUDIO_WHEEL_SOURCEMAP")
        print(f"PASS: API wheel contains {len(bundled)} exact Studio files")


if __name__ == "__main__":
    main()
