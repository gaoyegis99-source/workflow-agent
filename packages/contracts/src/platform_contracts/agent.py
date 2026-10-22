"""Public P6 Agent projections; framework checkpoints are never exposed."""

from typing import Annotated, Any, Literal

from pydantic import Field

from platform_contracts.v1 import Contract, Identifier, PositiveRevision, Sha256Digest, TaskRef


class AgentAnswerCreate(Contract):
    expected_spec_revision: PositiveRevision
    answer: Annotated[dict[str, Any], Field(max_length=64)]


class AgentCurrentView(Contract):
    agent_run_id: Identifier
    task_ref: TaskRef
    status: Literal["READY", "ADVANCING", "WAITING", "QUESTION", "BLOCKED", "STOPPED"]
    epoch: PositiveRevision
    generation: PositiveRevision
    wake_revision: Annotated[int, Field(ge=0)]
    runtime_profile_id: Identifier
    runtime_profile_digest: Sha256Digest
    plan_revision: Annotated[int, Field(ge=0)]
    plan: dict[str, Any] | None
    blocked_reason: str | None
    questions: list[dict[str, Any]]
    recent_decisions: list[dict[str, Any]]
    read_observations: list[dict[str, Any]]
    action_links: list[dict[str, Any]] = Field(default_factory=list)
    wait_dependencies: list[dict[str, Any]] = Field(default_factory=list)
    runtime_recoveries: list[dict[str, Any]] = Field(default_factory=list)
    delivery_attempts: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: dict[str, Any] | None


class AgentAnswerReceipt(Contract):
    answer_id: Identifier
    question_id: Identifier
    agent_run_id: Identifier
    task_spec_revision: PositiveRevision
    answer_digest: Sha256Digest
    answered_at: str
