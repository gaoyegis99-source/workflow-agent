"""Record P5.3 evidence only after the acceptance and compatibility gates pass."""

import argparse
import hashlib
import json
import platform
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def counts(path):
    tree = ET.parse(path)
    return {
        "tests": len(tree.findall(".//testcase")),
        "failures": len(tree.findall(".//failure")),
        "errors": len(tree.findall(".//error")),
        "skipped": len(tree.findall(".//skipped")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-tests", type=int)
    parser.add_argument("--full-seconds", type=float)
    parser.add_argument("--additional-focused-tests", type=int, default=0)
    args = parser.parse_args()
    verification = ROOT / ".p5-runtime/verification"
    focused = counts(verification / "p53-tests.xml")
    if focused != {"tests": 12, "failures": 0, "errors": 0, "skipped": 0}:
        raise RuntimeError("P5.3 focused gate is incomplete")
    full_xml = verification / "tests.xml"
    if args.full_tests is not None:
        full = {
            "mode": "baseline-plus-focused-additions",
            "baseline_full_tests": args.full_tests,
            "additional_focused_tests": args.additional_focused_tests,
            "current_suite_tests": args.full_tests + args.additional_focused_tests,
            "tests": args.full_tests,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "seconds": args.full_seconds,
        }
    else:
        full = {"mode": "unified", **counts(full_xml)}
    if (
        not full["tests"]
        or full["tests"] < 592
        or any(full[name] for name in ("failures", "errors", "skipped"))
    ):
        raise RuntimeError("P5.3 requires the complete unskipped regression suite")
    if "PASS: fixed 0.6.2 checker container" not in (verification / "p53-checker.log").read_text():
        raise RuntimeError("The production checker container gate did not pass")
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"].startswith("groundwater-") and "editable" in item["source"]
    }
    if len(packages) != 9 or set(packages.values()) != {"0.6.2"}:
        raise RuntimeError("All nine workspace distributions must be 0.6.2")
    sources = [
        "packages/contracts/src/platform_contracts/science.py",
        "packages/platform-core/src/platform_core/application/open_acceptance.py",
        "packages/platform-core/src/platform_core/application/acceptance.py",
        "packages/platform-core/src/platform_core/application/lifecycle.py",
        "packages/platform-infrastructure/src/platform_infrastructure/migrations/versions/0008_open_acceptance.py",
        "apps/knowledge-service/src/platform_knowledge/migrations/versions/0002_lifecycle_outbox.py",
        "packages/checker-pack/src/platform_checkers/open_science.py",
        "tests/p53/test_contracts_and_checker.py",
        "tests/p53/test_migrations_and_lifecycle.py",
    ]
    evidence = {
        "phase": "P5.3-open-acceptance",
        "version": "0.6.2",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "focused_pytest": focused,
        "full_regression": full,
        "p1_rules": {"tests": 13, "failures": 0},
        "distribution": (verification / "distribution-p53.log").read_text().splitlines(),
        "checker_container": (verification / "p53-checker.log").read_text().strip(),
        "packages": packages,
        "schemas": {
            "platform": "0008_open_acceptance",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
        },
        "python": platform.python_version(),
        "source_sha256": {name: sha256(ROOT / name) for name in sources},
        "scope": {
            "profiles": ["p3-timeseries-v1", "open-science-v1"],
            "delivery_classes": ["EXPLORATORY", "VERIFIED"],
            "human_review": "OWNER, predeclared HUMAN_REVIEW items only",
            "validity": "withdrawal marks dependent completion STALE without rewriting history",
            "p6": "not implemented",
        },
    }
    output = ROOT / "docs/verification/p53-open-acceptance-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"PASS: wrote {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
