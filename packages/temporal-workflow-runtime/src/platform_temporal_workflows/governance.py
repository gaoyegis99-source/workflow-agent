"""Governance-owned side-effect admission for Temporal Activities.

Temporal provides durable scheduling, not business authority.  This module
makes every Activity ask the V7 ledger for a stable action identity and current
permission/budget decision before it crosses an external side-effect boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .models import (
    ActionCommand,
    NodeOutcome,
    OutcomeStatus,
    PluginKind,
    PluginManifest,
    PluginReference,
    WorkflowContractError,
    canonical_json,
    contains_secret_name,
    json_copy,
    validate_schema_instance,
)
from .observability import activity_span


class GovernanceError(RuntimeError):
    """A ledger refuses an operation or detects conflicting replay state."""


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.allowed and not self.reason:
            raise WorkflowContractError("denied authorization requires a reason")

    def to_mapping(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": self.reason}


@dataclass(frozen=True)
class ActionRecord:
    command: ActionCommand
    state: str
    outcome: NodeOutcome | None = None
    reservation_state: str = "RESERVED"

    def to_mapping(self) -> dict[str, object]:
        return {
            "command": self.command.to_mapping(),
            "state": self.state,
            "outcome": self.outcome.to_mapping() if self.outcome is not None else None,
            "reservation_state": self.reservation_state,
        }


@dataclass(frozen=True)
class DispatchAdmission:
    """The only ledger response that may authorize a first external invoke."""

    record: ActionRecord
    invoke: bool

    def to_mapping(self) -> dict[str, object]:
        return self.record.to_mapping() | {"invoke": self.invoke}


class GovernanceLedger(Protocol):
    """Adapter implemented by V7's transactionally authoritative ledger."""

    async def action(self, action_id: str) -> ActionRecord | None: ...

    async def authorize(self, command: ActionCommand) -> Authorization: ...

    async def mark_dispatched(self, command: ActionCommand) -> DispatchAdmission: ...

    async def record_outcome(
        self, command: ActionCommand, outcome: NodeOutcome
    ) -> ActionRecord: ...

    async def record_unknown(self, command: ActionCommand, error_code: str) -> ActionRecord: ...

    async def workflow_event(
        self, run_id: str, event_type: str, payload: Mapping[str, object]
    ) -> None: ...


class PluginExecutionPort(Protocol):
    """A reviewed plugin host, normally a process/container/remote boundary."""

    async def invoke(self, command: ActionCommand, manifest: PluginManifest) -> NodeOutcome: ...

    async def reconcile(
        self, command: ActionCommand, manifest: PluginManifest
    ) -> NodeOutcome | None: ...


class GovernedActivityService:
    """Idempotent Activity implementation for Agent/HTTP/MCP/container nodes."""

    def __init__(
        self,
        ledger: GovernanceLedger,
        plugins: PluginExecutionPort,
        manifests: Mapping[tuple[str, str, str], PluginManifest],
    ) -> None:
        self._ledger = ledger
        self._plugins = plugins
        self._manifests = dict(manifests)

    async def execute(self, command: ActionCommand) -> NodeOutcome:
        """Run an Activity without treating a retry as permission to repeat I/O."""

        manifest = self._manifests.get(_plugin_key(command.plugin))
        if manifest is None:
            return NodeOutcome(
                command.action_id,
                OutcomeStatus.FAILED,
                error_code="PLUGIN_NOT_LOADED",
            )
        if manifest.kind is PluginKind.AGENT and command.agent_max_turns is None:
            return NodeOutcome(
                command.action_id,
                OutcomeStatus.FAILED,
                error_code="AGENT_TURN_LIMIT_MISSING",
            )
        if manifest.kind is not PluginKind.AGENT and command.agent_max_turns is not None:
            return NodeOutcome(
                command.action_id,
                OutcomeStatus.FAILED,
                error_code="AGENT_TURN_LIMIT_UNEXPECTED",
            )
        try:
            validate_schema_instance(manifest.input_schema, command.inputs, "action input")
        except WorkflowContractError:
            return NodeOutcome(
                command.action_id,
                OutcomeStatus.FAILED,
                error_code="INPUT_SCHEMA_INVALID",
            )
        existing = await self._ledger.action(command.action_id)
        if existing is not None:
            _same_command(existing.command, command)
            if existing.outcome is not None:
                return existing.outcome
            if existing.state in {"DISPATCHED", "UNKNOWN"}:
                return await self._reconcile_existing(command, manifest)
        authorization = await self._ledger.authorize(command)
        if not authorization.allowed:
            denied = NodeOutcome(
                command.action_id,
                OutcomeStatus.DENIED,
                error_code=authorization.reason or "AUTHORIZATION_DENIED",
            )
            if existing is not None:
                # An action that was admitted before a later revocation must
                # release its reservation instead of remaining runnable.
                await self._ledger.record_outcome(command, denied)
            return denied
        admission = await self._ledger.mark_dispatched(command)
        if not admission.invoke:
            # Another delivery has already crossed, or reserved, the external
            # boundary. Query its original identity; never issue a second call.
            return await self._reconcile_existing(command, manifest)
        return await self._invoke_new(command, manifest)

    async def _reconcile_existing(
        self, command: ActionCommand, manifest: PluginManifest
    ) -> NodeOutcome:
        try:
            recovered = await self._plugins.reconcile(command, manifest)
            if recovered is not None:
                _valid_outcome(command, recovered, manifest)
        except Exception as error:  # no second external request on uncertainty
            await self._ledger.record_unknown(command, _error_code(error))
            return _unknown_outcome(command)
        if recovered is None:
            return _unknown_outcome(command)
        await self._ledger.record_outcome(command, recovered)
        return recovered

    async def _invoke_new(self, command: ActionCommand, manifest: PluginManifest) -> NodeOutcome:
        with activity_span(command, manifest):
            try:
                outcome = await self._plugins.invoke(command, manifest)
                _valid_outcome(command, outcome, manifest)
            except Exception as error:
                await self._ledger.record_unknown(command, _error_code(error))
                return _unknown_outcome(command)
        await self._ledger.record_outcome(command, outcome)
        return outcome


