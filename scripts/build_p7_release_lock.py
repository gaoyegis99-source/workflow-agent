#!/usr/bin/env python3
"""Build the canonical P7 deployment lock from exact catalogues and artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from platform_contracts.release import P8_DATABASE_SCHEMAS, digest, file_digest, load_release_lock
from platform_core.application.catalog import load_catalog as load_operator_catalog
from platform_core.application.checker_catalog import load_catalog as load_checker_catalog
from platform_core.application.tool_catalog import load_tool_catalog

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_WHEELS = {
    "groundwater_agent_runtime",
    "groundwater_checker_pack",
    "groundwater_data_service",
    "groundwater_execution_service",
    "groundwater_knowledge_service",
    "groundwater_operator_pack",
    "groundwater_platform_api",
    "groundwater_platform_contracts",
    "groundwater_platform_core",
    "groundwater_platform_infrastructure",
    "groundwater_tabular_operator_pack",
}


def source_identity() -> dict:
    # Keep the release lock and app image label tied to exactly the same files.
    # The manifest is also the source of truth for the Docker build argument.
    sys.path.insert(0, str(ROOT))
    from scripts.release_source_manifest import manifest

    identity = manifest()
    return {
        "digest": identity["source_digest"],
        "file_count": identity["file_count"],
    }


def required_image(name: str) -> str:
    value = os.environ.get(name)
    if not (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    ):
        raise SystemExit(f"{name} must be an immutable image digest")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-catalog", type=Path, required=True)
    parser.add_argument("--checker-catalog", type=Path, required=True)
    parser.add_argument("--tool-catalog", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--dependency-dir", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--schema-version", type=int, choices=(2, 3), default=2)
    parser.add_argument("--model-catalog", type=Path)
    parser.add_argument("--runtime-profile", type=Path)
    parser.add_argument("--upgrade-from-lock", type=Path, action="append", default=[])
    parser.add_argument("--app-image-ref")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    images = {
        "operator": required_image("V7_OPERATOR_IMAGE"),
        "qgis": required_image("V7_SPATIAL_QGIS_IMAGE_ID"),
        "spatial_native": required_image("V7_SPATIAL_NATIVE_IMAGE_ID"),
        "checker": required_image("V7_CHECKER_IMAGE"),
        "gdal_checker": required_image("V7_GDAL_CHECKER_IMAGE"),
        "cwl": required_image("V7_CWL_IMAGE_ID"),
        "tabular": required_image("V7_TABULAR_OPERATOR_IMAGE"),
    }
    app_image = os.environ.get("V7_APP_IMAGE")
    if app_image:
        images["app"] = required_image("V7_APP_IMAGE")
    postgres_image = os.environ.get("V7_POSTGRES_IMAGE")
    if postgres_image:
        images["postgres"] = required_image("V7_POSTGRES_IMAGE")
    operators = load_operator_catalog(args.operator_catalog)
    tools = load_tool_catalog(args.tool_catalog)
    checkers = load_checker_catalog(args.checker_catalog)
    wheels = {
        path.name: file_digest(path)
        for path in sorted(args.wheel_dir.glob("*.whl"))
        if f"-{args.version}-" in path.name
    }
    if not wheels:
        raise SystemExit("No release wheels found")
    found = {name.split(f"-{args.version}-", 1)[0] for name in wheels}
    if found != REQUIRED_WHEELS:
        missing = sorted(REQUIRED_WHEELS - found)
        extra = sorted(found - REQUIRED_WHEELS)
        raise SystemExit(f"Release wheel set differs; missing={missing} extra={extra}")
    studio = ROOT / "apps/platform-api/src/platform_api/studio_static"
    web_assets = {
        path.relative_to(studio).as_posix(): file_digest(path)
        for path in sorted(studio.rglob("*"))
        if path.is_file()
    }
    if "index.html" not in web_assets or not any(
        name.startswith("assets/") and name.endswith(".js") for name in web_assets
    ):
        raise SystemExit("Capability Studio production bundle is incomplete")
    unsigned = {
        "schema_version": args.schema_version,
        "version": args.version,
        "operator_catalog": {
            "digest": operators["catalog_digest"],
            "file_sha256": file_digest(args.operator_catalog),
        },
        "checker_catalog": {
            "digest": checkers["catalog_digest"],
            "file_sha256": file_digest(args.checker_catalog),
        },
        "tool_catalog": {
            "catalog_id": tools["catalog_id"],
            "digest": tools["catalog_digest"],
            "file_sha256": file_digest(args.tool_catalog),
        },
        "images": images,
        "source_tree": source_identity(),
        "wheels": wheels,
        "web_assets": web_assets,
    }
    if args.dependency_dir is not None:
        dependencies = {
            path.name: file_digest(path) for path in sorted(args.dependency_dir.glob("*.whl"))
        }
        if not dependencies:
            raise SystemExit("No locked dependency wheels found")
        unsigned["dependency_wheels"] = dependencies
    if args.schema_version == 3:
        if args.model_catalog is None or args.runtime_profile is None or not args.app_image_ref:
            raise SystemExit(
                "Release lock v3 requires model catalog, runtime profile and app image reference"
            )
        model_catalog = json.loads(args.model_catalog.read_text(encoding="utf-8"))
        runtime_profile = json.loads(args.runtime_profile.read_text(encoding="utf-8"))
        recovery = {}
        upgrade_from = []
        for path in args.upgrade_from_lock:
            raw = json.loads(path.read_text(encoding="utf-8"))
            previous = load_release_lock(path, raw.get("release_digest", ""))
            previous_version = previous.get("version")
            previous_model = path.parent / "model-catalog.json"
            previous_runtime = path.parent / "agent-runtime-profile.json"
            if not previous_model.is_file() or not previous_runtime.is_file():
                raise SystemExit("Upgrade source model catalog or runtime profile is missing")
            if not isinstance(previous_version, str) or previous_version in recovery:
                raise SystemExit("Upgrade source releases must have unique versions")
            upgrade_from.append(previous_version)
            recovery[previous_version] = {
                "release_digest": previous["release_digest"],
                "catalogs": {
                    name: previous[name]["digest"]
                    for name in ("operator_catalog", "checker_catalog", "tool_catalog")
                },
                "images": previous["images"],
                "model_catalog": previous.get("model_catalog", {}).get(
                    "file_sha256", file_digest(previous_model)
                ),
                "runtime_profile": previous.get("runtime_profile", {}).get(
                    "file_sha256", file_digest(previous_runtime)
                ),
            }
        if not recovery:
            raise SystemExit("Release lock v3 requires at least one upgrade source release")
        unsigned |= {
            "model_catalog": {
                "digest": model_catalog.get("catalog_digest", digest(model_catalog)),
                "file_sha256": file_digest(args.model_catalog),
            },
            "runtime_profile": {
                "digest": runtime_profile.get("profile_digest", digest(runtime_profile)),
                "file_sha256": file_digest(args.runtime_profile),
            },
            "image_refs": {
                name: (
                    args.app_image_ref
                    if name == "app"
                    else (
                        "postgres:17.6"
                        if name == "postgres"
                        else f"groundwater-v7-{name}:{args.version}"
                    )
                )
                for name in images
            },
            "compatibility": {
                "upgrade_from": sorted(upgrade_from),
                "api_contract": "api/v1",
                "backup_formats": [1, 2],
                "database_schemas": P8_DATABASE_SCHEMAS,
                "recovery_releases": recovery,
            },
            "runtime_requirements": {
                "docker": ">=27,<30",
                "compose": ">=2.30,<3",
                "postgresql": "17",
                "python": ">=3.12,<3.13",
            },
        }
    value = unsigned | {"release_digest": digest(unsigned)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(value["release_digest"])


if __name__ == "__main__":
    main()
