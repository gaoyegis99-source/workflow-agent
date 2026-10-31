"""Durable local ledger for development and recovery-contract tests.

This is intentionally not V7's production ledger: production Workers use the
private HTTP gateway to the Platform's transactional action/budget/outbox
ledger.  The SQLite adapter makes the same protocol testable across a local
process restart without introducing an SDK checkpoint or a second production
state authority.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from .governance import (
    ActionRecord,
    Authorization,
    DispatchAdmission,
    GovernanceError,
)
from .models import (
    ActionCommand,
    NodeOutcome,
    OutcomeStatus,
    WorkflowContractError,
    canonical_json,
    contains_secret_name,
)


class SQLiteGovernanceLedger:
    """A small durable implementation of the governed Activity protocol.

    It stores only protocol facts required for local testing: frozen commands,
    reservation state, outcomes, and safe workflow events. It never stores
    credentials or plugin code.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        authorization_revision: int = 1,
        budget_revision: int = 1,
        release_digests: set[str] | None = None,
        revoked: bool = False,
    ) -> None:
        if authorization_revision < 1 or budget_revision < 1:
            raise WorkflowContractError("SQLite governance revisions must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(
            authorization_revision,
            budget_revision,
            release_digests or set(),
            revoked,
        )

    async def action(self, action_id: str) -> ActionRecord | None:
        return await asyncio.to_thread(self._action_sync, action_id)

    async def authorize(self, command: ActionCommand) -> Authorization:
        return await asyncio.to_thread(self._authorize_sync, command)

    async def mark_dispatched(self, command: ActionCommand) -> DispatchAdmission:
        return await asyncio.to_thread(self._mark_dispatched_sync, command)

    async def record_outcome(self, command: ActionCommand, outcome: NodeOutcome) -> ActionRecord:
        return await asyncio.to_thread(self._record_outcome_sync, command, outcome)

    async def record_unknown(self, command: ActionCommand, error_code: str) -> ActionRecord:
        return await asyncio.to_thread(self._record_unknown_sync, command, error_code)

    async def workflow_event(
        self, run_id: str, event_type: str, payload: Mapping[str, object]
    ) -> None:
        if contains_secret_name(payload):
            raise GovernanceError("workflow events must not contain credentials")
        await asyncio.to_thread(self._event_sync, run_id, event_type, dict(payload))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(
        self,
        authorization_revision: int,
        budget_revision: int,
        release_digests: set[str],
        revoked: bool,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS v7_temporal_governance_policy (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        authorization_revision INTEGER NOT NULL,
                        budget_revision INTEGER NOT NULL,
                        release_digests_json TEXT NOT NULL,
                        revoked INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS v7_temporal_governed_actions (
                        action_id TEXT PRIMARY KEY,
                        command_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        outcome_json TEXT,
                        reservation_state TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS v7_temporal_workflow_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO v7_temporal_governance_policy "
                    "(singleton, authorization_revision, budget_revision, release_digests_json, revoked) "
                    "VALUES (1, ?, ?, ?, ?)",
                    (
                        authorization_revision,
                        budget_revision,
                        canonical_json(sorted(release_digests)),
                        int(revoked),
                    ),
                )
        except sqlite3.Error as error:
            raise GovernanceError("could not initialize SQLite governance ledger") from error

    def _action_sync(self, action_id: str) -> ActionRecord | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT command_json, state, outcome_json, reservation_state "
                    "FROM v7_temporal_governed_actions WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                return _record_from_row(row) if row is not None else None
        except sqlite3.Error as error:
            raise GovernanceError("could not read SQLite governance ledger") from error

    def _authorize_sync(self, command: ActionCommand) -> Authorization:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = _action_row(connection, command.action_id)
                if row is not None:
                    record = _record_from_row(row)
                    _same_command(record.command, command)
                    if record.state != "ADMITTED":
                        connection.execute("COMMIT")
                        return Authorization(True)
                authorization = _policy_authorization(connection, command)
                if authorization.allowed and row is None:
                    connection.execute(
                        "INSERT INTO v7_temporal_governed_actions "
                        "(action_id, command_json, state, outcome_json, reservation_state) "
                        "VALUES (?, ?, 'ADMITTED', NULL, 'RESERVED')",
                        (command.action_id, canonical_json(command.to_mapping())),
                    )
                connection.execute("COMMIT")
                return authorization
        except GovernanceError:
            raise
        except sqlite3.Error as error:
            raise GovernanceError("could not authorize SQLite governed action") from error

    def _mark_dispatched_sync(self, command: ActionCommand) -> DispatchAdmission:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                record = _require_record(connection, command)
                if record.outcome is not None:
                    connection.execute("COMMIT")
                    return DispatchAdmission(record, invoke=False)
                if record.state == "ADMITTED":
                    connection.execute(
                        "UPDATE v7_temporal_governed_actions SET state = 'DISPATCHED' "
                        "WHERE action_id = ?",
                        (command.action_id,),
                    )
                    updated = ActionRecord(
                        command=record.command,
                        state="DISPATCHED",
                        reservation_state=record.reservation_state,
                    )
                    connection.execute("COMMIT")
                    return DispatchAdmission(updated, invoke=True)
                if record.state in {"DISPATCHED", "UNKNOWN"}:
                    connection.execute("COMMIT")
                    return DispatchAdmission(record, invoke=False)
                raise GovernanceError("action cannot be dispatched from its current state")
        except GovernanceError:
            raise
        except sqlite3.Error as error:
            raise GovernanceError("could not dispatch SQLite governed action") from error

    def _record_outcome_sync(self, command: ActionCommand, outcome: NodeOutcome) -> ActionRecord:
        _terminal_outcome(command, outcome)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                record = _require_record(connection, command)
                if record.outcome is not None:
                    if record.outcome.to_mapping() != outcome.to_mapping():
                        raise GovernanceError(
                            "action outcome conflicts with the persisted business fact"
                        )
                    connection.execute("COMMIT")
                    return record
                reservation = "SETTLED" if outcome.status is OutcomeStatus.SUCCEEDED else "RELEASED"
                connection.execute(
                    "UPDATE v7_temporal_governed_actions "
                    "SET state = 'COMPLETED', outcome_json = ?, reservation_state = ? "
                    "WHERE action_id = ?",
                    (canonical_json(outcome.to_mapping()), reservation, command.action_id),
                )
                updated = ActionRecord(
                    command=record.command,
                    state="COMPLETED",
                    outcome=outcome,
                    reservation_state=reservation,
                )
                connection.execute("COMMIT")
                return updated
        except GovernanceError:
            raise
        except sqlite3.Error as error:
            raise GovernanceError("could not record SQLite action outcome") from error

    def _record_unknown_sync(self, command: ActionCommand, error_code: str) -> ActionRecord:
        if not error_code:
            raise GovernanceError("unknown outcome requires a stable error code")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                record = _require_record(connection, command)
                if record.outcome is not None:
                    connection.execute("COMMIT")
                    return record
                connection.execute(
                    "UPDATE v7_temporal_governed_actions SET state = 'UNKNOWN' WHERE action_id = ?",
                    (command.action_id,),
                )
                connection.execute(
                    "INSERT INTO v7_temporal_workflow_events(run_id, event_type, payload_json) "
                    "VALUES (?, ?, ?)",
                    (
                        command.run_id,
                        "ACTION_OUTCOME_UNKNOWN",
                        canonical_json({"action_id": command.action_id, "error_code": error_code}),
                    ),
                )
                updated = ActionRecord(
                    command=record.command,
                    state="UNKNOWN",
                    reservation_state=record.reservation_state,
                )
                connection.execute("COMMIT")
                return updated
        except GovernanceError:
            raise
        except sqlite3.Error as error:
            raise GovernanceError("could not record SQLite unknown outcome") from error

    def _event_sync(self, run_id: str, event_type: str, payload: Mapping[str, object]) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO v7_temporal_workflow_events(run_id, event_type, payload_json) "
                    "VALUES (?, ?, ?)",
                    (run_id, event_type, canonical_json(payload)),
                )
        except sqlite3.Error as error:
            raise GovernanceError("could not record SQLite workflow event") from error


def _action_row(connection: sqlite3.Connection, action_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT command_json, state, outcome_json, reservation_state "
        "FROM v7_temporal_governed_actions WHERE action_id = ?",
        (action_id,),
    ).fetchone()


def _require_record(connection: sqlite3.Connection, command: ActionCommand) -> ActionRecord:
    row = _action_row(connection, command.action_id)
    if row is None:
        raise GovernanceError("action must be admitted before it can be changed")
    record = _record_from_row(row)
    _same_command(record.command, command)
    return record


def _record_from_row(row: sqlite3.Row) -> ActionRecord:
    try:
        command = ActionCommand.from_mapping(json.loads(row["command_json"]))
        outcome = (
            NodeOutcome.from_mapping(json.loads(row["outcome_json"]))
            if row["outcome_json"] is not None
            else None
        )
    except (json.JSONDecodeError, WorkflowContractError) as error:
        raise GovernanceError("SQLite governance record is malformed") from error
    return ActionRecord(
        command=command,
        state=row["state"],
        outcome=outcome,
        reservation_state=row["reservation_state"],
    )


def _policy_authorization(connection: sqlite3.Connection, command: ActionCommand) -> Authorization:
    row = connection.execute(
        "SELECT authorization_revision, budget_revision, release_digests_json, revoked "
        "FROM v7_temporal_governance_policy WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise GovernanceError("SQLite governance policy is missing")
    try:
        releases = set(json.loads(row["release_digests_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise GovernanceError("SQLite governance policy is malformed") from error
    if row["revoked"]:
        return Authorization(False, "PERMISSION_REVOKED")
    if command.fence.authorization_revision != row["authorization_revision"]:
        return Authorization(False, "AUTHORIZATION_STALE")
    if command.fence.budget_revision != row["budget_revision"]:
        return Authorization(False, "BUDGET_STALE")
    if releases and command.fence.release_digest not in releases:
        return Authorization(False, "RELEASE_NOT_PUBLISHED")
    return Authorization(True)


def _same_command(first: ActionCommand, second: ActionCommand) -> None:
    if canonical_json(first.to_mapping()) != canonical_json(second.to_mapping()):
        raise GovernanceError("action identity was reused with a different command")


def _terminal_outcome(command: ActionCommand, outcome: NodeOutcome) -> None:
    if outcome.action_id != command.action_id:
        raise GovernanceError("plugin returned an outcome for another action")
    if outcome.status is OutcomeStatus.RECONCILING:
        raise GovernanceError("plugins cannot commit a reconciling outcome")
