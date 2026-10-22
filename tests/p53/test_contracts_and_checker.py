import pytest
from platform_checkers.open_science import check
from platform_contracts.control import TaskSpec
from platform_core.application.open_acceptance import (
    compile_coverage,
    outcome,
    validate_checker_report,
)
from platform_core.domain.control import RuleViolation
from pydantic import ValidationError


def spec(delivery="EXPLORATORY", checks=None):
    return {
        "profile": "open-science-v1",
        "delivery_class": delivery,
        "requirements": [
            {
                "requirement_id": "scientific-clause",
                "description": "The result has the declared support.",
                "necessary": True,
                "checks": checks or ["DATA_VALIDITY", "METHOD_PRECONDITION"],
            }
        ],
        "required_outputs": [],
        "allow_manual_review": "HUMAN_REVIEW" in (checks or []),
        "max_check_executions": 2,
    }


def candidate():
    return {
        "digest": "candidate-digest",
        "spec_revision": 1,
        "payload": {
            "executions": ["execution-1"],
            "workflow_outputs": [],
            "artifacts": [
                {
                    "ref": {
                        "project_id": "project",
                        "asset_id": "asset",
                        "version_id": "1",
                        "version_manifest_digest": "sha256:" + "a" * 64,
                    },
                    "manifest": {
                        "representations": [
                            {"representation_id": "result", "digest": "sha256:" + "b" * 64}
                        ]
                    },
                }
            ],
        },
    }


def test_open_contract_is_discriminated_and_rejects_empty_or_unsafe_review():
    value = TaskSpec.model_validate({"goal": "Open", "requirements": [], "acceptance_spec": spec()})
    assert value.acceptance_spec.profile == "open-science-v1"
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {"goal": "Empty", "requirements": [], "acceptance_spec": spec() | {"requirements": []}}
        )
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "goal": "Review",
                "requirements": [],
                "acceptance_spec": spec(checks=["HUMAN_REVIEW"]) | {"allow_manual_review": False},
            }
        )


def test_checker_is_deterministic_and_exploratory_discloses_limits():
    plan, evidence = compile_coverage(spec(), candidate(), [], [], [], [])
    assert evidence == []
    assert plan == compile_coverage(spec(), candidate(), [], [], [], [])[0]
    report = check({"coverage_plan": plan})
    assert report["status"] == "INDETERMINATE"
    rows = []
    by_id = {item["item_id"]: item for item in report["items"]}
    data_item = next(value for value in plan["items"] if value["check_type"] == "DATA_VALIDITY")
    assert by_id[data_item["item_id"]]["status"] == "INDETERMINATE"
    for item in plan["items"]:
        rows.append(item | {"status": by_id[item["item_id"]]["status"]})
    assert outcome("EXPLORATORY", rows)[0] == "PASS"


def test_verified_delivery_cannot_turn_indeterminate_reproduction_into_pass():
    plan, _ = compile_coverage(
        spec("VERIFIED", ["RESULT_REPRODUCTION"]),
        candidate(),
        [
            {
                "registration_id": "registration",
                "analysis_id": "analysis",
                "revision_id": "revision",
                "revision": 1,
                "task_spec_revision": 1,
                "observation_watermark": [],
                "watermark_digest": "sha256:" + "c" * 64,
                "policy_decision_id": "policy",
            }
        ],
        [],
        [],
        [],
    )
    report = check({"coverage_plan": plan})
    assert report["status"] == "INDETERMINATE"
    by_id = {item["item_id"]: item for item in report["items"]}
    rows = [item | {"status": by_id[item["item_id"]]["status"]} for item in plan["items"]]
    assert outcome("VERIFIED", rows)[0] == "INDETERMINATE"


def test_plan_digest_tampering_is_an_error():
    plan, _ = compile_coverage(spec(), candidate(), [], [], [], [])
    plan["facts"]["available_output_ids"].append("forged")
    report = check({"coverage_plan": plan})
    assert report["status"] == "ERROR"
    assert report["category"] == "PLAN_DIGEST_MISMATCH"


def test_verified_plan_rejects_ambiguous_registrations():
    registration = {
        "registration_id": "registration-1",
        "analysis_id": "analysis",
        "revision_id": "revision",
        "revision": 1,
        "task_spec_revision": 1,
        "observation_watermark": [],
        "watermark_digest": "sha256:" + "c" * 64,
        "policy_decision_id": "policy",
    }
    with pytest.raises(RuleViolation) as error:
        compile_coverage(
            spec("VERIFIED"),
            candidate(),
            [registration, registration | {"registration_id": "registration-2"}],
            [],
            [],
            [],
        )
    assert error.value.code == "PRECONDITION_UNRESOLVED"