class RegisteredPluginHost:
    """Explicit registry for already-reviewed plugin host adapters.

    A registration is application boot configuration, never model output.  The
    supplied handlers are intended to be RPC clients for process/container or
    remote hosts.  This registry does not expose platform ledger objects to a
    plugin handler.
    """

    def __init__(self) -> None:
        self._handlers: dict[
            tuple[str, str, str],
            tuple[
                Callable[[ActionCommand, PluginManifest], Awaitable[NodeOutcome]],
                Callable[[ActionCommand, PluginManifest], Awaitable[NodeOutcome | None]],
            ],
        ] = {}

    def register(
        self,
        manifest: PluginManifest,
        *,
        invoke: Callable[[ActionCommand, PluginManifest], Awaitable[NodeOutcome]],
        reconcile: Callable[[ActionCommand, PluginManifest], Awaitable[NodeOutcome | None]],
    ) -> None:
        key = _plugin_key(manifest.plugin)
        if key in self._handlers:
            raise GovernanceError("plugin host is already registered for this exact version")
        self._handlers[key] = (invoke, reconcile)

    async def invoke(self, command: ActionCommand, manifest: PluginManifest) -> NodeOutcome:
        handler = self._handlers.get(_plugin_key(manifest.plugin))
        if handler is None:
            raise GovernanceError("plugin host is not registered")
        return await handler[0](command, manifest)

    async def reconcile(
        self, command: ActionCommand, manifest: PluginManifest
    ) -> NodeOutcome | None:
        handler = self._handlers.get(_plugin_key(manifest.plugin))
        if handler is None:
            raise GovernanceError("plugin host is not registered")
        return await handler[1](command, manifest)


