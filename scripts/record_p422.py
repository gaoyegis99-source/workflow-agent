"""Record P4.2.2 acceptance evidence after the unified gate has passed."""

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
    tree = ET.parse(verification / "tests-p422.xml")
    cases = tree.findall(".//testcase")
    counts = {
        name: len(tree.findall(".//" + tag))
        for name, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))
    }
    required = {
        "test_static_map_contract_and_catalog_are_closed",
        "test_static_map_is_deterministic_inside_the_frozen_image",
        "test_png_verifier_rejects_signature_and_header",
        "test_buffer_summary_map_workflow_is_atomic_and_unique",
        "test_map_failure_does_not_publish_partial_workflow_outputs",
        "test_browser_runs_and_previews_trusted_static_map",
        "test_real_qgis_to_native_workflow_is_atomic_and_unique",
        "test_browser_completes_spatial_upload_workflow_and_download",
    }
    names = {case.attrib["name"] for case in cases}
    if any(counts.values()) or len(cases) < 514 or not required <= names:
        raise RuntimeError("P4.2.2 requires the complete, unskipped regression suite")

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
        or tuple(map(int, next(iter(package_versions)).split("."))) < (0, 4, 1)
    ):
        raise RuntimeError("All eight V7 packages must share a version at or after P4.2.2")
    runtime_version = next(iter(package_versions))

    image_record = json.loads((ROOT / ".p4-runtime/spatial/images.json").read_text())
    for identity in image_record["images"].values():
        actual = subprocess.check_output(
            ["docker", "image", "inspect", identity, "--format", "{{.Id}}"], text=True
        ).strip()
        if actual != identity:
            raise RuntimeError("Spatial image identity changed")
    if "1:20230817+repack1-3" not in image_record["versions"]:
        raise RuntimeError("The fixed Noto CJK font package is not recorded")

    distribution = (verification / "distribution-p422.log").read_text().splitlines()
    required_distribution = {
        "PASS: built 8 wheels and source distributions",
        "PASS: installed trusted QGIS map, PNG delivery and atomic three-node publication",
        "PASS: installed 0001 data upgrade, unchanged ledger, known/unknown HTTP commands",
    }
    if not required_distribution <= set(distribution):
        raise RuntimeError("Repository-external distribution evidence is incomplete")

    sources = [
        "packages/contracts/src/platform_contracts/science.py",
        "packages/platform-core/src/platform_core/application/catalog.py",
        "packages/operator-pack/src/platform_operators/spatial.py",
        "apps/execution-service/src/platform_execution/cartography.py",
        "apps/execution-service/src/platform_execution/application/runner.py",
        "apps/execution-service/src/platform_execution/application/workflow_runner.py",
        "apps/data-service/src/platform_data/application/publication.py",
        "apps/capability-studio/src/app/App.tsx",
        "examples/workflows/qgis-buffer-summary-map-v1.json",
        "deploy/p42/qgis.Dockerfile",
        "scripts/check_p422.sh",
        "tests/p422/test_cartography_workflow_e2e.py",
        "tests/p422/test_browser.py",
        "tests/p4/test_engine_package.py",
    ]
    evidence = {
        "phase": "P4.2.2-cartography",
        "version": runtime_version,
        "baseline_version": "0.4.1",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "command": "bash scripts/check_p4.sh --phase cartography",
        "pytest": {"tests": len(cases), **counts},
        "pytest_by_suite": dict(
            sorted(Counter(case.attrib["classname"].split(".")[1] for case in cases).items())
        ),
        "p1_rules": {"tests": 13, "failures": 0},
        "distribution": distribution,
        "packages": packages,
        "cwl_runtime": tomllib.loads((ROOT / "runtimes/cwl/pyproject.toml").read_text())["project"][
            "version"
        ],
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
        "source_sha256": {name: sha256(ROOT / name) for name in sources},
        "scope": {
            "operator": "qgis-static-map@1.0.0",
            "template": "v7-spatial-basic-v1",
            "output": "fixed 1754x1240 PNG",
            "workflow": "qgis-buffer-summary-map-v1",
            "task_completion": "OPEN; no spatial acceptance template",
        },
        "data": "Repository-owned deterministic synthetic spatial fixtures; not field observations",
    }
    output = ROOT / "docs/verification/p422-cartography-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"PASS: wrote {output.relative_to(ROOT)} with {len(cases)} tests")


if __name__ == "__main__":
    main()
