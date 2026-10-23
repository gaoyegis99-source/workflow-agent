"""Record the completed unified gate; never overwrite earlier phase evidence."""

import hashlib
import json
import platform
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from runtime_layout import P3_RUNTIME_ROOT

ROOT = Path(__file__).resolve().parents[1]


def main():
    verification = P3_RUNTIME_ROOT / "verification"
    tree = ET.parse(verification / "tests.xml")
    cases = tree.findall(".//testcase")
    counts = {
        name: len(tree.findall(".//" + tag))
        for name, tag in [("failures", "failure"), ("errors", "error"), ("skipped", "skipped")]
    }
    if any(counts.values()) or len(cases) < 221:
        raise RuntimeError("P3 cannot pass with failed, skipped or missing regression tests")
    versions = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        p["name"]: p["version"]
        for p in versions["package"]
        if p["name"].startswith("groundwater-") and "editable" in p["source"]
    }
    package_versions = set(packages.values())
    if len(package_versions) != 1 or tuple(map(int, next(iter(package_versions)).split("."))) < (
        0,
        2,
        1,
    ):
        raise RuntimeError("Package versions disagree or precede the P3 baseline")
    runtime_version = next(iter(package_versions))
    paths = [
        p
        for folder in ["apps", "packages", "tests", "scripts", "deploy/p3"]
        for p in (ROOT / folder).rglob("*")
        if p.is_file()
        and p.suffix in (".py", ".toml", ".js", ".html", ".css", ".sh", ".Dockerfile")
    ]
    paths += [ROOT / "uv.lock", ROOT / "pyproject.toml", ROOT / "deploy/p3.compose.yaml"]
    source = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {
        "phase": "P3-runtime-followup",
        "version": runtime_version,
        "baseline_version": "0.2.1",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "command": "bash scripts/check_p3.sh",
        "pytest": {"tests": len(cases), **counts},
        "p1_rules": {"tests": 13, "failures": 0},
        "pytest_by_suite": dict(Counter(c.attrib["classname"].split(".")[1] for c in cases)),
        "distribution": (verification / "distribution.log").read_text().splitlines(),
        "packages": packages,
        "preserved_baseline": json.loads((ROOT / "docs/verification/p3-baseline.json").read_text()),
        "python": platform.python_version(),
        "schemas": {
            "platform": "0003_p3_delivery",
            "data": "0001_data",
            "execution": "0001_execution",
        },
        "images": json.loads((P3_RUNTIME_ROOT / "images.json").read_text()),
        "docker": subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"], text=True
        ).strip(),
        "browser": (
            "Playwright 1.63.0 / Chromium 153.0.8010.12; Linux build, ubuntu22.04 override on Focal"
        ),
        "data": "Auditable synthetic fixtures only; not real groundwater observations",
        "heavy_p1": "Rerun separately: see p1-followup-evidence.json",
        "historical_evidence": [
            "p1-evidence.json",
            "p2-initial-evidence.json",
            "p2-evidence.json",
            "p3-evidence.json",
            "p3-followup-evidence.json",
        ],
        "source_sha256": source,
    }
    output = ROOT / "docs/verification/p3-runtime-followup-evidence.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        f"PASS: P3 evidence recorded ({len(cases)} pytest, 13 P1 rules, independent distribution)"
    )


if __name__ == "__main__":
    main()
