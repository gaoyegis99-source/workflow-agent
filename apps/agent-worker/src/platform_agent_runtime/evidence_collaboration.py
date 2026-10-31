"""Adapter-side validation for evidence-bounded, non-autonomous assistance."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from platform_contracts.evidence_collaboration import EvidenceCollaborationResponse
from platform_core.domain.control import require


def validate_collaboration(
    response: EvidenceCollaborationResponse | Mapping[str, object],
    *,
    exact_reads: Iterable[Mapping[str, object]],
) -> EvidenceCollaborationResponse:
    """Reject citations that were not exact-read in this governed workflow.

    Semantic/lexical hits are intentionally absent from this input.  A model
    can suggest them as discovery output, but it cannot turn one into a fact
    without a matching active exact read receipt.
    """

    value = (
        response
        if isinstance(response, EvidenceCollaborationResponse)
        else EvidenceCollaborationResponse.model_validate(response)
    )
    observed = {
        (
            item.get("read_receipt_id"),
            item.get("evidence_ref", {}).get("project_id"),
            item.get("evidence_ref", {}).get("document_id"),
            item.get("evidence_ref", {}).get("version_id"),
            item.get("evidence_ref", {}).get("unit_id"),
            item.get("evidence_ref", {}).get("exact_text_digest"),
            item.get("lifecycle_revision"),
        )
        for item in exact_reads
        if item.get("availability") == "ACTIVE"
    }
    for finding in value.findings:
        for citation in finding.citations:
            ref = citation.evidence_ref
            identity = (
                citation.read_receipt_id,
                ref.project_id,
                ref.document_id,
                ref.version_id,
                ref.unit_id,
                ref.exact_text_digest,
                citation.lifecycle_revision,
            )
            require(
                identity in observed,
                "INVALID_BINDING",
                "Collaboration citation was not verified by an active exact read",
                409,
            )
    return value