class InMemoryGovernanceLedger:
    """A concurrency-safe reference ledger used by contract and failure tests.

    Production V7 adapters make the corresponding mutations in the existing
    transaction/outbox ledger.  This implementation documents the required
    atomic invariants without becoming a second production state system.
    """

    def __init__(
        self,
        *,
        authorization_revision: int = 1,
        budget_revision: int = 1,
        release_digests: set[str] | None = None,
    ) -> None:
        self.authorization_revision = authorization_revision
        self.budget_revision = budget_revision
        self.release_digests = set(release_digests or ())
        self.revoked = False
        self._lock = asyncio.Lock()
        self._actions: dict[str, ActionRecord] = {}
        self._events: list[dict[str, object]] = []

    @property
    def events(self) -> tuple[Mapping[str, object], ...]:
        return tuple(dict(item) for item in self._events)

    async def action(self, action_id: str) -> ActionRecord | None:
        async with self._lock:
            return self._actions.get(action_id)

    async def authorize(self, command: ActionCommand) -> Authorization:
        async with self._lock:
            existing = self._actions.get(command.action_id)
            if existing is not None:
                _same_command(existing.command, command)
                if existing.state != "ADMITTED":
                    return Authorization(True)
            fence = command.fence
            if self.revoked:
                return Authorization(False, "PERMISSION_REVOKED")
            if fence.authorization_revision != self.authorization_revision:
                return Authorization(False, "AUTHORIZATION_STALE")
            if fence.budget_revision != self.budget_revision:
                return Authorization(False, "BUDGET_STALE")
            if self.release_digests and fence.release_digest not in self.release_digests:
                return Authorization(False, "RELEASE_NOT_PUBLISHED")
            self._actions[command.action_id] = ActionRecord(command=command, state="ADMITTED")
            return Authorization(True)

    async def mark_dispatched(self, command: ActionCommand) -> DispatchAdmission:
        async with self._lock:
            record = self._require(command)
            if record.outcome is not None:
                return DispatchAdmission(record, invoke=False)
            if record.state == "ADMITTED":
                updated = ActionRecord(
                    command=record.command,
                    state="DISPATCHED",
                    reservation_state=record.reservation_state,
                )
                self._actions[command.action_id] = updated
                return DispatchAdmission(updated, invoke=True)
            if record.state in {"DISPATCHED", "UNKNOWN"}:
                return DispatchAdmission(record, invoke=False)
            else:
                raise GovernanceError("action cannot be dispatched from its current state")

    async def record_outcome(self, command: ActionCommand, outcome: NodeOutcome) -> ActionRecord:
        _valid_outcome(command, outcome)
        async with self._lock:
            record = self._require(command)
            if record.outcome is not None:
                if record.outcome.to_mapping() != outcome.to_mapping():
                    raise GovernanceError(
                        "action outcome conflicts with the persisted business fact"
                    )
                return record
            updated = ActionRecord(
                command=record.command,
                state="COMPLETED",
                outcome=outcome,
                reservation_state="SETTLED"
                if outcome.status is OutcomeStatus.SUCCEEDED
                else "RELEASED",
            )
            self._actions[command.action_id] = updated
            return updated

    async def record_unknown(self, command: ActionCommand, error_code: str) -> ActionRecord:
        async with self._lock:
            record = self._require(command)
            if record.outcome is not None:
                return record
            updated = ActionRecord(
                command=record.command, state="UNKNOWN", reservation_state=record.reservation_state
            )
            self._actions[command.action_id] = updated
            self._events.append(
                {
                    "run_id": command.run_id,
                    "type": "ACTION_OUTCOME_UNKNOWN",
                    "action_id": command.action_id,
                    "error_code": error_code,
                }
            )
            return updated

    async def workflow_event(
        self, run_id: str, event_type: str, payload: Mapping[str, object]
    ) -> None:
        if contains_secret_name(payload):
            raise GovernanceError("workflow events must not contain credentials")
        async with self._lock:
            self._events.append(
                {"run_id": run_id, "type": event_type, "payload": json_safe(payload)}
            )

    def _require(self, command: ActionCommand) -> ActionRecord:
        record = self._actions.get(command.action_id)
        if record is None:
            raise GovernanceError("action must be admitted before it can be changed")
        _same_command(record.command, command)
        return record


def _plugin_key(reference: PluginReference) -> tuple[str, str, str]:
    return reference.plugin_id, reference.version, reference.package_digest


def _same_command(first: ActionCommand, second: ActionCommand) -> None:
    if canonical_json(first.to_mapping()) != canonical_json(second.to_mapping()):
        raise GovernanceError("action identity was reused with a different command")


def _valid_outcome(
    command: ActionCommand,
    outcome: NodeOutcome,
    manifest: PluginManifest | None = None,
) -> None:
    if outcome.action_id != command.action_id:
        raise GovernanceError("plugin returned an outcome for another action")
    if outcome.status is OutcomeStatus.RECONCILING:
        raise GovernanceError("plugins cannot commit a reconciling outcome")
    if outcome.status is OutcomeStatus.SUCCEEDED and manifest is not None:
        validate_schema_instance(manifest.output_schema, outcome.output or {}, "action output")


def _unknown_outcome(command: ActionCommand) -> NodeOutcome:
    return NodeOutcome(
        command.action_id,
        OutcomeStatus.RECONCILING,
        error_code="OUTCOME_UNKNOWN",
    )


def _error_code(error: Exception) -> str:
    # Do not persist arbitrary error strings: they may contain upstream data.
    name = type(error).__name__.upper()
    return "PLUGIN_" + "".join(char for char in name if char.isalnum() or char == "_")[:80]


def json_safe(value: Mapping[str, object]) -> dict[str, object]:
    return json_copy(value)  # type: ignore[return-value]
