"""P2 control DTOs. No public computation request endpoint is registered."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, field_validator

from platform_contracts.science import (
    AcceptanceSpecification,
    TableAggregateParameters,
    TableCastParameters,
    TableFilterParameters,
    TableProfileParameters,
    TableProjectParameters,
    TableSelection,
    TableValidateParameters,
)
from platform_contracts.v1 import (
    AssetRef,
    ComputationTarget,
    Contract,
    Identifier,
    PositiveRevision,
    Sha256Digest,
)

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ProjectCreate(Contract):
    name: ShortText


class ProjectView(ProjectCreate):
    project_id: Identifier
    created_at: str
    role: Literal["VIEWER", "EDITOR", "OWNER"]


class WorkspaceResourceCounts(Contract):
    assets: Annotated[int, Field(ge=0)]
    workflow_definitions: Annotated[int, Field(ge=0)]
    workflow_releases: Annotated[int, Field(ge=0)]
    operator_versions: Annotated[int, Field(ge=0)]
    operator_materials: Annotated[int, Field(ge=0)]


class TaskStateCounts(Contract):
    OPEN: Annotated[int, Field(ge=0)]
    PAUSED: Annotated[int, Field(ge=0)]
    CANCELLED: Annotated[int, Field(ge=0)]
    COMPLETED: Annotated[int, Field(ge=0)]


class ExecutionStateCounts(Contract):
    RECORDED: Annotated[int, Field(ge=0)]
    DISPATCHING: Annotated[int, Field(ge=0)]
    RECONCILING: Annotated[int, Field(ge=0)]
    QUEUED: Annotated[int, Field(ge=0)]
    RUNNING: Annotated[int, Field(ge=0)]
    SUCCEEDED: Annotated[int, Field(ge=0)]
    FAILED: Annotated[int, Field(ge=0)]
    CANCELLED: Annotated[int, Field(ge=0)]
    SEALED: Annotated[int, Field(ge=0)]
    REJECTED: Annotated[int, Field(ge=0)]


class WorkspaceSummaryView(Contract):
    project: ProjectView
    role: Literal["VIEWER", "EDITOR", "OWNER"]
    phase: Literal["P8.2"]
    release_digest: Sha256Digest | None
    as_of: Annotated[str, Field(json_schema_extra={"format": "date-time"})]
    resources: WorkspaceResourceCounts
    tasks: TaskStateCounts
    executions: ExecutionStateCounts


class ExecutionSummary(Contract):
    action_id: Identifier
    execution_id: Identifier | None
    task_id: Identifier
    state: Literal[
        "RECORDED",
        "DISPATCHING",
        "RECONCILING",
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "SEALED",
        "REJECTED",
    ]
    spec_revision: PositiveRevision
    decision_source: Literal["CLIENT", "AGENT"]
    target_kind: Literal["OPERATOR", "WORKFLOW", "CHECK", "UNKNOWN"]
    target_id: str
    stopped: bool
    cancel_requested: bool


class ExecutionSummaryPage(Contract):
    items: list[ExecutionSummary]
    next_cursor: str | None


class AgentOperatorIntent(Contract):
    operator_id: Literal[
        "table-profile",
        "table-validate",
        "table-project",
        "table-filter",
        "table-cast",
        "table-aggregate",
    ]
    version_id: Literal["1.0.0"] = "1.0.0"
    input: TableSelection
    parameters: (
        TableProfileParameters
        | TableValidateParameters
        | TableProjectParameters
        | TableFilterParameters
        | TableCastParameters
        | TableAggregateParameters
    )

    @field_validator("parameters")
    @classmethod
    def parameters_match_operator(cls, value, info):
        operator = info.data.get("operator_id")
        expected = {
            "table-profile": TableProfileParameters,
            "table-validate": TableValidateParameters,
            "table-project": TableProjectParameters,
            "table-filter": TableFilterParameters,
            "table-cast": TableCastParameters,
            "table-aggregate": TableAggregateParameters,
        }.get(operator)
        if expected is not None and not isinstance(value, expected):
            raise ValueError("Agent operator intent parameters differ from the operator")
        return value


class TaskSpec(Contract):
    acceptance_spec: AcceptanceSpecification | None = None
    goal: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    requirements: Annotated[list[ShortText], Field(max_length=32)] = Field(default_factory=list)
    # Optional user-frozen P6.6 intents. The Agent may choose when to invoke
    # them, but cannot synthesize or alter their scientific payload.
    agent_workflow: Annotated[dict[str, JsonValue], Field(max_length=32)] | None = None
    agent_analysis: Annotated[dict[str, JsonValue], Field(max_length=64)] | None = None
    agent_claim: Annotated[dict[str, JsonValue], Field(max_length=64)] | None = None
    agent_operator_intents: Annotated[list[AgentOperatorIntent], Field(max_length=8)] = Field(
        default_factory=list
    )

    @field_validator("agent_operator_intents")
    @classmethod
    def distinct_operator_intents(cls, values):
        identities = {(value.operator_id, value.version_id) for value in values}
        if len(identities) != len(values):
            raise ValueError("Agent operator intents must have unique operator versions")
        return values


class BudgetLimits(Contract):
    execution_count: Annotated[int, Field(ge=0, le=1_000_000)]
    max_in_flight: Annotated[int, Field(ge=1, le=1000)]


class ModelBudgetLimits(Contract):
    max_model_calls: Annotated[int, Field(ge=1, le=10_000)]
    max_input_tokens: Annotated[int, Field(ge=1, le=10_000_000_000)]
    max_output_tokens: Annotated[int, Field(ge=1, le=1_000_000_000)]
    max_reasoning_tokens: Annotated[int, Field(ge=0, le=1_000_000_000)] | None = None
    max_estimated_cost_microunits: Annotated[int, Field(ge=1, le=10**15)]
    max_agent_wall_seconds: Annotated[int, Field(ge=1, le=31_536_000)]
    max_decision_frames: Annotated[int, Field(ge=1, le=100_000)]


class TaskCreate(Contract):
    execution_mode: Literal["DIRECT", "AGENT"]
    spec: TaskSpec
    budget: BudgetLimits
    model_budget: ModelBudgetLimits | None = None


class TaskView(Contract):
    task_id: Identifier
    project_id: Identifier
    spec_revision: PositiveRevision
    control_version: PositiveRevision
    lifecycle: Literal["OPEN", "CANCELLED", "COMPLETED"]
    control: Literal["ACTIVE", "PAUSED"]
    cleanup_status: Literal["CLEAR", "PENDING", "UNKNOWN"]
    grant_epoch: PositiveRevision
    created_at: str
    execution_mode: Literal["DIRECT", "AGENT"]
    spec: TaskSpec
    completion_validity: Literal["CURRENT", "STALE"] | None = None
    completion_validity_revision: PositiveRevision | None = None


class ControlRequest(Contract):
    expected_control_version: PositiveRevision


class ReviseTask(ControlRequest):
    expected_spec_revision: PositiveRevision
    spec: TaskSpec


class GrantCreate(Contract):
    derived_outputs: bool = False
    acceptance_profiles: Annotated[
        list[Literal["p3-timeseries-v1", "open-science-v1"]], Field(max_length=2)
    ] = Field(default_factory=list)
    task_id: Identifier
    targets: Annotated[list[ComputationTarget], Field(min_length=1, max_length=32)]
    resources: Annotated[list[AssetRef], Field(max_length=128)]
    expires_at: Annotated[str, Field(json_schema_extra={"format": "date-time"})]

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: str) -> str:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Expiry must include a timezone")
        return instant.astimezone(UTC).isoformat()


class GrantView(GrantCreate):
    grant_id: Identifier
    project_id: Identifier
    revoked: bool
    created_by: Identifier
    generation: PositiveRevision
    revoked_at: str | None
    revoked_by: Identifier | None


class BudgetView(BudgetLimits):
    task_id: Identifier
    used: Annotated[int, Field(ge=0)]
    reserved: Annotated[int, Field(ge=0)]
    in_flight: Annotated[int, Field(ge=0)]


class ModelUsageView(ModelBudgetLimits):
    agent_run_id: Identifier
    task_id: Identifier
    profile_id: Identifier
    profile_digest: Sha256Digest
    used_calls: Annotated[int, Field(ge=0)]
    reserved_calls: Annotated[int, Field(ge=0)]
    used_input_tokens: Annotated[int, Field(ge=0)]
    reserved_input_tokens: Annotated[int, Field(ge=0)]
    used_output_tokens: Annotated[int, Field(ge=0)]
    reserved_output_tokens: Annotated[int, Field(ge=0)]
    used_reasoning_tokens: Annotated[int, Field(ge=0)]
    reserved_reasoning_tokens: Annotated[int, Field(ge=0)]
    used_cost_microunits: Annotated[int, Field(ge=0)]
    reserved_cost_microunits: Annotated[int, Field(ge=0)]
    unknown_result_calls: Annotated[int, Field(ge=0)]
    unknown_usage_calls: Annotated[int, Field(ge=0)]


class ProjectPage(Contract):
    items: list[ProjectView]
    next_cursor: str | None


class TaskPage(Contract):
    items: list[TaskView]
    next_cursor: str | None


class GrantPage(Contract):
    items: list[GrantView]
    next_cursor: str | None


class Readiness(Contract):
    status: Literal["ready", "unavailable"]


class ActionRequest(Contract):
    """Internal application test boundary, not a public P2 HTTP request."""

    task_id: Identifier
    expected_spec_revision: PositiveRevision
    target: ComputationTarget
    resources: Annotated[list[AssetRef], Field(max_length=128)]
    parameters: dict[str, str | int | bool] = Field(default_factory=dict)
