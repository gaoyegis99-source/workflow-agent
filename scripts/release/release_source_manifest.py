"""Create the deterministic source identity used by release evidence and image labels."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOT_FILES = ("pyproject.toml", "uv.lock")
SOURCE_ROOTS = (
    "apps",
    "packages",
    "deploy",
    "examples",
    "runtimes/cwl",
    "scripts",
    "tests/knowledge/core",
    "tests/knowledge/research",
    "tests/knowledge/acceptance",
    "tests/operators/catalog",
    "tests/operators/tabular/core",
    "tests/operators/tabular/acceptance",
    "tests/operators/project",
    "tests/operators/filters",
    "tests/operators/cast",
    "tests/operators/aggregate",
    "tests/release/restore",
    "tests/release/upgrade",
)
IGNORED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vite",
    ".vitest-cache",
    "node_modules",
    "coverage",
    "dist",
    "build",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def source_files():
    files = [ROOT / name for name in ROOT_FILES]
    for name in SOURCE_ROOTS:
        files.extend(path for path in (ROOT / name).rglob("*") if path.is_file())
    return sorted(
        (
            path
            for path in files
            if not IGNORED_PARTS.intersection(path.relative_to(ROOT).parts)
            and path.suffix not in IGNORED_SUFFIXES
        ),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )


def manifest():
    entries = []
    for path in source_files():
        entries.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "algorithm": "sha256",
        "file_count": len(entries),
        "source_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "files": entries,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--digest-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    value = manifest()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(value, indent=2) + "\n")
    if args.digest_only or args.output:
        print(value["source_digest"])
    else:
        print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
