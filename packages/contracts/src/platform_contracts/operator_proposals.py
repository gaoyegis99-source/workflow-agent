"""Strict contracts for inert operator registration materials.

These DTOs describe evidence for the existing controlled release process.  They
do not describe executable uploads and deliberately contain no command, URL
fetch, image selection, or arbitrary resource fields.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from platform_contracts.v1 import (
    Contract,
    Identifier,
    PositiveRevision,
    Sha256Digest,
    VersionId,
)

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
LongText = Annotated[str, StringConstraints(max_length=4000)]
ProvenanceText = Annotated[str, StringConstraints(min_length=1, max_length=2048)]


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _bounded_json(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 128 * 1024:
        raise ValueError("Operator material contract exceeds 128 KiB")
    count = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal count
        if depth > 16:
            raise ValueError("Operator material contract nesting is too deep")
        count += 1
        if count > 4096:
            raise ValueError("Operator material contract has too many values")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise ValueError("Operator material contract has an invalid key")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str) and len(item) > 4096:
            raise ValueError("Operator material contract contains an oversized string")
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("Operator material contract contains an unsupported value")

    visit(value, 0)
    return value


class ProposedOperator(Contract):
    operator_id: Identifier
    version_id: VersionId
    ownership: Literal["CURATED_PACKAGE", "DOMAIN_PACKAGE"]
    trust_level: Literal["VERIFIED"]
    inputs: Annotated[dict[Identifier, Any], Field(min_length=1, max_length=128)]
    parameters: dict[str, Any]
    outputs: Annotated[dict[Identifier, Any], Field(min_length=1, max_length=128)]
    effects: list[Literal["CONSUMES_EXECUTION_BUDGET", "CREATES_ASSETS"]]
    supports_workflow: bool
    legacy: dict[str, Any] | None = None
    operator_digest: Sha256Digest

    @field_validator("inputs", "parameters", "outputs", "legacy")
    @classmethod
    def bounded_contract(cls, value):
        return _bounded_json(value)

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        if self.effects != ["CONSUMES_EXECUTION_BUDGET", "CREATES_ASSETS"]:
            raise ValueError("Operator effects must use the fixed compute effects")
        semantic = self.model_dump(mode="json", exclude={"operator_digest"})
        if self.operator_digest != _digest(semantic):
            raise ValueError("operator_digest does not match the proposed semantic contract")
        return self


class ProposedImplementation(Contract):
    implementation_id: Identifier
    binding_kind: Literal["LOCAL"]
    runtime: Literal["NATIVE", "QGIS"]
    image_digest: Sha256Digest
    protocol: Literal[
        "P3_TABLE",
        "SPATIAL_VECTOR",
        "SPATIAL_SUMMARY",
        "CARTOGRAPHIC_PNG",
        "TABULAR_V1",
    ]
    resource_profile: Literal[
        "native-small-v1",
        "spatial-native-v1",
        "qgis-medium-v1",
        "table-small-v1",
    ]
    implementation_digest: Sha256Digest

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        identity = self.model_dump(mode="json", exclude={"implementation_digest"})
        if self.implementation_digest != _digest(identity):
            raise ValueError("implementation_digest does not match the proposed implementation")
        return self


class OperatorSourceMaterial(Contract):
    package_name: VersionId
    package_version: VersionId
    package_source: ProvenanceText
    source_digest: Sha256Digest
    tests_digest: Sha256Digest
    test_summary: LongText = ""
    checker_coverage: Annotated[list[Identifier], Field(max_length=64)] = Field(
        default_factory=list
    )

    @field_validator("checker_coverage")
    @classmethod
    def unique_checkers(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("checker_coverage must be unique")
        return value


class OperatorProposalMaterials(Contract):
    title: ShortText
    purpose: LongText
    limitations: LongText
    operator: ProposedOperator
    implementation: ProposedImplementation
    source: OperatorSourceMaterial

    @model_validator(mode="after")
    def bounded_materials(self) -> Self:
        _bounded_json(self.model_dump(mode="json"))
        return self


class OperatorProposalCreate(Contract):
    stable_key: Identifier
    materials: OperatorProposalMaterials


class OperatorProposalRevisionCreate(Contract):
    expected_revision: PositiveRevision
    materials: OperatorProposalMaterials


class OperatorProposalSubmit(Contract):
    revision: PositiveRevision
    materials_digest: Sha256Digest


class OperatorProposalRevisionView(Contract):
    revision: PositiveRevision
    materials: OperatorProposalMaterials
    materials_digest: Sha256Digest
    created_by: Identifier
    created_at: str


class OperatorReviewEventView(Contract):
    event_id: Identifier
    revision: PositiveRevision
    materials_digest: Sha256Digest
    event_type: Literal["SUBMITTED", "CHANGES_REQUESTED", "APPROVED", "REJECTED"]
    reason: str
    actor_kind: Literal["PROJECT", "MAINTAINER"]
    actor_id: Identifier
    evidence_digest: Sha256Digest | None = None
    created_at: str


class OperatorDeploymentReceiptView(Contract):
    receipt_id: Identifier
    revision: PositiveRevision
    materials_digest: Sha256Digest
    release_digest: Sha256Digest
    operator_catalog_digest: Sha256Digest
    operator_id: Identifier
    version_id: VersionId
    operator_digest: Sha256Digest
    implementation_id: Identifier
    implementation_digest: Sha256Digest
    image_digest: Sha256Digest
    source_digest: Sha256Digest
    tests_digest: Sha256Digest
    maintainer_id: Identifier
    evidence_digest: Sha256Digest
    created_at: str


class OperatorDeploymentMatch(Contract):
    status: Literal[
        "NO_RECEIPT",
        "NOT_APPROVED",
        "RELEASE_MISMATCH",
        "CATALOG_MISMATCH",
        "OPERATOR_NOT_FOUND",
        "IMPLEMENTATION_NOT_FOUND",
        "MATERIAL_MISMATCH",
        "MATCHED_ACTIVE",
        "MATCHED_DISABLED",
        "MATCHED_REVOKED",
    ]
    current_executable: bool
    reasons: list[Identifier]
    current_release_digest: Sha256Digest | None = None
    current_catalog_digest: Sha256Digest


class OperatorProposalView(Contract):
    proposal_id: Identifier
    project_id: Identifier
    stable_key: Identifier
    head_revision: PositiveRevision
    status: Literal["DRAFT", "SUBMITTED", "CHANGES_REQUESTED", "APPROVED", "REJECTED"]
    head: OperatorProposalRevisionView
    revisions: list[OperatorProposalRevisionView] = Field(default_factory=list)
    review_events: list[OperatorReviewEventView] = Field(default_factory=list)
    deployment_receipts: list[OperatorDeploymentReceiptView] = Field(default_factory=list)
    deployment_match: OperatorDeploymentMatch
    created_by: Identifier
    created_at: str


class OperatorProposalPage(Contract):
    items: list[OperatorProposalView]
    next_cursor: str | None = None
