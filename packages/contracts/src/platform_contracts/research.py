"""P5.2 immutable research, claim, provenance and policy contracts."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from platform_contracts.knowledge import EvidenceRef
from platform_contracts.v1 import (
    AssetRef,
    Contract,
    Identifier,
    PositiveRevision,
    Sha256Digest,
    VersionId,
)

ResearchText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
ShortResearchText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class AssetSource(Contract):
    kind: Literal["ASSET"]
    ref: AssetRef


class EvidenceSource(Contract):
    kind: Literal["EVIDENCE"]
    ref: EvidenceRef


class ExecutionSource(Contract):
    kind: Literal["EXECUTION"]
    execution_id: Identifier


class MethodRef(Contract):
    method_id: Identifier
    version_id: VersionId
    implementation_digest: Sha256Digest


class MethodSource(Contract):
    kind: Literal["METHOD"]
    ref: MethodRef


SourceRef = Annotated[
    AssetSource | EvidenceSource | ExecutionSource | MethodSource,
    Field(discriminator="kind"),
]


class TemporalScope(Contract):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def aware_instant(cls, value):
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Temporal boundaries require UTC offsets")
        return instant.isoformat()

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if datetime.fromisoformat(self.start) >= datetime.fromisoformat(self.end):
            raise ValueError("Temporal scope must be nonempty")
        return self


class AnalysisInputRole(Contract):
    role: Identifier
    source: SourceRef
    description: ShortResearchText


class GroundwaterConversion(Contract):
    method: ShortResearchText
    source: AssetSource | EvidenceSource


class GroundwaterSeriesFact(Contract):
    input_role: Identifier
    monitoring_point_id: Identifier | None = None
    quantity: Literal["water_level_elevation", "depth_to_water"] | None = None
    unit: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    vertical_datum: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    elevation_conversion: GroundwaterConversion | None = None
    datum_conversion: GroundwaterConversion | None = None
    temporal_scope: TemporalScope | None = None
    aggregation: Identifier | None = None


class GroundwaterHeadComparison(Contract):
    kind: Literal["GROUNDWATER_HEAD_COMPARISON"]
    series: Annotated[list[GroundwaterSeriesFact], Field(min_length=2, max_length=128)]

    @field_validator("series")
    @classmethod
    def distinct_roles(cls, values):
        if len({value.input_role for value in values}) != len(values):
            raise ValueError("Comparison input roles must be unique")
        return values


class AnalysisRevisionCreate(Contract):
    expected_spec_revision: PositiveRevision
    supersedes_revision_id: Identifier | None = None
    revision_reason: ShortResearchText | None = None
    question: ResearchText
    research_objects: Annotated[list[ShortResearchText], Field(min_length=1, max_length=64)]
    spatial_scope: ShortResearchText | None = None
    temporal_scope: TemporalScope | None = None
    input_roles: Annotated[list[AnalysisInputRole], Field(max_length=128)] = Field(
        default_factory=list
    )
    methods: Annotated[list[MethodRef], Field(max_length=32)] = Field(default_factory=list)
    primary_metrics: Annotated[list[ShortResearchText], Field(max_length=32)] = Field(
        default_factory=list
    )
    comparison_rules: Annotated[list[ShortResearchText], Field(max_length=32)] = Field(
        default_factory=list
    )
    data_splits: Annotated[list[ShortResearchText], Field(max_length=32)] = Field(
        default_factory=list
    )
    stopping_rules: Annotated[list[ShortResearchText], Field(max_length=32)] = Field(
        default_factory=list
    )
    allowed_deviations: Annotated[list[ShortResearchText], Field(max_length=32)] = Field(
        default_factory=list
    )
    checker_versions: Annotated[list[MethodRef], Field(max_length=16)] = Field(default_factory=list)
    observed_sources: Annotated[list[SourceRef], Field(max_length=128)] = Field(
        default_factory=list
    )
    groundwater_comparison: GroundwaterHeadComparison | None = None

    @model_validator(mode="after")
    def revision_and_roles(self) -> Self:
        if (self.supersedes_revision_id is None) != (self.revision_reason is None):
            raise ValueError("A successor requires both supersedes_revision_id and reason")
        roles = [value.role for value in self.input_roles]
        if len(set(roles)) != len(roles):
            raise ValueError("Input roles must be unique")
        if self.groundwater_comparison is not None:
            unknown = {item.input_role for item in self.groundwater_comparison.series} - set(roles)
            if unknown:
                raise ValueError("Groundwater comparison refers to an unknown input role")
        methods = [value.model_dump_json() for value in self.methods]
        if len(set(methods)) != len(methods):
            raise ValueError("Methods must be unique")
        observations = [value.model_dump_json() for value in self.observed_sources]
        if len(set(observations)) != len(observations):
            raise ValueError("Observed sources must be unique")
        return self


class RegisterAnalysis(Contract):
    expected_spec_revision: PositiveRevision


class ResearchExecutionContext(Contract):
    """Optional linkage; policy checks apply only when an action declares that dependency."""

    analysis_registration_id: Identifier
    policy_requirement: Literal["groundwater-head-comparison-v1"] | None = None


class PolicyReason(Contract):
    code: Identifier
    message: ShortResearchText
    input_role: Identifier | None = None


class PolicyDecisionView(Contract):
    decision_id: Identifier
    policy_id: Literal["groundwater-head-comparison-v1"]
    policy_version: Literal["1.0.0"]
    policy_digest: Sha256Digest
    input_digest: Sha256Digest
    facts_used: list[dict[str, Any]]
    result: Literal["APPLICABLE", "MISSING", "CONFLICT", "NOT_APPLICABLE"]
    reasons: list[PolicyReason]
    evaluated_at: str


class ObservedSourceFact(Contract):
    source: SourceRef
    observed_at: str
    read_receipt_id: Identifier | None = None
    action_id: Identifier | None = None
    state: Identifier | None = None
    lifecycle_revision: PositiveRevision | None = None
    availability: Literal["ACTIVE", "WITHDRAWN"] | None = None
    epistemic_status: Literal["DECLARED", "CORROBORATED", "CONFLICTING"] | None = None
    review_state: Literal["UNREVIEWED", "REVIEWED"] | None = None


class AnalysisRevisionView(AnalysisRevisionCreate):
    analysis_id: Identifier
    revision_id: Identifier
    revision: PositiveRevision
    task_id: Identifier
    task_spec_revision: PositiveRevision
    status: Literal["EXPLORATORY", "REGISTERED", "SUPERSEDED"]
    registration_id: Identifier | None = None
    content_digest: Sha256Digest
    policy_decision: PolicyDecisionView
    created_by: Identifier
    created_at: str


class AnalysisRegistrationView(Contract):
    registration_id: Identifier
    analysis_id: Identifier
    revision_id: Identifier
    revision: PositiveRevision
    task_id: Identifier
    task_spec_revision: PositiveRevision
    observation_watermark: list[ObservedSourceFact]
    watermark_digest: Sha256Digest
    policy_decision: PolicyDecisionView
    registered_by: Identifier
    registered_at: str


class ClaimScope(Contract):
    objects: Annotated[list[ShortResearchText], Field(min_length=1, max_length=64)]
    spatial_scope: ShortResearchText | None = None
    temporal_scope: TemporalScope | None = None


class ClaimQuantity(Contract):
    value: float
    unit: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class P3ScalarAssertion(Contract):
    kind: Literal["P3_SCALAR"] = "P3_SCALAR"
    role_id: Identifier
    group: Identifier
    metric: Literal[
        "expected_periods",
        "observed_periods",
        "missing_periods",
        "duplicate_periods",
        "null_count",
        "completeness",
        "sample_count",
        "slope_per_day",
        "slope_per_year",
        "intercept",
        "r_squared",
    ]


ResultAssertion = Annotated[P3ScalarAssertion, Field(discriminator="kind")]


class ClaimSourceUse(Contract):
    relation: Literal[
        "USED",
        "GENERATED",
        "DERIVED_FROM",
        "CITED",
        "SUPPORTED_BY",
        "CHECKED_BY",
        "INVALIDATED_BY",
    ]
    role: Identifier
    source: SourceRef


class ClaimRevisionCreate(Contract):
    expected_spec_revision: PositiveRevision
    analysis_registration_id: Identifier
    claim_id: Identifier | None = None
    expected_claim_revision: PositiveRevision | None = None
    revision_reason: ShortResearchText | None = None
    proposition_type: Literal["DESCRIPTIVE", "ASSOCIATIONAL", "PREDICTIVE", "CAUSAL"]
    statement: ResearchText
    scope: ClaimScope
    quantity: ClaimQuantity | None = None
    result_assertion: ResultAssertion | None = None
    limitations: Annotated[list[ShortResearchText], Field(max_length=32)] = Field(
        default_factory=list
    )
    sources: Annotated[list[ClaimSourceUse], Field(max_length=128)] = Field(default_factory=list)
    support_status: Literal["PROPOSED", "SUPPORTED", "UNVERIFIED", "CONFLICTING", "WITHDRAWN"] = (
        "PROPOSED"
    )

    @model_validator(mode="after")
    def revision_shape(self) -> Self:
        successor = self.claim_id is not None
        if successor != (self.expected_claim_revision is not None) or successor != (
            self.revision_reason is not None
        ):
            raise ValueError("A claim successor requires identity, expected revision and reason")
        uses = [value.model_dump_json() for value in self.sources]
        if len(set(uses)) != len(uses):
            raise ValueError("Claim source relations must be unique")
        if self.result_assertion is not None and self.quantity is None:
            raise ValueError("A result assertion requires an expected quantity and unit")
        return self


class ClaimRevisionView(Contract):
    expected_spec_revision: PositiveRevision
    analysis_registration_id: Identifier
    claim_id: Identifier
    expected_claim_revision: PositiveRevision | None = None
    revision_reason: ShortResearchText | None = None
    proposition_type: Literal["DESCRIPTIVE", "ASSOCIATIONAL", "PREDICTIVE", "CAUSAL"]
    statement: ResearchText
    scope: ClaimScope
    quantity: ClaimQuantity | None = None
    result_assertion: ResultAssertion | None = None
    limitations: Annotated[list[ShortResearchText], Field(max_length=32)] = Field(
        default_factory=list
    )
    sources: Annotated[list[ClaimSourceUse], Field(max_length=128)] = Field(default_factory=list)
    support_status: Literal["PROPOSED", "SUPPORTED", "UNVERIFIED", "CONFLICTING", "WITHDRAWN"]
    claim_revision_id: Identifier
    revision: PositiveRevision
    task_id: Identifier
    supersedes_claim_revision_id: Identifier | None = None
    content_digest: Sha256Digest
    created_by: Identifier
    created_at: str


class ResearchObjectRef(Contract):
    kind: Literal["ANALYSIS_REVISION", "CLAIM_REVISION", "POLICY_DECISION"]
    object_id: Identifier
    revision: PositiveRevision | None = None


class ProvenanceRelationView(Contract):
    relation_id: Identifier
    relation: Literal[
        "USED",
        "GENERATED",
        "DERIVED_FROM",
        "CITED",
        "SUPPORTED_BY",
        "CHECKED_BY",
        "INVALIDATED_BY",
    ]
    provenance_kind: Literal["DECLARED_PROVENANCE", "OBSERVED_PROVENANCE"]
    subject: ResearchObjectRef
    object: SourceRef
    activity_execution_id: Identifier | None = None
    agent_id: Identifier
    role: Identifier
    recorded_at: str


class ResearchView(Contract):
    task_id: Identifier
    analysis_revisions: list[AnalysisRevisionView]
    registrations: list[AnalysisRegistrationView]
    claims: list[ClaimRevisionView]
    provenance: list[ProvenanceRelationView]
    policy_decisions: list[PolicyDecisionView]
    missing: list[PolicyReason]
