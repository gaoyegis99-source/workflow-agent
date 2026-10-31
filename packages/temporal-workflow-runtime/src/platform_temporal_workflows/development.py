"""Durable single-machine reference for the V7 trigger/outbox boundary.

This module is deliberately a development/reference composition, not a second
production ledger.  Production Platform code supplies ``PlatformTriggerService``
over its existing transaction, action, budget, and outbox facts.  The SQLite
implementation makes the *same atomicity contract* executable in local V7
tests: a public trigger creates a frozen invocation, audit fact, and immutable
Temporal dispatch command in one transaction; an outbox worker leases and
acknowledges that exact command later.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .dispatch import TemporalDispatchCommand
from .models import (
    GeneralWorkflowSpec,
    GovernanceFence,
    JsonValue,
    TriggerKind,
    WorkflowContractError,
    WorkflowInvocation,
    canonical_json,
    contains_secret_name,
    digest,
)
from .trigger_gateway import (
    TriggerAuthorizationError,
    TriggerConflictError,
    TriggerGatewayError,
    TriggerNotFoundError,
    TriggerUnavailableError,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise WorkflowContractError(f"{label} must be a portable identifier")
    return value


@dataclass(frozen=True)
class PublishedWorkflow:
    """A boot-time reviewed workflow release available to a local platform.

    The production equivalent comes from the V7 release catalogue.  Keeping
    this object explicit prevents a trigger request from supplying a project,
    task, release digest, or permission/budget revision of its own.
    """

    workflow: GeneralWorkflowSpec
    project_id: str
    task_id: str
    authorization_revision: int = 1
    budget_revision: int = 1
    allowed_actors: frozenset[str] = frozenset()
    task_queue: str = "v7-general-workflows"

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, GeneralWorkflowSpec):
            raise WorkflowContractError("published workflow must be a frozen workflow spec")
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project_id"))
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        object.__setattr__(self, "task_queue", _identifier(self.task_queue, "task_queue"))
        for name in ("authorization_revision", "budget_revision"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise WorkflowContractError(f"published workflow {name} must be positive")
        actors = frozenset(_identifier(item, "allowed actor") for item in self.allowed_actors)
        object.__setattr__(self, "allowed_actors", actors)


@dataclass(frozen=True)
class VerifiedWebhook:
    """Provider-aware verification result; raw body is never stored here."""

    delivery_id: str
    workflow_id: str
    version: str
    actor_id: str
    inputs: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "delivery_id", _identifier(self.delivery_id, "webhook delivery_id")
        )
        object.__setattr__(self, "workflow_id", _identifier(self.workflow_id, "workflow_id"))
        version = _identifier(self.version, "workflow version")
        if version.lower() == "latest":
            raise WorkflowContractError("webhook workflow version must be fixed")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "actor_id", _identifier(self.actor_id, "webhook actor_id"))
        if contains_secret_name(self.inputs):
            raise WorkflowContractError("webhook inputs must use credential references")
        copied = json.loads(canonical_json(dict(self.inputs)))
        if not isinstance(copied, dict):
            raise WorkflowContractError("webhook inputs must be an object")
        object.__setattr__(self, "inputs", copied)


class WebhookVerifier(Protocol):
    """A deployment-specific verifier that owns provider signatures/delivery IDs."""

    def verify(
        self, *, webhook_id: str, headers: Mapping[str, str], raw_payload: bytes
    ) -> VerifiedWebhook | Awaitable[VerifiedWebhook]: ...


@dataclass(frozen=True)
class OutboxLease:
    """One durable command claimed by an outbox delivery worker."""

    dispatch_id: str
    command: TemporalDispatchCommand
    owner: str
    generation: int


class SQLiteDevelopmentWorkflowPlatform:
    """A local ``PlatformTriggerService`` plus leased immutable outbox.

    It is appropriate for one-machine development/demo and failure-contract
    tests.  It has no plugin host and no production authorization database;
    production must use the existing V7 transactional adapter instead.
    """

    def __init__(
        self,
        path: str | Path,
        published_workflows: Sequence[PublishedWorkflow],
        *,
        webhook_verifier: WebhookVerifier | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        items = tuple(published_workflows)
        self._published = {
            (item.workflow.workflow_id, item.workflow.version): item for item in items
        }
        if len(items) != len(self._published):
            raise WorkflowContractError("published workflows must have unique fixed versions")
        self._webhook_verifier = webhook_verifier
        self._initialize()

    async def enqueue_start(
        self,
        *,
        actor_id: str,
        request_id: str,
        trigger: TriggerKind,
        workflow_id: str,
        version: str,
        inputs: Mapping[str, JsonValue],
    ) -> TemporalDispatchCommand:
        if trigger not in {TriggerKind.MANUAL, TriggerKind.API}:
            raise WorkflowContractError("start trigger must be manual or API")
        return await asyncio.to_thread(
            self._start_sync,
            actor_id,
            request_id,
            trigger,
            workflow_id,
            version,
            inputs,
            f"start:{trigger.value}:{actor_id}",
        )

    async def enqueue_webhook(
        self,
        *,
        webhook_id: str,
        headers: Mapping[str, str],
        raw_payload: bytes,
    ) -> TemporalDispatchCommand:
        _identifier(webhook_id, "webhook_id")
        verifier = self._webhook_verifier
        if verifier is None:
            raise TriggerUnavailableError("webhook verifier is not configured")
        try:
            verified = verifier.verify(
                webhook_id=webhook_id, headers=dict(headers), raw_payload=raw_payload
            )
            if inspect.isawaitable(verified):
                verified = await verified
        except TriggerGatewayError:
            raise
        except Exception as error:
            raise TriggerAuthorizationError("webhook authentication failed") from error
        if not isinstance(verified, VerifiedWebhook):
            raise TriggerAuthorizationError("webhook verifier returned an invalid result")
        return await asyncio.to_thread(
            self._start_sync,
            verified.actor_id,
            verified.delivery_id,
            TriggerKind.WEBHOOK,
            verified.workflow_id,
            verified.version,
            verified.inputs,
            f"webhook:{webhook_id}",
        )

    async def enqueue_schedule(
        self,
        *,
        actor_id: str,
        request_id: str,
        workflow_id: str,
        version: str,
        inputs: Mapping[str, JsonValue],
        schedule_id: str,
        every_seconds: int,
    ) -> TemporalDispatchCommand:
        return await asyncio.to_thread(
            self._schedule_sync,
            actor_id,
            request_id,
            workflow_id,
            version,
            inputs,
            schedule_id,
            every_seconds,
        )

    async def enqueue_approval(
        self, *, actor_id: str, run_id: str, node_key: str, approved: bool
    ) -> TemporalDispatchCommand:
        if not isinstance(approved, bool):
            raise WorkflowContractError("approval decision must be boolean")
        return await asyncio.to_thread(
            self._control_sync,
            "approval",
            actor_id,
            run_id,
            node_key,
            TemporalDispatchCommand.approve(
                run_id, node_key=node_key, approved=approved, actor_id=actor_id
            ),
            {"approved": approved, "actor_id": actor_id},
        )

    async def enqueue_reconciliation(
        self, *, actor_id: str, run_id: str, node_key: str
    ) -> TemporalDispatchCommand:
        return await asyncio.to_thread(
            self._control_sync,
            "reconcile",
            actor_id,
            run_id,
            node_key,
            TemporalDispatchCommand.reconcile(run_id, node_key=node_key),
            {},
        )

    async def enqueue_cancellation(
        self, *, actor_id: str, run_id: str, reason: str
    ) -> TemporalDispatchCommand:
        if not isinstance(reason, str) or not reason:
            raise WorkflowContractError("cancellation reason is required")
        # A generic dev audit can prove a reason was supplied without copying
        # free text (or accidental credentials) into a portable/runtime store.
        return await asyncio.to_thread(
            self._control_sync,
            "cancel",
            actor_id,
            run_id,
            "cancel",
            TemporalDispatchCommand.cancel(run_id),
            {"reason_digest": digest({"reason": reason})},
        )

    async def claim_outbox(
        self, owner: str, *, limit: int = 32, lease_seconds: int = 30
    ) -> tuple[OutboxLease, ...]:
        return await asyncio.to_thread(self._claim_sync, owner, limit, lease_seconds)

    async def acknowledge_outbox(self, lease: OutboxLease) -> None:
        await asyncio.to_thread(self._acknowledge_sync, lease)

    async def release_outbox(self, lease: OutboxLease, *, error_code: str) -> None:
        await asyncio.to_thread(self._release_sync, lease, error_code)

    async def events(self, run_id: str) -> tuple[Mapping[str, object], ...]:
        return await asyncio.to_thread(self._events_sync, run_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS v7_temporal_development_runs (
                        run_id TEXT PRIMARY KEY,
                        invocation_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS v7_temporal_development_idempotency (
                        scope TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        dispatch_id TEXT NOT NULL,
                        PRIMARY KEY (scope, request_id)
                    );
                    CREATE TABLE IF NOT EXISTS v7_temporal_development_schedules (
                        schedule_id TEXT PRIMARY KEY,
                        dispatch_id TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS v7_temporal_development_controls (
                        run_id TEXT NOT NULL REFERENCES v7_temporal_development_runs(run_id),
                        node_key TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        dispatch_id TEXT NOT NULL,
                        PRIMARY KEY (run_id, node_key, kind)
                    );
                    CREATE TABLE IF NOT EXISTS v7_temporal_development_outbox (
                        dispatch_id TEXT PRIMARY KEY,
                        command_json TEXT NOT NULL,
                        done INTEGER NOT NULL DEFAULT 0,
                        owner TEXT,
                        lease_until REAL,
                        generation INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error_code TEXT
                    );
                    CREATE TABLE IF NOT EXISTS v7_temporal_development_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES v7_temporal_development_runs(run_id),
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        UNIQUE (run_id, event_type, payload_json)
                    );
                    CREATE INDEX IF NOT EXISTS v7_temporal_development_outbox_ready
                    ON v7_temporal_development_outbox(done, lease_until, dispatch_id);
                    """
                )
        except sqlite3.Error as error:
            raise TriggerUnavailableError(
                "could not initialize development workflow state"
            ) from error

    def _start_sync(
        self,
        actor_id: str,
        request_id: str,
        trigger: TriggerKind,
        workflow_id: str,
        version: str,
        inputs: Mapping[str, JsonValue],
        scope: str,
    ) -> TemporalDispatchCommand:
        actor_id = _identifier(actor_id, "actor_id")
        request_id = _identifier(request_id, "request_id")
        published = self._published_workflow(workflow_id, version, actor_id)
        invocation = WorkflowInvocation(
            run_id=_run_id(scope, request_id, workflow_id, version, inputs),
            workflow=published.workflow,
            inputs=inputs,
            fence=GovernanceFence(
                published.project_id,
                published.task_id,
                actor_id,
                published.authorization_revision,
                published.budget_revision,
                published.workflow.release_digest,
            ),
            trigger=trigger,
            task_queue=published.task_queue,
        )
        command = TemporalDispatchCommand.start(invocation)
        fingerprint = digest(
            {
                "kind": "start",
                "workflow_id": workflow_id,
                "version": version,
                "trigger": trigger.value,
                "actor_id": actor_id,
                "inputs": dict(inputs),
            }
        )
        return self._write_start(scope, request_id, fingerprint, invocation, command)

    def _schedule_sync(
        self,
        actor_id: str,
        request_id: str,
        workflow_id: str,
        version: str,
        inputs: Mapping[str, JsonValue],
        schedule_id: str,
        every_seconds: int,
    ) -> TemporalDispatchCommand:
        actor_id = _identifier(actor_id, "actor_id")
        request_id = _identifier(request_id, "request_id")
        schedule_id = _identifier(schedule_id, "schedule_id")
        if (
            not isinstance(every_seconds, int)
            or isinstance(every_seconds, bool)
            or every_seconds < 60
        ):
            raise WorkflowContractError("schedule interval must be at least 60 seconds")
        published = self._published_workflow(workflow_id, version, actor_id)
        scope = f"schedule:{actor_id}"
        invocation = WorkflowInvocation(
            run_id=_run_id(scope, request_id, workflow_id, version, inputs),
            workflow=published.workflow,
            inputs=inputs,
            fence=GovernanceFence(
                published.project_id,
                published.task_id,
                actor_id,
                published.authorization_revision,
                published.budget_revision,
                published.workflow.release_digest,
            ),
            trigger=TriggerKind.SCHEDULE,
            task_queue=published.task_queue,
        )
        command = TemporalDispatchCommand.schedule(
            schedule_id, invocation, every_seconds=every_seconds
        )
        fingerprint = digest(
            {
                "kind": "schedule",
                "workflow_id": workflow_id,
                "version": version,
                "actor_id": actor_id,
                "inputs": dict(inputs),
                "schedule_id": schedule_id,
                "every_seconds": every_seconds,
            }
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = _idempotent_command(connection, scope, request_id, fingerprint)
                if existing is not None:
                    connection.execute("COMMIT")
                    return existing
                scheduled = connection.execute(
                    "SELECT dispatch_id FROM v7_temporal_development_schedules WHERE schedule_id = ?",
                    (schedule_id,),
                ).fetchone()
                if scheduled is not None:
                    connection.execute("ROLLBACK")
                    raise TriggerConflictError("schedule is already bound")
                _insert_run(connection, invocation)
                _insert_outbox(connection, command)
                connection.execute(
                    "INSERT INTO v7_temporal_development_idempotency "
                    "(scope, request_id, fingerprint, dispatch_id) VALUES (?, ?, ?, ?)",
                    (scope, request_id, fingerprint, command.dispatch_id),
                )
                connection.execute(
                    "INSERT INTO v7_temporal_development_schedules (schedule_id, dispatch_id) VALUES (?, ?)",
                    (schedule_id, command.dispatch_id),
                )
                _event(
                    connection,
                    invocation.run_id,
                    "SCHEDULE_REQUESTED",
                    {"schedule_id": schedule_id, "dispatch_id": command.dispatch_id},
                )
                connection.execute("COMMIT")
                return command
        except TriggerGatewayError:
            raise
        except sqlite3.Error as error:
            raise TriggerUnavailableError("could not persist schedule trigger") from error

    def _write_start(
        self,
        scope: str,
        request_id: str,
        fingerprint: str,
        invocation: WorkflowInvocation,
        command: TemporalDispatchCommand,
    ) -> TemporalDispatchCommand:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = _idempotent_command(connection, scope, request_id, fingerprint)
                if existing is not None:
                    connection.execute("COMMIT")
                    return existing
                _insert_run(connection, invocation)
                _insert_outbox(connection, command)
                connection.execute(
                    "INSERT INTO v7_temporal_development_idempotency "
                    "(scope, request_id, fingerprint, dispatch_id) VALUES (?, ?, ?, ?)",
                    (scope, request_id, fingerprint, command.dispatch_id),
                )
                _event(
                    connection,
                    invocation.run_id,
                    "WORKFLOW_TRIGGERED",
                    {
                        "trigger": invocation.trigger.value,
                        "dispatch_id": command.dispatch_id,
                        "workflow_id": invocation.workflow.workflow_id,
                        "version": invocation.workflow.version,
                    },
                )
                connection.execute("COMMIT")
                return command
        except TriggerGatewayError:
            raise
        except sqlite3.Error as error:
            raise TriggerUnavailableError("could not persist workflow trigger") from error

    def _control_sync(
        self,
        kind: str,
        actor_id: str,
        run_id: str,
        node_key: str,
        command: TemporalDispatchCommand,
        event_payload: Mapping[str, object],
    ) -> TemporalDispatchCommand:
        actor_id = _identifier(actor_id, "actor_id")
        run_id = _identifier(run_id, "run_id")
        if (
            not isinstance(node_key, str)
            or not node_key
            or len(node_key) > 512
            or "\x00" in node_key
        ):
            raise WorkflowContractError("node_key is invalid")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                invocation = _run(connection, run_id)
                published = self._published_workflow(
                    invocation.workflow.workflow_id, invocation.workflow.version, actor_id
                )
                del published
                existing = connection.execute(
                    "SELECT dispatch_id FROM v7_temporal_development_controls "
                    "WHERE run_id = ? AND node_key = ? AND kind = ?",
                    (run_id, node_key, kind),
                ).fetchone()
                if existing is not None:
                    saved = _outbox_command(connection, str(existing["dispatch_id"]))
                    if saved.to_mapping() != command.to_mapping():
                        connection.execute("ROLLBACK")
                        raise TriggerConflictError("workflow control was already decided")
                    connection.execute("COMMIT")
                    return saved
                _insert_outbox(connection, command)
                connection.execute(
                    "INSERT INTO v7_temporal_development_controls "
                    "(run_id, node_key, kind, dispatch_id) VALUES (?, ?, ?, ?)",
                    (run_id, node_key, kind, command.dispatch_id),
                )
                _event(
                    connection,
                    run_id,
                    f"{kind.upper()}_REQUESTED",
                    {"node_key": node_key, "dispatch_id": command.dispatch_id, **event_payload},
                )
                connection.execute("COMMIT")
                return command
        except TriggerGatewayError:
            raise
        except sqlite3.Error as error:
            raise TriggerUnavailableError("could not persist workflow control") from error

    def _published_workflow(
        self, workflow_id: str, version: str, actor_id: str
    ) -> PublishedWorkflow:
        workflow_id = _identifier(workflow_id, "workflow_id")
        version = _identifier(version, "workflow version")
        if version.lower() == "latest":
            raise WorkflowContractError("workflow version must be fixed")
        actor_id = _identifier(actor_id, "actor_id")
        published = self._published.get((workflow_id, version))
        if published is None:
            raise TriggerNotFoundError("published workflow is unavailable")
        if published.allowed_actors and actor_id not in published.allowed_actors:
            raise TriggerAuthorizationError("actor may not operate this workflow")
        return published

    def _claim_sync(self, owner: str, limit: int, lease_seconds: int) -> tuple[OutboxLease, ...]:
        owner = _identifier(owner, "outbox owner")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 128:
            raise WorkflowContractError("outbox claim limit must be between 1 and 128")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3_600
        ):
            raise WorkflowContractError("outbox lease_seconds must be between 1 and 3600")
        now = time.time()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT dispatch_id FROM v7_temporal_development_outbox "
                    "WHERE done = 0 AND (lease_until IS NULL OR lease_until <= ?) "
                    "ORDER BY dispatch_id LIMIT ?",
                    (now, limit),
                ).fetchall()
                leases: list[OutboxLease] = []
                for row in rows:
                    dispatch_id = str(row["dispatch_id"])
                    result = connection.execute(
                        "UPDATE v7_temporal_development_outbox "
                        "SET owner = ?, lease_until = ?, generation = generation + 1, attempts = attempts + 1 "
                        "WHERE dispatch_id = ? AND done = 0 "
                        "AND (lease_until IS NULL OR lease_until <= ?)",
                        (owner, now + lease_seconds, dispatch_id, now),
                    )
                    if result.rowcount != 1:
                        continue
                    leased = connection.execute(
                        "SELECT command_json, generation FROM v7_temporal_development_outbox "
                        "WHERE dispatch_id = ?",
                        (dispatch_id,),
                    ).fetchone()
                    if leased is None:
                        raise TriggerUnavailableError("outbox lease disappeared")
                    leases.append(
                        OutboxLease(
                            dispatch_id,
                            _command_from_json(str(leased["command_json"])),
                            owner,
                            int(leased["generation"]),
                        )
                    )
                connection.execute("COMMIT")
                return tuple(leases)
        except TriggerGatewayError:
            raise
        except sqlite3.Error as error:
            raise TriggerUnavailableError("could not lease development outbox") from error

    def _acknowledge_sync(self, lease: OutboxLease) -> None:
        self._finish_lease(lease, done=True, error_code=None)

    def _release_sync(self, lease: OutboxLease, error_code: str) -> None:
        if not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None:
            raise WorkflowContractError("outbox error_code is invalid")
        self._finish_lease(lease, done=False, error_code=error_code)

    def _finish_lease(self, lease: OutboxLease, *, done: bool, error_code: str | None) -> None:
        if not isinstance(lease, OutboxLease):
            raise WorkflowContractError("outbox lease is invalid")
        try:
            with self._connect() as connection:
                result = connection.execute(
                    "UPDATE v7_temporal_development_outbox "
                    "SET done = ?, owner = NULL, lease_until = NULL, last_error_code = ? "
                    "WHERE dispatch_id = ? AND owner = ? AND generation = ? AND done = 0",
                    (int(done), error_code, lease.dispatch_id, lease.owner, lease.generation),
                )
                if result.rowcount != 1:
                    raise TriggerConflictError("outbox lease is no longer current")
        except TriggerGatewayError:
            raise
        except sqlite3.Error as error:
            raise TriggerUnavailableError("could not update development outbox") from error

    def _events_sync(self, run_id: str) -> tuple[Mapping[str, object], ...]:
        run_id = _identifier(run_id, "run_id")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT sequence, event_type, payload_json FROM v7_temporal_development_events "
                    "WHERE run_id = ? ORDER BY sequence",
                    (run_id,),
                ).fetchall()
        except sqlite3.Error as error:
            raise TriggerUnavailableError("could not read development workflow events") from error
        return tuple(
            {
                "sequence": int(row["sequence"]),
                "event_type": str(row["event_type"]),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        )


def _run_id(
    scope: str,
    request_id: str,
    workflow_id: str,
    version: str,
    inputs: Mapping[str, JsonValue],
) -> str:
    raw = canonical_json(
        {
            "scope": scope,
            "request_id": request_id,
            "workflow_id": workflow_id,
            "version": version,
            "inputs": dict(inputs),
        }
    ).encode()
    return "run-" + hashlib.sha256(raw).hexdigest()[:52]


def _idempotent_command(
    connection: sqlite3.Connection, scope: str, request_id: str, fingerprint: str
) -> TemporalDispatchCommand | None:
    row = connection.execute(
        "SELECT fingerprint, dispatch_id FROM v7_temporal_development_idempotency "
        "WHERE scope = ? AND request_id = ?",
        (scope, request_id),
    ).fetchone()
    if row is None:
        return None
    if str(row["fingerprint"]) != fingerprint:
        raise TriggerConflictError("idempotency key was reused with another request")
    return _outbox_command(connection, str(row["dispatch_id"]))


def _insert_run(connection: sqlite3.Connection, invocation: WorkflowInvocation) -> None:
    existing = connection.execute(
        "SELECT invocation_json FROM v7_temporal_development_runs WHERE run_id = ?",
        (invocation.run_id,),
    ).fetchone()
    encoded = canonical_json(invocation.to_mapping())
    if existing is not None:
        if str(existing["invocation_json"]) != encoded:
            raise TriggerConflictError("run identity is already bound to another invocation")
        return
    connection.execute(
        "INSERT INTO v7_temporal_development_runs (run_id, invocation_json) VALUES (?, ?)",
        (invocation.run_id, encoded),
    )


def _run(connection: sqlite3.Connection, run_id: str) -> WorkflowInvocation:
    row = connection.execute(
        "SELECT invocation_json FROM v7_temporal_development_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise TriggerNotFoundError("workflow run is unavailable")
    try:
        return WorkflowInvocation.from_mapping(json.loads(str(row["invocation_json"])))
    except (json.JSONDecodeError, WorkflowContractError) as error:
        raise TriggerUnavailableError("persisted development run is malformed") from error


def _insert_outbox(connection: sqlite3.Connection, command: TemporalDispatchCommand) -> None:
    encoded = canonical_json(command.to_mapping())
    existing = connection.execute(
        "SELECT command_json FROM v7_temporal_development_outbox WHERE dispatch_id = ?",
        (command.dispatch_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["command_json"]) != encoded:
            raise TriggerConflictError("dispatch identity is already bound to another command")
        return
    connection.execute(
        "INSERT INTO v7_temporal_development_outbox (dispatch_id, command_json) VALUES (?, ?)",
        (command.dispatch_id, encoded),
    )


def _outbox_command(connection: sqlite3.Connection, dispatch_id: str) -> TemporalDispatchCommand:
    row = connection.execute(
        "SELECT command_json FROM v7_temporal_development_outbox WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchone()
    if row is None:
        raise TriggerUnavailableError("persistent dispatch command is unavailable")
    return _command_from_json(str(row["command_json"]))


def _command_from_json(value: str) -> TemporalDispatchCommand:
    try:
        return TemporalDispatchCommand.from_mapping(json.loads(value))
    except (json.JSONDecodeError, WorkflowContractError) as error:
        raise TriggerUnavailableError("persistent dispatch command is malformed") from error


def _event(
    connection: sqlite3.Connection, run_id: str, event_type: str, payload: Mapping[str, object]
) -> None:
    if contains_secret_name(payload):
        raise TriggerUnavailableError("development workflow event would contain a credential")
    connection.execute(
        "INSERT OR IGNORE INTO v7_temporal_development_events "
        "(run_id, event_type, payload_json) VALUES (?, ?, ?)",
        (run_id, event_type, canonical_json(dict(payload))),
    )
