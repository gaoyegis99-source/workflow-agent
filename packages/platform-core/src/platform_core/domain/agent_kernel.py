"""Pure P6.1 rules for a ledger-owned Agent decision cycle."""

from __future__ import annotations

from dataclasses import dataclass

from platform_core.domain.control import require, valid_identifier

AGENT_RUN_STATES = frozenset({"READY", "ADVANCING", "WAITING", "QUESTION", "BLOCKED", "STOPPED"})


@dataclass(frozen=True, slots=True)
class AgentLeaseClaim:
    agent_run_id: str
    project_id: str
    task_id: str
    runtime_ref: str
    epoch: int
    generation: int
    owner: str
    task_spec_revision: int
    control_version: int
    runtime_profile: dict

    def __post_init__(self) -> None:
        require(
            all(
                valid_identifier(value)
                for value in (
                    self.agent_run_id,
                    self.project_id,
                    self.task_id,
                    self.runtime_ref,
                    self.owner,
                )
            ),
            "INVALID_BINDING",
            "Invalid Agent lease identity",
            422,
        )
        require(
            all(
                type(value) is int and value > 0
                for value in (
                    self.epoch,
                    self.generation,
                    self.task_spec_revision,
                    self.control_version,
                )
            ),
            "INVALID_BINDING",
            "Invalid Agent lease revision",
            422,
        )


def require_agent_task_active(task, run: dict, generation: int) -> None:
    require(
        task.lifecycle in {"OPEN", "COMPLETED"},
        "PRECONDITION_UNRESOLVED",
        "Task is no longer available to the Agent",
    )
    require(task.control == "ACTIVE", "PRECONDITION_UNRESOLVED", "Task is paused")
    require(run["status"] == "ADVANCING", "REVISION_CONFLICT", "AgentRun is not advancing")
    require(run["generation"] == generation, "REVISION_CONFLICT", "Agent generation changed")
    require(
        run["task_spec_revision"] == task.spec_revision,
        "REVISION_CONFLICT",
        "Task requirements changed",
    )
    require(
        run["control_version"] == task.control_version,
        "REVISION_CONFLICT",
        "Task control changed",
    )


def outcome_state(kind: str) -> tuple[str, str | None]:
    if kind == "CLARIFICATION_REQUEST":
        return "QUESTION", None
    if kind == "UNABLE_TO_PROCEED":
        return "BLOCKED", "MODEL_UNABLE_TO_PROCEED"
    return "WAITING", None
