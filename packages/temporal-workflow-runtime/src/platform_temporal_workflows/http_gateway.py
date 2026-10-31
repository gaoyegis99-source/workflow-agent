"""Private HTTP adapter for the V7 ledger-owned Temporal gateway contract.

The worker never writes V7 database tables.  It calls authenticated internal
endpoints, which perform the existing transaction/outbox changes atomically.
Credentials are read from a mounted file in the worker process and never enter
workflow input, Temporal history, events, or trace attributes.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib import error, request

from .governance import (
    ActionRecord,
    Authorization,
    DispatchAdmission,
    GovernanceError,
    GovernanceLedger,
)
from .models import ActionCommand, NodeOutcome, WorkflowContractError


class HttpGovernanceLedger:
    """Async adapter for `/internal/temporal/v1`; unknown failures fail closed."""

    def __init__(self, base_url: str, bearer_token: str, *, timeout_seconds: float = 10) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise WorkflowContractError("V7 Temporal governance URL must be HTTP(S)")
        if not bearer_token:
            raise WorkflowContractError("V7 Temporal governance token is required")
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token
        self._timeout_seconds = timeout_seconds

    async def action(self, action_id: str) -> ActionRecord | None:
        status, payload = await self._request("GET", f"/internal/temporal/v1/actions/{action_id}")
        if status == 404:
            return None
        self._success(status)
        return _action_record(payload)

    async def authorize(self, command: ActionCommand) -> Authorization:
        status, payload = await self._request(
            "POST", "/internal/temporal/v1/actions/authorize", command.to_mapping()
        )
        self._success(status)
        return _authorization(payload)

    async def mark_dispatched(self, command: ActionCommand) -> DispatchAdmission:
        status, payload = await self._request(
            "POST", "/internal/temporal/v1/actions/dispatch", command.to_mapping()
        )
        self._success(status)
        return _dispatch_admission(payload)

    async def record_outcome(self, command: ActionCommand, outcome: NodeOutcome) -> ActionRecord:
        status, payload = await self._request(
            "POST",
            "/internal/temporal/v1/actions/outcome",
            {"command": command.to_mapping(), "outcome": outcome.to_mapping()},
        )
        self._success(status)
        return _action_record(payload)

    async def record_unknown(self, command: ActionCommand, error_code: str) -> ActionRecord:
        status, payload = await self._request(
            "POST",
            "/internal/temporal/v1/actions/unknown",
            {"command": command.to_mapping(), "error_code": error_code},
        )
        self._success(status)
        return _action_record(payload)

    async def workflow_event(
        self, run_id: str, event_type: str, payload: Mapping[str, object]
    ) -> None:
        status, _ = await self._request(
            "POST",
            "/internal/temporal/v1/events",
            {"run_id": run_id, "event_type": event_type, "payload": dict(payload)},
        )
        self._success(status)

    async def _request(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> tuple[int, Mapping[str, object]]:
        return await asyncio.to_thread(self._request_sync, method, path, payload)

    def _request_sync(
        self, method: str, path: str, payload: Mapping[str, object] | None
    ) -> tuple[int, Mapping[str, object]]:
        data = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        outbound = request.Request(self._base_url + path, data=data, headers=headers, method=method)
        try:
            with request.urlopen(outbound, timeout=self._timeout_seconds) as response:
                raw = response.read()
                return response.status, _payload(raw)
        except error.HTTPError as response:
            return response.code, _payload(response.read())
        except (OSError, ValueError) as exc:
            raise GovernanceError("V7 ledger gateway is unavailable") from exc

    @staticmethod
    def _success(status: int) -> None:
        if not 200 <= status < 300:
            raise GovernanceError(f"V7 ledger gateway refused the operation ({status})")


def http_ledger_from_environment() -> GovernanceLedger:
    """Worker factory for a mounted single-machine secret token file."""

    base_url = os.environ.get("V7_TEMPORAL_GOVERNANCE_URL")
    token_file = os.environ.get("V7_TEMPORAL_GOVERNANCE_TOKEN_FILE")
    if not base_url or not token_file:
        raise WorkflowContractError(
            "V7_TEMPORAL_GOVERNANCE_URL and V7_TEMPORAL_GOVERNANCE_TOKEN_FILE are required"
        )
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkflowContractError("could not read the V7 Temporal governance token file") from exc
    return HttpGovernanceLedger(base_url, token)


def _payload(raw: bytes) -> Mapping[str, object]:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernanceError("V7 ledger gateway returned malformed JSON") from exc
    if not isinstance(value, Mapping):
        raise GovernanceError("V7 ledger gateway returned an invalid response")
    return dict(value)


def _authorization(value: Mapping[str, object]) -> Authorization:
    allowed = value.get("allowed")
    reason = value.get("reason")
    if not isinstance(allowed, bool) or (reason is not None and not isinstance(reason, str)):
        raise GovernanceError("V7 ledger gateway returned an invalid authorization")
    return Authorization(allowed, reason)


def _action_record(value: Mapping[str, object]) -> ActionRecord:
    command = value.get("command")
    state = value.get("state")
    outcome = value.get("outcome")
    reservation_state = value.get("reservation_state")
    if (
        not isinstance(command, Mapping)
        or not isinstance(state, str)
        or not isinstance(reservation_state, str)
    ):
        raise GovernanceError("V7 ledger gateway returned an invalid action record")
    return ActionRecord(
        command=ActionCommand.from_mapping(command),
        state=state,
        outcome=NodeOutcome.from_mapping(outcome) if isinstance(outcome, Mapping) else None,
        reservation_state=reservation_state,
    )


def _dispatch_admission(value: Mapping[str, object]) -> DispatchAdmission:
    invoke = value.get("invoke")
    if not isinstance(invoke, bool):
        raise GovernanceError("V7 ledger gateway returned an invalid dispatch admission")
    return DispatchAdmission(_action_record(value), invoke)
