#!/usr/bin/env python3
"""Run P5-R2 scientific capabilities in their real locked containers."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from platform_core.application.checker_catalog import build_catalog, load_catalog
from platform_core.application.open_acceptance import compile_coverage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.p5r1.test_planning import registration  # noqa: E402
from tests.p5r2.test_cartographic_capability import fixture as map_fixture  # noqa: E402
from tests.p5r2.test_catalog_and_planning import r2_spec  # noqa: E402
from tests.p5r2.test_p3_capability import fixture as p3_fixture  # noqa: E402


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def writable(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)


def run(image, source, output, *args):
    writable(output)
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        "-v",
        f"{source}:/input:ro",
        "-v",
        f"{output}:/output",
        image,
        *args,
    ]
    subprocess.run(command, check=True, timeout=180)


def p3_check(root, images):
    source, output = root / "p3-input", root / "p3-output"
    writable(source)
    request, _, _ = p3_fixture(source)
    (source / "request.json").write_text(json.dumps(request), encoding="utf-8")
    for path in source.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    run(images["CHECKER"], source, output)
    report = json.loads((output / "check.json").read_text())
    if report["status"] != "PASS":
        raise RuntimeError("P3 Decimal capability did not pass")


def cartographic_check(root, images):
    source, output = root / "map-input", root / "map-output"
    writable(source)
    request = map_fixture(source)
    (source / "request.json").write_text(json.dumps(request), encoding="utf-8")
    for path in source.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    run(images["CHECKER"], source, output)
    report = json.loads((output / "check.json").read_text())
    if report["status"] != "PASS":
        raise RuntimeError("Cartographic product capability did not pass")


def inspect_vector(root, native_image):
    source = ROOT / "tests/p42/fixtures/points-utm.gpkg"
    stage, output = root / "inspect-input", root / "inspect-output"
    writable(stage)
    writable(output)
    shutil.copyfile(source, stage / "vector.gpkg")
    (stage / "vector.gpkg").chmod(0o644)
    run(
        native_image,
        stage,
        output,
        "platform-spatial",
        "inspect",
        "--format",
        "geopackage",
        "--input",
        "/input/vector.gpkg",
        "--output",
        "/output/manifest.json",
    )
    return json.loads((output / "manifest.json").read_text())


def spatial_request(root, catalog, descriptor):
    source = root / "spatial-input"
    writable(source / "roles/vector")
    vector = source / "roles/vector/vector.gpkg"
    shutil.copyfile(ROOT / "tests/p42/fixtures/points-utm.gpkg", vector)
    vector.chmod(0o644)
    representation = {
        "representation_id": "vector",
        "format": "geopackage",
        "digest": digest(vector),
        "size": vector.stat().st_size,
        "schema_id": "spatial-vector-v1",
    }
    manifest = descriptor | {
        "representations": [representation],
        "source": {
            "target": {
                "kind": "OPERATOR",
                "operator_id": "qgis-buffer",
                "version_id": "1.0.0",
            },
            "task_ref": {"task_id": "task", "spec_revision": 1},
            "execution_id": "spatial-execution",
        },
    }
    ref = {
        "project_id": "project",
        "asset_id": "spatial-asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "c" * 64,
    }
    output_ref = {
        "kind": "OPERATOR_OUTPUT",
        "action_id": "spatial-action",
        "execution_id": "spatial-execution",
        "output_port": "vector",
        "asset_ref": ref,
        "representation_id": "vector",
        "representation_digest": representation["digest"],
        "schema_id": "spatial-vector-v1",
        "schema_source": "MANIFEST",
    }
    candidate = {
        "digest": "spatial-candidate",
        "spec_revision": 1,
        "identity_mode": "EXACT",
        "payload": {
            "identity_mode": "EXACT",
            "artifacts": [{"ref": ref, "manifest": manifest}],
            "executions": [],
            "workflow_outputs": [],
            "role_bindings": [{"role_id": "vector", "output_ref": output_ref}],
        },
    }
    spec = r2_spec("DATA_VALIDITY")
    spec["required_outputs"] = [
        {
            "role_id": "vector",
            "necessary": True,
            "expected": {
                "result_kind": "SPATIAL_VECTOR",
                "operator_id": "qgis-buffer",
                "operator_version": "1.0.0",
                "output_port": "vector",
                "schema_id": "spatial-vector-v1",
            },
        }
    ]
    plan, _ = compile_coverage(
        spec, candidate, [registration()], [], [], [], [], "registration", catalog
    )
    frozen = next(
        item
        for item in plan["checker_runs"]
        if item["capability_id"] == "spatial-vector-validity-v1"
    )
    checker_run = frozen | {"run_id": "spatial-run", "catalog_digest": plan["checker_catalog"]}
    request = {
        "coverage_plan": plan,
        "checker_run": checker_run,
        "artifacts": candidate["payload"]["artifacts"],
        "role_bindings": candidate["payload"]["role_bindings"],
    }
    index = {
        "roles": {
            "vector": {
                "output_ref": output_ref,
                "manifest": manifest,
                "files": {
                    "vector": {
                        "path": "roles/vector/vector.gpkg",
                        **representation,
                    }
                },
            }
        }
    }
    (source / "request.json").write_text(json.dumps(request), encoding="utf-8")
    (source / "input-index.json").write_text(json.dumps(index), encoding="utf-8")
    for path in source.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return source


def main():
    lock = json.loads((ROOT / ".p5-runtime/checker-catalog.json").read_text())
    images = lock["images"]
    os.environ["V7_CHECKER_IMAGE"] = images["CHECKER"]
    os.environ["V7_GDAL_CHECKER_IMAGE"] = images["GDAL_CHECKER"]
    catalog = load_catalog(ROOT / ".p5-runtime/checker-catalog.json")
    assert catalog == build_catalog(images)
    native = json.loads((ROOT / ".p4-runtime/spatial/images.json").read_text())["images"]["native"]
    with tempfile.TemporaryDirectory(prefix="v7-p5r2-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        p3_check(root, images)
        cartographic_check(root, images)
        descriptor = inspect_vector(root, native)
        source = spatial_request(root, catalog, descriptor)
        output = root / "spatial-output"
        run(images["GDAL_CHECKER"], source, output)
        report = json.loads((output / "check.json").read_text())
        if report["status"] != "PASS":
            raise RuntimeError("GDAL spatial capability did not pass")
        index_path = source / "input-index.json"
        changed = json.loads(index_path.read_text())
        changed["roles"]["vector"]["manifest"]["spatial"]["layers"][0]["bounds"]["min_x"] += 1
        index_path.write_text(json.dumps(changed), encoding="utf-8")
        tampered = root / "spatial-tampered-output"
        run(images["GDAL_CHECKER"], source, tampered)
        if json.loads((tampered / "check.json").read_text())["status"] != "FAIL":
            raise RuntimeError("GDAL capability accepted a false spatial extent")
    workspace_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    print(
        f"PASS: locked {workspace_version} generic and GDAL checker containers independently "
        "verified P3 numerical, cartographic and spatial vector results"
    )


if __name__ == "__main__":
    main()
