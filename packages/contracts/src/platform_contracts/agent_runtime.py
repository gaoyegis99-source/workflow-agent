"""Strict internal contracts for the P6 runtime selection boundary."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from platform_contracts.v1 import (
    Contract,
    Identifier,
    PositiveRevision,
    Sha256Digest,
    TaskRef,
    VersionId,
)

Purpose = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class AgentFrame(Contract):
    agent_run_id: Identifier
    frame_id: Identifier
    task_ref: TaskRef
    control_version: PositiveRevision
    generation: PositiveRevision
    context_manifest_digest: Sha256Digest
    payload: Annotated[dict[str, JsonValue], Field(max_length=128)] = Field(default_factory=dict)


class ToolRequest(Contract):
    call_id: Identifier
    # Namespaced catalogue identities use dots; VersionId has the same bounded
    # ASCII grammar and also rejects the floating value "latest".
    tool_id: VersionId
    tool_version: VersionId
    purpose: Purpose
    arguments: Annotated[dict[str, JsonValue], Field(max_length=128)] = Field(default_factory=dict)
    expected_observation: Purpose

    @field_validator("tool_version")
    @classmethod
    def reject_floating_version(cls, value: str) -> str:
        if value.lower() == "latest":
            raise ValueError("A fixed tool_version is required")
        return value


class ProposalBase(Contract):
    frame_id: Identifier
    expected_task_revision: PositiveRevision


class PlanStep(Contract):
    step_id: Identifier
    purpose: Purpose
    depends_on: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)


class PlanRevisionProposal(ProposalBase):
    kind: Literal["PLAN_REVISION"]
    reason: Purpose
    steps: Annotated[list[PlanStep], Field(min_length=1, max_length=128)]


class ToolRequestsProposal(ProposalBase):
    kind: Literal["READ_TOOL_REQUESTS", "ACTION_REQUESTS"]
    requests: Annotated[list[ToolRequest], Field(min_length=1, max_length=32)]


class ResearchRevisionProposal(ProposalBase):
    kind: Literal["RESEARCH_REVISION_REQUEST"]
    operation: Literal["CREATE_EXPLORATORY", "REGISTER", "CREATE_CLAIM"]
    payload: Annotated[dict[str, JsonValue], Field(max_length=128)]


class ClarificationProposal(ProposalBase):
    kind: Literal["CLARIFICATION_REQUEST"]
    gap_id: Identifier
    question: Purpose
    why_needed: Purpose
    answer_schema: Annotated[dict[str, JsonValue], Field(max_length=64)]
    blocked_actions: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)


class WaitDependency(Contract):
    owner: Literal["PLATFORM", "DATA", "KNOWLEDGE", "EXECUTION", "ACCEPTANCE", "MODEL"]
    object_id: Identifier
    minimum_revision: PositiveRevision | None = None


class WaitProposal(ProposalBase):
    kind: Literal["WAIT_REQUEST"]
    reason: Literal["EXECUTION", "USER", "RECONCILIATION", "BUDGET", "PAUSED"]
    dependencies: Annotated[list[WaitDependency], Field(min_length=1, max_length=64)]


class DeliverableProposal(ProposalBase):
    kind: Literal["DELIVERABLE_REQUEST"]
    payload: Annotated[dict[str, JsonValue], Field(max_length=128)]


class AcceptanceProposal(ProposalBase):
    kind: Literal["ACCEPTANCE_REQUEST"]
    payload: Annotated[dict[str, JsonValue], Field(max_length=128)]


class ExplanationProposal(ProposalBase):
    kind: Literal["EXPLANATION"]
    message: Annotated[str, StringConstraints(min_length=1, max_length=16000)]
    cited_refs: Annotated[list[Identifier], Field(max_length=128)] = Field(default_factory=list)


class UnableProposal(ProposalBase):
    kind: Literal["UNABLE_TO_PROCEED"]
    code: Literal["UNSUPPORTED", "MISSING_INPUT", "CONFLICT", "EXTERNAL_BLOCKER"]
    reasons: Annotated[list[Purpose], Field(min_length=1, max_length=32)]


DecisionProposal = Annotated[
    PlanRevisionProposal
    | ToolRequestsProposal
    | ResearchRevisionProposal
    | ClarificationProposal
    | WaitProposal
    | DeliverableProposal
    | AcceptanceProposal
    | ExplanationProposal
    | UnableProposal,
    Field(discriminator="kind"),
]

_DECISION_ADAPTER = TypeAdapter(DecisionProposal)


def parse_decision_proposal(value):
    """Keep the DTO framework behind the contracts package boundary."""
    return _DECISION_ADAPTER.validate_python(value)


def decision_proposal_json_schema() -> dict:
    schema = _DECISION_ADAPTER.json_schema()
    # Pydantic intentionally renders JsonValue as the permissive schema ``{}``.
    # That is valid JSON Schema, but strict structured-output providers require
    # every named definition to state a concrete type, union, or reference.
    # Keep the runtime DTO permissive while spelling out the same recursive JSON
    # value language at the provider boundary.
    schema["$defs"]["JsonValue"] = {
        "anyOf": [
            {"type": "null"},
            {"type": "boolean"},
            {"type": "integer"},
            {"type": "number"},
            {"type": "string"},
            {"type": "array", "items": {"$ref": "#/$defs/JsonValue"}},
            {
                "type": "object",
                "additionalProperties": {"$ref": "#/$defs/JsonValue"},
            },
        ]
    }
    # The variants are disjoint on the required ``kind`` discriminator, so
    # ``anyOf`` has the same acceptance semantics as Pydantic's ``oneOf`` here.
    # Some Responses-compatible strict-schema implementations accept only
    # ``type``, ``anyOf``, or ``$ref`` at a schema node.
    schema["anyOf"] = schema.pop("oneOf")
    schema["type"] = "object"
    return schema


class AgentRuntimeProfile(Contract):
    schema_version: Literal[1]
    profile_id: Identifier
    adapter: Literal["PLAIN_LOOP", "LANGGRAPH"]
    framework: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    framework_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    checkpoint_kind: Literal["MEMORY", "POSTGRES"]
    control_mode: Literal["YIELD_TO_KERNEL"]
    max_model_calls_per_advance: Literal[1]
    profile_digest: Sha256Digest

    @model_validator(mode="after")
    def consistent_framework(self):
        if self.adapter == "PLAIN_LOOP" and self.framework != "none":
            raise ValueError("The plain runtime cannot claim an agent framework")
        if self.adapter == "LANGGRAPH" and self.framework != "langgraph":
            raise ValueError("The LangGraph adapter must identify its framework")
        return self
