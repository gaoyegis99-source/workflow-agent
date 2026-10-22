"""P5.1 knowledge wire contracts; search discovery is distinct from exact evidence reads."""

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator

from platform_contracts.v1 import AssetRef, Contract, Identifier, PositiveRevision, Sha256Digest

KnowledgeText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class KnowledgeImportCreate(Contract):
    source: AssetRef
    representation_id: Literal["original"] = "original"


class KnowledgeImportReceipt(Contract):
    import_id: Identifier
    status: Literal["QUEUED", "IMPORTING", "READY", "FAILED"]
    status_url: str


class KnowledgeImportView(KnowledgeImportReceipt):
    project_id: Identifier
    actor_id: Identifier
    source: AssetRef
    representation_id: Literal["original"]
    document_id: Identifier | None = None
    version_id: Identifier | None = None
    error: dict[str, Any] | None = None


class KnowledgeSearch(Contract):
    query: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    limit: Annotated[int, Field(ge=1, le=20)] = 20

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value):
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("query must contain visible text without control characters")
        return value


class KnowledgeScopedSearch(KnowledgeSearch):
    """Internal search restricted to exact immutable source identities."""

    sources: Annotated[list[AssetRef], Field(max_length=4096)]


class SearchHit(Contract):
    document_id: Identifier
    version_id: Identifier
    unit_id: Identifier
    title: KnowledgeText
    snippet: Annotated[str, StringConstraints(max_length=600)]
    start_char: Annotated[int, Field(ge=0)]
    end_char: Annotated[int, Field(gt=0)]
    score: float
    availability: Literal["ACTIVE"]
    epistemic_status: Literal["DECLARED", "CORROBORATED", "CONFLICTING"]
    review_state: Literal["UNREVIEWED", "REVIEWED"]


class KnowledgeSearchResult(Contract):
    status: Literal["RESULTS", "NO_RESULTS"]
    query_digest: Sha256Digest
    index_version: Literal["postgres-lexical-v1"]
    candidate_count: Annotated[int, Field(ge=0)]
    items: Annotated[list[SearchHit], Field(max_length=20)]


class EvidenceRef(Contract):
    project_id: Identifier
    document_id: Identifier
    version_id: Identifier
    unit_id: Identifier
    content_digest: Sha256Digest
    start_char: Annotated[int, Field(ge=0)]
    end_char: Annotated[int, Field(gt=0)]
    exact_text_digest: Sha256Digest
    normalization: Literal["utf8-nfc-lf-v1"]


class EvidenceRead(Contract):
    evidence_ref: EvidenceRef
    text: str
    prefix: str
    suffix: str
    lifecycle_revision: PositiveRevision
    availability: Literal["ACTIVE", "WITHDRAWN"]
    epistemic_status: Literal["DECLARED", "CORROBORATED", "CONFLICTING"]
    review_state: Literal["UNREVIEWED", "REVIEWED"]
    read_receipt_id: Identifier


class KnowledgeVersionView(Contract):
    project_id: Identifier
    document_id: Identifier
    version_id: Identifier
    title: KnowledgeText
    source: AssetRef
    source_digest: Sha256Digest
    content_digest: Sha256Digest
    format: Literal["markdown", "text"]
    normalization: Literal["utf8-nfc-lf-v1"]
    chunking: Literal["paragraph-window-v1"]
    availability: Literal["ACTIVE", "WITHDRAWN"]
    epistemic_status: Literal["DECLARED", "CORROBORATED", "CONFLICTING"]
    review_state: Literal["UNREVIEWED", "REVIEWED"]
    lifecycle_revision: PositiveRevision
    unit_count: Annotated[int, Field(ge=1)]
    withdrawal_reason: str | None = None


class KnowledgeDocumentList(Contract):
    items: list[KnowledgeVersionView]
    next_cursor: str | None


class KnowledgeWithdraw(Contract):
    expected_lifecycle_revision: PositiveRevision
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]
