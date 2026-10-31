"""Stable Temporal commands for delivery through the V7 business outbox.

Public/API/Webhook handlers do not call Temporal directly. Their V7
transactional service freezes a :class:`TemporalDispatchCommand` beside the
run/audit fact and places it in the existing outbox. A worker may deliver that
command more than once; it always carries the same run, schedule, signal, and
action identities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .client import TemporalTriggerClient
from .models import (
    JsonValue,
    TriggerKind,
    WorkflowContractError,
    WorkflowInvocation,
    contains_secret_name,
    digest,
    json_copy,
)
from .observability import trigger_span

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class TemporalDispatchKind(StrEnum):
    START = "start"
    SCHEDULE = "schedule"
    APPROVE = "approve"
    RECONCILE = "reconcile"
    CANCEL = "cancel"


@dataclass(frozen=True)
class TemporalDispatchCommand:
    """Credential-free, immutable work handed from V7 outbox to Temporal."""

    dispatch_id: str
    kind: TemporalDispatchKind
    payload: dict[str, JsonValue]
    schema_version: str = "v7-temporal-dispatch-v1"

    def __post_init__(self) -> None:
        try:
            kind = (
                self.kind
                if isinstance(self.kind, TemporalDispatchKind)
                else TemporalDispatchKind(self.kind)
            )
        except ValueError as error:
            raise WorkflowContractError("Temporal dispatch kind is unsupported") from error
        object.__setattr__(self, "kind", kind)
        if self.schema_version != "v7-temporal-dispatch-v1":
            raise WorkflowContractError("Temporal dispatch schema is unsupported")
        payload = json_copy(self.payload)
        if not isinstance(payload, dict) or contains_secret_name(payload):
            raise WorkflowContractError("Temporal dispatch payload is invalid or sensitive")
        _validate_payload(kind, payload)
        expected = _dispatch_id(kind, payload)
        if self.dispatch_id != expected:
            raise WorkflowContractError(
                "Temporal dispatch identity does not match its frozen payload"
            )
        object.__setattr__(self, "payload", payload)

    @classmethod
    def start(cls, invocation: WorkflowInvocation) -> TemporalDispatchCommand:
        if invocation.trigger is TriggerKind.SCHEDULE:
            raise WorkflowContractError("scheduled invocations require a schedule dispatch")
        payload: dict[str, JsonValue] = {"invocation": invocation.to_mapping()}
        return cls(
            _dispatch_id(TemporalDispatchKind.START, payload), TemporalDispatchKind.START, payload
        )

    @classmethod
    def schedule(
        cls, schedule_id: str, invocation: WorkflowInvocation, *, every_seconds: int
    ) -> TemporalDispatchCommand:
        payload: dict[str, JsonValue] = {
            "schedule_id": schedule_id,
            "invocation": invocation.to_mapping(),
            "every_seconds": every_seconds,
        }
        return cls(
            _dispatch_id(TemporalDispatchKind.SCHEDULE, payload),
            TemporalDispatchKind.SCHEDULE,
            payload,
        )

    @classmethod
    def approve(
        cls, run_id: str, *, node_key: str, approved: bool, actor_id: str
    ) -> TemporalDispatchCommand:
        payload: dict[str, JsonValue] = {
            "run_id": run_id,
            "node_key": node_key,
            "approved": approved,
            "actor_id": actor_id,
        }
        return cls(
            _dispatch_id(TemporalDispatchKind.APPROVE, payload),
            TemporalDispatchKind.APPROVE,
            payload,
        )

    @classmethod
    def reconcile(cls, run_id: str, *, node_key: str) -> TemporalDispatchCommand:
        payload: dict[str, JsonValue] = {"run_id": run_id, "node_key": node_key}
        return cls(
            _dispatch_id(TemporalDispatchKind.RECONCILE, payload),
            TemporalDispatchKind.RECONCILE,
            payload,
        )

    @classmethod
    def cancel(cls, run_id: str) -> TemporalDispatchCommand:
        # Any operator explanation belongs only in the platform audit record;
        # a Temporal signal must remain credential- and prompt-free.
        payload: dict[str, JsonValue] = {"run_id": run_id}
        return cls(
            _dispatch_id(TemporalDispatchKind.CANCEL, payload),
            TemporalDispatchKind.CANCEL,
            payload,
        )

    @classmethod
    def from_mapping(cls, value: object) -> TemporalDispatchCommand:
        if not isinstance(value, dict):
            raise WorkflowContractError("Temporal dispatch must be an object")
        required = {"schema_version", "dispatch_id", "kind", "payload"}
        if set(value) != required or not isinstance(value.get("payload"), dict):
            raise WorkflowContractError("Temporal dispatch has unknown or missing fields")
        try:
            kind = TemporalDispatchKind(value["kind"])
        except (KeyError, TypeError, ValueError) as error:
            raise WorkflowContractError("Temporal dispatch is invalid") from error
        return cls(
            dispatch_id=value["dispatch_id"],
            kind=kind,
            payload=value["payload"],
            schema_version=value["schema_version"],
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "dispatch_id": self.dispatch_id,
            "kind": self.kind.value,
            "payload": dict(self.payload),
        }


class TemporalOutboxDispatcher:
    """Deliver one already-persisted command to a Temporal client.

    The caller owns lease, retry and completion bookkeeping in V7's existing
    outbox. This class intentionally does not mark a command complete: a
    failed call remains retryable under the exact same ``dispatch_id`` and
    immutable payload.
    """

    def __init__(self, trigger_client: TemporalTriggerClient) -> None:
        self._client = trigger_client

    async def dispatch(self, command: TemporalDispatchCommand) -> None:
        run_id = _dispatch_run_id(command)
        with trigger_span(
            "outbox-delivery",
            dispatch_id=command.dispatch_id,
            run_id=run_id,
        ):
            if command.kind is TemporalDispatchKind.START:
                invocation = _invocation(command.payload)
                if invocation.trigger is TriggerKind.MANUAL:
                    await self._client.start_manual(invocation)
                elif invocation.trigger is TriggerKind.API:
                    await self._client.start_api(invocation)
                elif invocation.trigger is TriggerKind.WEBHOOK:
                    await self._client.start_webhook(invocation)
                else:  # guarded by the frozen command validator
                    raise WorkflowContractError("schedule invocation cannot use a start dispatch")
                return
            if command.kind is TemporalDispatchKind.SCHEDULE:
                await self._client.create_interval_schedule(
                    str(command.payload["schedule_id"]),
                    _invocation(command.payload),
                    every_seconds=int(command.payload["every_seconds"]),
                    dispatch_id=command.dispatch_id,
                )
                return
            if command.kind is TemporalDispatchKind.APPROVE:
                await self._client.approve(
                    str(command.payload["run_id"]),
                    node_key=str(command.payload["node_key"]),
                    approved=bool(command.payload["approved"]),
                    actor_id=str(command.payload["actor_id"]),
                )
                return
            if command.kind is TemporalDispatchKind.RECONCILE:
                await self._client.reconcile(
                    str(command.payload["run_id"]), node_key=str(command.payload["node_key"])
                )
                return
            if command.kind is TemporalDispatchKind.CANCEL:
                await self._client.cancel(str(command.payload["run_id"]), reason="CANCEL_REQUESTED")
                return
            raise WorkflowContractError("Temporal dispatch kind is unsupported")


def _validate_payload(kind: TemporalDispatchKind, payload: dict[str, Any]) -> None:
    if kind is TemporalDispatchKind.START:
        if set(payload) != {"invocation"}:
            raise WorkflowContractError("start dispatch payload is invalid")
        invocation = _invocation(payload)
        if invocation.trigger is TriggerKind.SCHEDULE:
            raise WorkflowContractError("scheduled invocation requires a schedule dispatch")
        return
    if kind is TemporalDispatchKind.SCHEDULE:
        if set(payload) != {"schedule_id", "invocation", "every_seconds"}:
            raise WorkflowContractError("schedule dispatch payload is invalid")
        if not _portable_identifier(payload["schedule_id"]):
            raise WorkflowContractError("schedule dispatch id is invalid")
        every_seconds = payload["every_seconds"]
        if (
            not isinstance(every_seconds, int)
            or isinstance(every_seconds, bool)
            or every_seconds < 60
        ):
            raise WorkflowContractError("schedule dispatch interval is invalid")
        if _invocation(payload).trigger is not TriggerKind.SCHEDULE:
            raise WorkflowContractError("schedule dispatch requires a scheduled invocation")
        return
    if kind is TemporalDispatchKind.APPROVE:
        if set(payload) != {"run_id", "node_key", "approved", "actor_id"}:
            raise WorkflowContractError("approval dispatch payload is invalid")
        if (
            not _portable_identifier(payload["run_id"])
            or not _node_key(payload["node_key"])
            or not isinstance(payload["approved"], bool)
            or not _portable_identifier(payload["actor_id"])
        ):
            raise WorkflowContractError("approval dispatch payload is invalid")
        return
    if kind is TemporalDispatchKind.RECONCILE:
        if set(payload) != {"run_id", "node_key"}:
            raise WorkflowContractError("reconciliation dispatch payload is invalid")
        if not _portable_identifier(payload["run_id"]) or not _node_key(payload["node_key"]):
            raise WorkflowContractError("reconciliation dispatch payload is invalid")
        return
    if kind is TemporalDispatchKind.CANCEL:
        if set(payload) != {"run_id"} or not _portable_identifier(payload["run_id"]):
            raise WorkflowContractError("cancellation dispatch payload is invalid")
        return
    raise WorkflowContractError("Temporal dispatch kind is unsupported")


def _invocation(payload: dict[str, Any]) -> WorkflowInvocation:
    try:
        return WorkflowInvocation.from_mapping(payload["invocation"])
    except (KeyError, WorkflowContractError, TypeError) as error:
        raise WorkflowContractError("Temporal dispatch invocation is invalid") from error


def _portable_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _node_key(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512 and "\x00" not in value


def _dispatch_id(kind: TemporalDispatchKind, payload: dict[str, JsonValue]) -> str:
    return "td-" + digest({"kind": kind.value, "payload": payload})[7:55]


def _dispatch_run_id(command: TemporalDispatchCommand) -> str | None:
    if command.kind in {TemporalDispatchKind.START, TemporalDispatchKind.SCHEDULE}:
        invocation = command.payload.get("invocation")
        if isinstance(invocation, dict) and isinstance(invocation.get("run_id"), str):
            return invocation["run_id"]
        return None
    run_id = command.payload.get("run_id")
    return run_id if isinstance(run_id, str) else None