def test_superseded_registration_does_not_make_current_protocol_ambiguous():
    old = {
        "registration_id": "registration-1",
        "analysis_id": "analysis",
        "revision_id": "revision-1",
        "revision": 1,
        "task_spec_revision": 1,
        "observation_watermark": [],
        "watermark_digest": "sha256:" + "c" * 64,
        "policy_decision_id": "policy-1",
    }
    current = old | {
        "registration_id": "registration-2",
        "revision_id": "revision-2",
        "revision": 2,
        "policy_decision_id": "policy-2",
    }
    analyses = [
        {"revision_id": "revision-1", "supersedes_revision_id": None},
        {"revision_id": "revision-2", "supersedes_revision_id": "revision-1"},
    ]
    plan, _ = compile_coverage(
        spec("VERIFIED", ["RESULT_REPRODUCTION"]),
        candidate(),
        [old, current],
        [],
        [],
        [],
        analyses,
    )
    assert plan["facts"]["registration"]["registration_id"] == "registration-2"


def test_claim_support_requires_observed_support_for_each_claim_revision():
    registration = {
        "registration_id": "registration",
        "analysis_id": "analysis",
        "revision_id": "analysis-revision",
        "revision": 1,
        "task_spec_revision": 1,
        "observation_watermark": [],
        "watermark_digest": "sha256:" + "c" * 64,
        "policy_decision_id": "policy",
    }
    claim = {
        "claim_id": "claim",
        "claim_revision_id": "claim-revision",
        "revision": 1,
        "payload": {
            "analysis_registration_id": "registration",
            "support_status": "SUPPORTED",
            "proposition_type": "DESCRIPTIVE",
        },
    }
    unrelated = {
        "provenance_kind": "OBSERVED_PROVENANCE",
        "relation": "USED",
        "subject": {"kind": "ANALYSIS_REVISION", "object_id": "analysis-revision"},
        "object": {"kind": "EXECUTION", "execution_id": "execution-1"},
        "role": "observation",
        "relation_digest": "sha256:" + "d" * 64,
    }
    plan, _ = compile_coverage(
        spec("VERIFIED", ["CLAIM_SUPPORT"]),
        candidate(),
        [registration],
        [claim],
        [],
        [unrelated],
    )
    report = check({"coverage_plan": plan})
    claim_item = next(value for value in plan["items"] if value["check_type"] == "CLAIM_SUPPORT")
    result = next(value for value in report["items"] if value["item_id"] == claim_item["item_id"])
    assert result["status"] == "FAIL"


def test_declared_evidence_used_by_policy_is_tracked_for_invalidation():
    ref = {
        "project_id": "project",
        "document_id": "document",
        "version_id": "1",
        "unit_id": "unit",
        "content_digest": "sha256:" + "a" * 64,
        "start_char": 0,
        "end_char": 4,
        "exact_text_digest": "sha256:" + "b" * 64,
        "normalization": "utf8-nfc-lf-v1",
    }
    relation = {
        "provenance_kind": "DECLARED_PROVENANCE",
        "relation": "SUPPORTED_BY",
        "subject": {"kind": "ANALYSIS_REVISION", "object_id": "analysis-revision"},
        "object": {"kind": "EVIDENCE", "ref": ref},
        "role": "datum_conversion",
        "relation_digest": "sha256:" + "d" * 64,
    }
    _, evidence = compile_coverage(spec(), candidate(), [], [], [], [relation])
    assert evidence == [ref]


def test_checker_report_must_exactly_match_every_automatic_plan_item():
    plan, _ = compile_coverage(spec(), candidate(), [], [], [], [])
    report = check({"coverage_plan": plan})
    validate_checker_report(plan, report)
    forged = report | {"items": [report["items"][0] | {"check_type": "CLAIM_SUPPORT"}]}
    with pytest.raises(RuleViolation) as error:
        validate_checker_report(plan, forged)
    assert error.value.code == "INVALID_BINDING"


def test_checker_error_remains_retryable_instead_of_becoming_scientific_fail():
    items = [
        {
            "item_id": "runtime",
            "necessary": True,
            "check_type": "RUNTIME_INTEGRITY",
            "status": "PASS",
        },
        {
            "item_id": "data",
            "necessary": True,
            "check_type": "DATA_VALIDITY",
            "status": "ERROR",
        },
    ]
    assert outcome("VERIFIED", items) == ("ERROR", ["data"])
