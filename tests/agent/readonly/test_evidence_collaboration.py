import pytest
from platform_agent_runtime.evidence_collaboration import validate_collaboration
from platform_contracts.evidence_collaboration import EvidenceCollaborationResponse
from platform_core.domain.control import RuleViolation
from pydantic import ValidationError


def citation():
    return {
        "evidence_ref": {
            "project_id": "project",
            "document_id": "document",
            "version_id": "1",
            "unit_id": "unit",
            "content_digest": "sha256:" + "a" * 64,
            "start_char": 0,
            "end_char": 4,
            "exact_text_digest": "sha256:" + "b" * 64,
            "normalization": "utf8-nfc-lf-v1",
        },
        "read_receipt_id": "receipt",
        "lifecycle_revision": 1,
    }


def response():
    return {
        "project_id": "project",
        "findings": [
            {
                "kind": "EVIDENCED_FACT",
                "statement": "The exact evidence describes the observed datum.",
                "confidence": "MEDIUM",
                "citations": [citation()],
            }
        ],
        "proposals": [
            {
                "proposal_id": "proposal",
                "kind": "RESEARCH_DRAFT",
                "summary": "Review this frozen research draft.",
                "frozen_payload_digest": "sha256:" + "c" * 64,
            }
        ],
    }


def test_evidence_collaboration_requires_active_exact_read_and_human_confirmation():
    value = EvidenceCollaborationResponse.model_validate(response())
    assert value.proposals[0].requires_human_confirmation is True
    assert value.proposals[0].execution_allowed is False
    assert (
        validate_collaboration(
            value,
            exact_reads=[
                {
                    "read_receipt_id": "receipt",
                    "evidence_ref": citation()["evidence_ref"],
                    "lifecycle_revision": 1,
                    "availability": "ACTIVE",
                }
            ],
        )
        == value
    )

    with pytest.raises(RuleViolation, match="not verified"):
        validate_collaboration(value, exact_reads=[])


def test_collaboration_rejects_unsupported_facts_and_cross_project_citations():
    unsupported = response()
    unsupported["findings"][0]["citations"] = []
    with pytest.raises(ValidationError, match="requires exact-read"):
        EvidenceCollaborationResponse.model_validate(unsupported)

    cross_project = response()
    cross_project["findings"][0]["citations"][0]["evidence_ref"]["project_id"] = "other"
    with pytest.raises(ValidationError, match="project differs"):
        EvidenceCollaborationResponse.model_validate(cross_project)
