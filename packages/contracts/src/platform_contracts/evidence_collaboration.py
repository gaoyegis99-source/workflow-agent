"""Evidence-bounded collaboration DTOs for the non-autonomous V7 assistant."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from platform_contracts.knowledge import EvidenceRef
from platform_contracts.v1 import Contract, Identifier, PositiveRevision, Sha256Digest

CollaborationText = Annotated[str, StringConstraints(min_length=1, max_length=8_000)]


class ExactCitation(Contract):
    """A citation becomes usable only after the existing exact-read operation."""

    evidence_ref: EvidenceRef
    read_receipt_id: Identifier
    lifecycle_revision: PositiveRevision


class EvidenceFinding(Contract):
    kind: Literal[
        "EVIDENCED_FACT", "HYPOTHESIS", "INSUFFICIENT_EVIDENCE", "ANALYSIS_RECOMMENDATION"
    ]
    statement: CollaborationText
    confidence: Literal["HIGH", "MEDIUM", "LOW", "NOT_DETERMINED"]
    citations: Annotated[list[ExactCitation], Field(max_length=64)] = Field(default_factory=list)
    limitations: Annotated[list[CollaborationText], Field(max_length=32)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def evidence_boundary(self):
        identities = [
            (
                citation.evidence_ref.document_id,
                citation.evidence_ref.version_id,
                citation.evidence_ref.unit_id,
                citation.evidence_ref.exact_text_digest,
            )
            for citation in self.citations
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Evidence citations must be unique")
        if self.kind == "EVIDENCED_FACT" and not self.citations:
            raise ValueError("An evidenced fact requires exact-read citations")
        if self.kind == "INSUFFICIENT_EVIDENCE" and (
            self.citations or self.confidence != "NOT_DETERMINED"
        ):
            raise ValueError("Insufficient evidence cannot assert a cited fact")
        if self.kind == "HYPOTHESIS" and self.confidence == "HIGH":
            raise ValueError("A hypothesis cannot be high confidence")
        return self


class ConfirmationProposal(Contract):
    """A review item, not a command and not an authorisation bypass."""

    proposal_id: Identifier
    kind: Literal["RESEARCH_DRAFT", "CONTROLLED_ACTION", "WITHDRAWAL_REQUEST"]
    summary: CollaborationText
    frozen_payload_digest: Sha256Digest
    requires_human_confirmation: Literal[True] = True
    execution_allowed: Literal[False] = False


class EvidenceCollaborationResponse(Contract):
    schema_version: Literal["evidence-collaboration.v1"] = "evidence-collaboration.v1"
    project_id: Identifier
    findings: Annotated[list[EvidenceFinding], Field(min_length=1, max_length=64)]
    clarification_questions: Annotated[list[CollaborationText], Field(max_length=16)] = Field(
        default_factory=list
    )
    proposals: Annotated[list[ConfirmationProposal], Field(max_length=16)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def project_and_proposal_shape(self):
        for finding in self.findings:
            if any(
                citation.evidence_ref.project_id != self.project_id
                for citation in finding.citations
            ):
                raise ValueError("Evidence citation project differs from collaboration project")
        proposal_ids = [proposal.proposal_id for proposal in self.proposals]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("Collaboration proposal identities must be unique")
        return self
