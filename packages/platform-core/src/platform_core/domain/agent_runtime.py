"""Framework-independent identities and values for the P6 agent runtime boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from platform_core.domain.control import require, valid_identifier

PROPOSAL_KINDS = frozenset(
    {
        "PLAN_REVISION",
        "READ_TOOL_REQUESTS",
        "ACTION_REQUESTS",
        "RESEARCH_REVISION_REQUEST",
        "CLARIFICATION_REQUEST",
        "WAIT_REQUEST",
        "DELIVERABLE_REQUEST",
        "ACCEPTANCE_REQUEST",
        "EXPLANATION",
        "UNABLE_TO_PROCEED",
    }
)


def canonical_digest(value: Mapping[str, Any]) -> str:
    """Return the stable digest used at the runtime boundary."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionFrame:
    agent_run_id: str
    frame_id: str
    task_id: str
    task_spec_revision: int
    control_version: int
    generation: int
    context_manifest_digest: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require(
            all(
                valid_identifier(value)
                for value in (self.agent_run_id, self.frame_id, self.task_id)
            ),
            "INVALID_BINDING",
            "Invalid agent decision frame identity",
            422,
        )
        require(
            all(
                type(value) is int and value > 0
                for value in (self.task_spec_revision, self.control_version, self.generation)
            ),
            "INVALID_BINDING",
            "Invalid agent decision frame revision",
            422,
        )
        require(
            isinstance(self.context_manifest_digest, str)
            and len(self.context_manifest_digest) == 71
            and self.context_manifest_digest.startswith("sha256:")
            and all(
                character in "0123456789abcdef" for character in self.context_manifest_digest[7:]
            ),
            "INVALID_BINDING",
            "Invalid context manifest digest",
            422,
        )
        canonical_digest(self.payload)

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_record())

    def as_record(self) -> dict[str, Any]:
        return {
            "agent_run_id": self.agent_run_id,
            "frame_id": self.frame_id,
            "task_id": self.task_id,
            "task_spec_revision": self.task_spec_revision,
            "control_version": self.control_version,
            "generation": self.generation,
            "context_manifest_digest": self.context_manifest_digest,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    kind: str
    frame_id: str
    expected_task_revision: int
    body: Mapping[str, Any]

    def __post_init__(self) -> None:
        require(self.kind in PROPOSAL_KINDS, "INVALID_BINDING", "Unknown proposal kind", 422)
        require(
            valid_identifier(self.frame_id),
            "INVALID_BINDING",
            "Invalid proposal frame identity",
            422,
        )
        require(
            type(self.expected_task_revision) is int and self.expected_task_revision > 0,
            "INVALID_BINDING",
            "Invalid proposal task revision",
            422,
        )
        canonical_digest(self.body)

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_record())

    def as_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "frame_id": self.frame_id,
            "expected_task_revision": self.expected_task_revision,
            "body": dict(self.body),
        }


@dataclass(frozen=True, slots=True)
class RuntimeReceipt:
    runtime_ref: str
    profile_id: str
    profile_digest: str
    epoch: int
    checkpoint_revision: int
    state: str


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    runtime_ref: str
    epoch: int
    checkpoint_revision: int
    state: str
    last_frame_id: str | None = None
    last_proposal_digest: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeInterruptReceipt:
    runtime_ref: str
    epoch: int
    state: str
    reason: str


@dataclass(frozen=True, slots=True)
class RuntimeTraceRef:
    runtime_ref: str
    epoch: int
    trace_digest: str
    event_count: int


@dataclass(frozen=True, slots=True)
class WakeBatch:
    wake_id: str
    event_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        require(valid_identifier(self.wake_id), "INVALID_BINDING", "Invalid wake identity", 422)
        require(
            bool(self.event_ids) and all(valid_identifier(event) for event in self.event_ids),
            "INVALID_BINDING",
            "A wake batch requires valid event identities",
            422,
        )


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model_call_id: str
    frame: DecisionFrame
    profile_id: str
    request_digest: str
    input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelObservation:
    model_call_id: str
    status: str
    provider_ref: str | None
    response: Mapping[str, Any] | None
    usage: Mapping[str, int] | None


@dataclass(frozen=True, slots=True)
class ToolRequest:
    call_id: str
    tool_id: str
    tool_version: str
    purpose: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call_id: str
    status: str
    observation_ref: str | None
    result: Mapping[str, Any] | None
