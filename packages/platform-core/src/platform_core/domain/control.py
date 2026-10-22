"""Pure control rules. No transport, persistence, clocks or SDKs."""

import re
from dataclasses import dataclass, replace
from datetime import datetime


class RuleViolation(Exception):
    def __init__(self, code: str, message: str, status: int = 409, retry: str = "NONE"):
        super().__init__(message)
        self.code, self.message, self.status, self.retry = code, message, status, retry


def require(condition: bool, code: str, message: str, status: int = 409) -> None:
    if not condition:
        retry = "AFTER_STATE_CHANGE" if status == 409 else "NONE"
        raise RuleViolation(code, message, status, retry)


def valid_identifier(value: object) -> bool:
    """Public Identifier grammar, kept framework-free and tested against the DTO."""
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is not None
    )


@dataclass(frozen=True, slots=True)
class Identity:
    principal_id: str
    # Service-side continuations (Agent/checker orchestration) retain the
    # authenticated principal but never retain the user's bearer token.
    token_id: str | None


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    project_id: str
    spec_revision: int
    control_version: int
    lifecycle: str
    control: str
    cleanup_status: str
    grant_epoch: int
    created_at: datetime

    def version(self, expected: int) -> None:
        require(self.control_version == expected, "REVISION_CONFLICT", "Control version changed")

    def open(self) -> None:
        require(self.lifecycle == "OPEN", "PRECONDITION_UNRESOLVED", "Task is no longer open")

    def admissible(self, revision: int) -> None:
        self.open()
        require(self.control == "ACTIVE", "PRECONDITION_UNRESOLVED", "Task is paused")
        require(self.spec_revision == revision, "REVISION_CONFLICT", "Task requirements changed")

    def control_change(self, operation: str, expected: int, *, pending: bool = False) -> "Task":
        self.version(expected)
        self.open()
        if operation == "cancel":
            return replace(
                self,
                lifecycle="CANCELLED",
                control_version=self.control_version + 1,
                cleanup_status="PENDING" if pending else "CLEAR",
                grant_epoch=self.grant_epoch + 1,
            )
        require(operation in {"pause", "resume"}, "MALFORMED_REQUEST", "Unknown control", 400)
        desired = "PAUSED" if operation == "pause" else "ACTIVE"
        require(self.control != desired, "PRECONDITION_UNRESOLVED", "Control already applied")
        return replace(self, control=desired, control_version=self.control_version + 1)


@dataclass(frozen=True, slots=True)
class Budget:
    task_id: str
    execution_count: int
    max_in_flight: int
    used: int = 0
    reserved: int = 0
    in_flight: int = 0

    def reserve(self) -> "Budget":
        require(
            self.used + self.reserved < self.execution_count
            and self.in_flight < self.max_in_flight,
            "BUDGET_BLOCKED",
            "Task execution budget is exhausted",
        )
        return replace(self, reserved=self.reserved + 1, in_flight=self.in_flight + 1)

    def account(self, *, was_charged: bool, charged: bool, stopped: bool) -> "Budget":
        require(not was_charged or charged, "PRECONDITION_UNRESOLVED", "Usage cannot be retracted")
        newly_charged = int(charged and not was_charged)
        released = int(stopped and not charged)
        updated = replace(
            self,
            used=self.used + newly_charged,
            reserved=self.reserved - newly_charged - released,
            in_flight=self.in_flight - int(stopped),
        )
        require(
            min(updated.used, updated.reserved, updated.in_flight) >= 0
            and updated.used + updated.reserved <= updated.execution_count,
            "PRECONDITION_UNRESOLVED",
            "Invalid budget settlement",
        )
        return updated


@dataclass(frozen=True, slots=True)
class ExternalObservation:
    event_id: str
    action_id: str
    revision: int
    state: str
    execution_id: str | None
    executed: bool

    @property
    def stopped(self) -> bool:
        return self.state in {"SUCCEEDED", "FAILED", "CANCELLED", "SEALED", "REJECTED"}

    def validate(self) -> None:
        require(
            type(self.revision) is int and self.revision > 0,
            "INVALID_BINDING",
            "Invalid observation revision",
            422,
        )
        require(
            isinstance(self.event_id, str)
            and 0 < len(self.event_id) <= 128
            and valid_identifier(self.action_id)
            and (self.execution_id is None or valid_identifier(self.execution_id)),
            "INVALID_BINDING",
            "Invalid observation identity",
            422,
        )
        require(
            isinstance(self.state, str)
            and self.state
            in {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "SEALED", "REJECTED"},
            "INVALID_BINDING",
            "Unknown external state",
            422,
        )
        require(
            type(self.executed) is bool
            and (not self.executed or self.execution_id is not None)
            and (self.state not in {"RUNNING", "SUCCEEDED"} or self.executed)
            and (self.state not in {"QUEUED", "SEALED", "REJECTED"} or not self.executed),
            "INVALID_BINDING",
            "Inconsistent execution evidence",
            422,
        )
