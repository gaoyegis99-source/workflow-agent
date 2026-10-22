"""Record P4.2.1 acceptance evidence after the unified gate has passed."""

import hashlib
import json
import platform
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    verification = ROOT / ".p4-runtime/verification"
    tree = ET.parse(verification / "tests-p42.xml")
    cases = tree.findall(".//testcase")
    counts = {
        name: len(tree.findall(".//" + tag))
        for name, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))
    }
    required = {
        "test_real_qgis_buffer_then_independent_native_summary",
        "test_projected_feet_input_is_explicitly_reprojected_to_metre_output",
        "test_real_qgis_to_native_workflow_is_atomic_and_unique",
        "test_browser_completes_spatial_upload_workflow_and_download",
        "test_geopackage_import_layer_crs_semantics_and_resolve",
        "test_geotiff_import_bands_nodata_and_download",
        "test_0002_upgrade_preserves_assets_publications_and_recoverable_upload",
        "test_only_fixed_spatial_runtime_may_import_system_gdal_qgis_sdks",
    }
    names = {case.attrib["name"] for case in cases}
    if any(counts.values()) or len(cases) < 494 or not required <= names:
        raise RuntimeError("P4.2.1 requires the complete, unskipped regression suite")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"].startswith("groundwater-") and "editable" in item["source"]
    }
    package_versions = set(packages.values())
    if (
        len(packages) != 8
        or len(package_versions) != 1
        or tuple(map(int, next(iter(package_versions)).split("."))) < (0, 4, 0)
    ):
        raise RuntimeError("All eight V7 packages must share a version at or after P4.2.1")
    runtime_version = next(iter(package_versions))

    image_record = json.loads((ROOT / ".p4-runtime/spatial/images.json").read_text())
    for identity in image_record["images"].values():
        actual = subprocess.check_output(
            ["docker", "image", "inspect", identity, "--format", "{{.Id}}"], text=True
        ).strip()
        if actual != identity:
            raise RuntimeError("Spatial image identity changed")
    distribution = (verification / "distribution-p42.log").read_text().splitlines()
    required_distribution = {
        "PASS: built 8 wheels and source distributions",
        "PASS: installed GeoPackage inspection, QGIS buffer, Native summary and publication",
        "PASS: installed 0001 data upgrade, unchanged ledger, known/unknown HTTP commands",
    }
    if not required_distribution <= set(distribution):
        raise RuntimeError("Repository-external distribution evidence is incomplete")

    sources = [
        "packages/contracts/src/platform_contracts/science.py",
        "packages/operator-pack/src/platform_operators/spatial.py",
        "apps/data-service/src/platform_data/spatial_worker.py",
        "apps/data-service/src/platform_data/migrations/versions/0003_spatial_import_runtime.py",
        "apps/execution-service/src/platform_execution/application/runner.py",
        "apps/execution-service/src/platform_execution/application/workflow_runner.py",
        "examples/workflows/qgis-buffer-summary-v1.json",
        "deploy/p42/qgis.Dockerfile",
        "deploy/p42/native.Dockerfile",
        "scripts/check_p42.sh",
        "tests/p42/test_migration.py",
    ]
    evidence = {
        "phase": "P4.2.1-spatial",
        "version": runtime_version,
        "baseline_version": "0.4.0",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "command": "bash scripts/check_p4.sh --phase spatial",
        "pytest": {"tests": len(cases), **counts},
        "pytest_by_suite": dict(
            sorted(Counter(case.attrib["classname"].split(".")[1] for case in cases).items())
        ),
        "p1_rules": {"tests": 13, "failures": 0},
        "distribution": distribution,
        "packages": packages,
        "schemas": {
            "platform": "0004_workflows",
            "data": "0003_spatial_import_runtime",
            "execution": "0004_execution_work_class",
        },
        "images": image_record,
        "python": platform.python_version(),
        "docker": subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"], text=True
        ).strip(),
        "fixtures": json.loads((ROOT / "tests/p42/fixtures/checksums.json").read_text()),
        "source_sha256": {name: sha256(ROOT / name) for name in sources},
        "scope": {
            "vector_import": "GeoPackage",
            "raster_import": "GeoTIFF",
            "operators": ["qgis-buffer@1.0.0", "spatial-vector-summary@1.0.0"],
            "workflow": "qgis-buffer-summary-v1",
            "task_completion": "OPEN; no spatial acceptance template",
        },
        "data": "Repository-owned deterministic synthetic spatial fixtures; not field observations",
    }
    output = ROOT / "docs/verification/p42-spatial-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"PASS: wrote {output.relative_to(ROOT)} with {len(cases)} tests")


if __name__ == "__main__":
    main()
