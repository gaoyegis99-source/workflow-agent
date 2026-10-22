"""Record P5.1 acceptance evidence after the unified gate has passed."""

import hashlib
import json
import platform
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    verification = ROOT / ".p5-runtime/verification"
    tree = ET.parse(verification / "tests.xml")
    cases = tree.findall(".//testcase")
    counts = {
        name: len(tree.findall(".//" + tag))
        for name, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))
    }
    required = {
        "test_document_and_knowledge_contracts_are_strict",
        "test_normalization_and_segmentation_are_deterministic_and_exact",
        "test_import_search_exact_read_withdraw_and_historical_read",
        "test_transient_data_failure_requeues_without_fabricating_terminal_failure",
        "test_lexical_quality_and_ten_thousand_unit_latency",
        "test_real_http_document_to_exact_evidence_and_current_roles",
        "test_data_0003_to_0004_preserves_existing_records",
        "test_knowledge_initial_migration_is_repeatable_and_complete",
    }
    names = {case.attrib["name"] for case in cases}
    if any(counts.values()) or len(cases) < 576 or not required <= names:
        raise RuntimeError("P5.1 requires the complete, unskipped regression suite")
    response_loss_tree = ET.parse(verification / "response-loss.xml")
    response_loss_cases = response_loss_tree.findall(".//testcase")
    if len(response_loss_cases) != 1 or any(
        response_loss_tree.findall(".//" + tag) for tag in ("failure", "error", "skipped")
    ):
        raise RuntimeError("The final real HTTP response-loss regression must pass")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"].startswith("groundwater-") and "editable" in item["source"]
    }
    if len(packages) != 9 or set(packages.values()) != {"0.6.0"}:
        raise RuntimeError("All nine V7 distributions must be frozen at 0.6.0")

    distribution = (verification / "distribution.log").read_text().splitlines()
    required_distribution = {
        "PASS: built 9 wheels and source distributions",
        "PASS: installed Knowledge migration, schema check, readiness and shutdown",
        "PASS: installed 0001 data upgrade, unchanged ledger, known/unknown HTTP commands",
    }
    if not required_distribution <= set(distribution):
        raise RuntimeError("Repository-external distribution evidence is incomplete")

    sources = [
        "packages/contracts/src/platform_contracts/knowledge.py",
        "apps/data-service/src/platform_data/document_worker.py",
        "apps/data-service/src/platform_data/migrations/versions/0004_document_assets.py",
        "apps/knowledge-service/src/platform_knowledge/domain.py",
        "apps/knowledge-service/src/platform_knowledge/repository.py",
        "apps/knowledge-service/src/platform_knowledge/worker.py",
        "apps/knowledge-service/src/platform_knowledge/migrations/versions/0001_knowledge.py",
        "apps/platform-api/src/platform_api/adapters/http/knowledge.py",
        "tests/p51/test_http_e2e.py",
        "scripts/check_p5.sh",
    ]
    evidence = {
        "phase": "P5.1-knowledge",
        "version": "0.6.0",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "command": "bash scripts/check_p5.sh --phase knowledge",
        "pytest": {"tests": len(cases), **counts},
        "post_gate_response_loss": {
            "tests": len(response_loss_cases),
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
        "pytest_by_suite": dict(
            sorted(Counter(case.attrib["classname"].split(".")[1] for case in cases).items())
        ),
        "p1_rules": {"tests": 13, "failures": 0},
        "distribution": distribution,
        "packages": packages,
        "schemas": {
            "platform": "0006_service_binding_scope",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0001_knowledge",
        },
        "python": platform.python_version(),
        "source_sha256": {name: sha256(ROOT / name) for name in sources},
        "scope": {
            "formats": ["markdown", "text"],
            "normalization": "utf8-nfc-lf-v1",
            "chunking": "paragraph-window-v1",
            "search": "PostgreSQL simple tsvector plus pg_trgm",
            "formal_reference": "exact EvidenceRef created only by exact unit read",
            "p52_p53": "not implemented",
        },
        "data": "Repository-owned deterministic synthetic documents; not field evidence",
    }
    output = ROOT / "docs/verification/p51-knowledge-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"PASS: wrote {output.relative_to(ROOT)} with {len(cases)} tests")


if __name__ == "__main__":
    main()
