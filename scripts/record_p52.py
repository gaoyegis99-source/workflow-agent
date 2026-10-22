"""Record P5.2 acceptance evidence after the unified gate has passed."""

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


def main():
    verification = ROOT / ".p5-runtime/verification"
    tree = ET.parse(verification / "tests.xml")
    cases = tree.findall(".//testcase")
    counts = {
        name: len(tree.findall(".//" + tag))
        for name, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))
    }
    required = {
        "test_research_contracts_are_closed_and_revision_shapes_are_explicit",
        "test_groundwater_policy_is_local_deterministic_and_does_not_block_unrelated_work",
        "test_protocol_registration_claim_and_provenance_are_immutable",
        "test_cross_project_observed_source_is_rejected",
        "test_action_policy_is_opt_in_local_and_frozen_to_current_registration",
        "test_0006_to_0007_preserves_existing_platform_state",
        "test_real_public_research_routes_recheck_roles_and_return_current_state",
        "test_lexical_quality_and_ten_thousand_unit_latency",
    }
    names = {case.attrib["name"] for case in cases}
    if any(counts.values()) or len(cases) < 583 or not required <= names:
        raise RuntimeError("P5.2 requires the complete, unskipped regression suite")

    response_loss_tree = ET.parse(verification / "response-loss.xml")
    if len(response_loss_tree.findall(".//testcase")) != 1 or any(
        response_loss_tree.findall(".//" + tag) for tag in ("failure", "error", "skipped")
    ):
        raise RuntimeError("The final real HTTP response-loss regression must pass")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"].startswith("groundwater-") and "editable" in item["source"]
    }
    if len(packages) != 9 or set(packages.values()) != {"0.6.1"}:
        raise RuntimeError("All nine V7 distributions must be frozen at 0.6.1")

    distribution = (verification / "distribution.log").read_text().splitlines()
    required_distribution = {
        "PASS: built 9 wheels and source distributions",
        "PASS: installed migrations, local token CLI, real task/control HTTP and worker",
        "PASS: installed Knowledge migration, schema check, readiness and shutdown",
        "PASS: installed 0001 data upgrade, unchanged ledger, known/unknown HTTP commands",
    }
    if not required_distribution <= set(distribution):
        raise RuntimeError("Repository-external distribution evidence is incomplete")

    sources = [
        "packages/contracts/src/platform_contracts/research.py",
        "packages/platform-core/src/platform_core/domain/research.py",
        "packages/platform-core/src/platform_core/application/research.py",
        "packages/platform-infrastructure/src/platform_infrastructure/migrations/versions/0007_research_policy.py",
        "apps/platform-api/src/platform_api/adapters/http/research.py",
        "tests/p52/test_contracts_and_policy.py",
        "tests/p52/test_research_application.py",
        "tests/p52/test_http.py",
        "scripts/check_p5.sh",
    ]
    evidence = {
        "phase": "P5.2-research-policy",
        "version": "0.6.1",
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "command": "bash scripts/check_p5.sh --phase research",
        "pytest": {"tests": len(cases), **counts},
        "p1_rules": {"tests": 13, "failures": 0},
        "distribution": distribution,
        "packages": packages,
        "schemas": {
            "platform": "0007_research_policy",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0001_knowledge",
        },
        "python": platform.python_version(),
        "source_sha256": {name: sha256(ROOT / name) for name in sources},
        "scope": {
            "analysis": "immutable exploratory revisions and explicit registration",
            "claims": "immutable scoped revisions with exact observed sources",
            "provenance": ["DECLARED_PROVENANCE", "OBSERVED_PROVENANCE"],
            "policy": "groundwater-head-comparison-v1@1.0.0",
            "action_gate": "opt-in exact registration and frozen policy decision",
            "p53": "not implemented",
        },
        "data": "Repository-owned deterministic synthetic research facts; not field evidence",
    }
    output = ROOT / "docs/verification/p52-research-evidence.json"
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"PASS: wrote {output.relative_to(ROOT)} with {len(cases)} tests")


if __name__ == "__main__":
    main()
