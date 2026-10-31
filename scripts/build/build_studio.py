#!/usr/bin/env python3
"""Build and validate Capability Studio outside the Git source tree."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SOURCE_ROOT.parent
STUDIO = SOURCE_ROOT / "apps/capability-studio"
DEFAULT_OUTPUT = PROJECT_ROOT / "runtime/work/capability-studio"


def _run(*args: str) -> None:
    subprocess.run(args, cwd=STUDIO, check=True)


def validate_bundle(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    if root.is_relative_to(SOURCE_ROOT) or root.is_symlink():
        raise RuntimeError("V7_STUDIO_OUTPUT_MUST_BE_OUTSIDE_SOURCE")
    index = root / "index.html"
    assets = root / "assets"
    if not index.is_file() or index.is_symlink() or not assets.is_dir() or assets.is_symlink():
        raise RuntimeError("V7_STUDIO_OUTPUT_INCOMPLETE")
    entries = list(root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise RuntimeError("V7_STUDIO_OUTPUT_SYMLINK")
    files = sorted(path for path in entries if path.is_file())
    if not any(path.is_relative_to(assets) for path in files):
        raise RuntimeError("V7_STUDIO_OUTPUT_INCOMPLETE")
    if any(path.suffix == ".map" for path in files):
        raise RuntimeError("V7_STUDIO_OUTPUT_SOURCEMAP")
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-checks", action="store_true")
    args = parser.parse_args()
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("NPM_REQUIRED")
    output = args.output.expanduser().resolve()
    if output.is_relative_to(SOURCE_ROOT):
        raise SystemExit("V7_STUDIO_OUTPUT_MUST_BE_OUTSIDE_SOURCE")
    if not args.skip_install:
        _run(npm, "ci", "--no-audit", "--no-fund")
    if not args.skip_checks:
        _run(npm, "run", "typecheck")
        _run(npm, "test")
    _run(npm, "run", "build", "--", "--outDir", str(output))
    files = validate_bundle(output)
    print(f"PASS: Capability Studio built at {output} ({len(files)} files)")


if __name__ == "__main__":
    main()
