"""Private ASGI router implementing the V7 Temporal governance contract.

The V7 Platform mounts this router on a private listener and supplies the
transactional ledger adapter that owns its business facts.  The Temporal
worker never receives that adapter or a database connection; it uses
``HttpGovernanceLedger`` instead.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from typing import Any

from .governance import GovernanceError, GovernanceLedger
from .models import ActionCommand, NodeOutcome, WorkflowContractError

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def create_governance_router(ledger: GovernanceLedger, service_token: str) -> Any:
    """Create an unaudited-by-default private router for a supplied V7 ledger.

    FastAPI remains an optional platform-edge dependency: importing the worker
    package does not import it.  ``service_token`` must come from the Platform
    composition layer, never a workflow definition or Temporal payload.
    """

    if not service_token:
        raise WorkflowContractError("Temporal governance service token is required")
    try:
        from fastapi import APIRouter, Header, HTTPException
    except ImportError as error:  # pragma: no cover - exercised by platform install
        raise RuntimeError("Install FastAPI to mount the V7 Temporal governance router") from error

    router = APIRouter(prefix="/internal/temporal/v1", include_in_schema=False)

    def authorize(authorization: str | None) -> None:
        expected = "Bearer " + service_token
        if not hmac.compare_digest(authorization or "", expected):
            raise HTTPException(status_code=401, detail="service credential required")

    def command(payload: object) -> ActionCommand:
        try:
            return ActionCommand.from_mapping(payload)
        except WorkflowContractError as error:
            raise HTTPException(status_code=422, detail="invalid governed action") from error

    def outcome(payload: object) -> NodeOutcome:
        try:
            return NodeOutcome.from_mapping(payload)
        except WorkflowContractError as error:
            raise HTTPException(
                status_code=422, detail="invalid governed action outcome"
            ) from error

    def mapping(payload: object, label: str) -> Mapping[str, object]:
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=422, detail=f"{label} must be an object")
        return payload

    def failure(error: GovernanceError) -> Any:
        raise HTTPException(status_code=409, detail="governance state conflict") from error

    @router.get("/actions/{action_id}")
    async def get_action(
        action_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        if _IDENTIFIER.fullmatch(action_id) is None:
            raise HTTPException(status_code=422, detail="invalid action identifier")
        try:
            record = await ledger.action(action_id)
        except GovernanceError as error:
            failure(error)
        if record is None:
            raise HTTPException(status_code=404, detail="action not found")
        return record.to_mapping()

    @router.post("/actions/authorize")
    async def authorize_action(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return (await ledger.authorize(command(payload))).to_mapping()
        except GovernanceError as error:
            failure(error)

    @router.post("/actions/dispatch")
    async def dispatch_action(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        try:
            return (await ledger.mark_dispatched(command(payload))).to_mapping()
        except GovernanceError as error:
            failure(error)

    @router.post("/actions/outcome")
    async def record_outcome(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        body = mapping(payload, "outcome request")
        if set(body) != {"command", "outcome"}:
            raise HTTPException(status_code=422, detail="invalid outcome request")
        try:
            record = await ledger.record_outcome(command(body["command"]), outcome(body["outcome"]))
            return record.to_mapping()
        except GovernanceError as error:
            failure(error)

    @router.post("/actions/unknown")
    async def record_unknown(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(authorization)
        body = mapping(payload, "unknown-outcome request")
        error_code = body.get("error_code")
        if set(body) != {"command", "error_code"} or (
            not isinstance(error_code, str) or _IDENTIFIER.fullmatch(error_code) is None
        ):
            raise HTTPException(status_code=422, detail="invalid unknown-outcome request")
        try:
            record = await ledger.record_unknown(command(body["command"]), error_code)
            return record.to_mapping()
        except GovernanceError as error:
            failure(error)

    @router.post("/events")
    async def record_event(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> None:
        authorize(authorization)
        body = mapping(payload, "workflow event")
        run_id = body.get("run_id")
        event_type = body.get("event_type")
        event_payload = body.get("payload")
        if (
            set(body) != {"run_id", "event_type", "payload"}
            or not isinstance(run_id, str)
            or _IDENTIFIER.fullmatch(run_id) is None
            or not isinstance(event_type, str)
            or _IDENTIFIER.fullmatch(event_type) is None
            or not isinstance(event_payload, Mapping)
        ):
            raise HTTPException(status_code=422, detail="invalid workflow event")
        try:
            await ledger.workflow_event(run_id, event_type, dict(event_payload))
        except GovernanceError as error:
            failure(error)

    return router
