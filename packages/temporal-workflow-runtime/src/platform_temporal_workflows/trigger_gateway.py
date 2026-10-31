"""Optional public HTTP boundary for governed V7 Temporal trigger requests.

This router intentionally never owns workflow catalogues, authorization,
business records, or a Temporal client. The supplied Platform service must
freeze the published definition, authorize the actor/webhook, persist its audit
fact and a ``TemporalDispatchCommand`` in one V7 transaction, then return that
command for a later outbox delivery.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .dispatch import TemporalDispatchCommand, TemporalDispatchKind
from .models import JsonValue, TriggerKind, WorkflowContractError, contains_secret_name

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_JSON_REQUEST_BYTES = 1_048_576
_MAX_WEBHOOK_REQUEST_BYTES = 8_388_608


class TriggerGatewayError(RuntimeError):
    """Base error a Platform trigger service may raise without leaking details."""


class TriggerAuthorizationError(TriggerGatewayError):
    """The actor or webhook is not entitled to create/control this run."""


class TriggerNotFoundError(TriggerGatewayError):
    """A published workflow, webhook, or run identity was not found."""


class TriggerConflictError(TriggerGatewayError):
    """A release/run/schedule state conflicts with this request."""


class TriggerUnavailableError(TriggerGatewayError):
    """The authoritative platform transaction service is unavailable."""


class PlatformTriggerService(Protocol):
    """Platform-owned transactional intake for public Temporal requests.

    Each method validates current permissions and release state, writes the
    authoritative run/control/audit fact plus the returned immutable command
    to the existing outbox atomically, and returns only after that commit.
    Webhook input remains raw so this service can verify the provider signature
    before interpreting or storing payload fields.
    """

    async def enqueue_start(
        self,
        *,
        actor_id: str,
        request_id: str,
        trigger: TriggerKind,
        workflow_id: str,
        version: str,
        inputs: Mapping[str, JsonValue],
    ) -> TemporalDispatchCommand: ...

    async def enqueue_webhook(
        self,
        *,
        webhook_id: str,
        headers: Mapping[str, str],
        raw_payload: bytes,
    ) -> TemporalDispatchCommand: ...

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
    ) -> TemporalDispatchCommand: ...

    async def enqueue_approval(
        self, *, actor_id: str, run_id: str, node_key: str, approved: bool
    ) -> TemporalDispatchCommand: ...

    async def enqueue_reconciliation(
        self, *, actor_id: str, run_id: str, node_key: str
    ) -> TemporalDispatchCommand: ...

    async def enqueue_cancellation(
        self, *, actor_id: str, run_id: str, reason: str
    ) -> TemporalDispatchCommand: ...


ActorResolver = Callable[[Any], str | Awaitable[str]]


def create_trigger_router(
    service: PlatformTriggerService,
    actor_resolver: ActorResolver,
    *,
    prefix: str = "/api/v1/temporal-workflows",
) -> Any:
    """Create a public API/Webhook router over one authoritative service.

    ``actor_resolver`` is injected by the existing Platform API authentication
    layer. Actor identity is never accepted in request JSON. The webhook route
    forwards raw bytes and headers to the service so an unverified payload is
    never parsed by this generic layer or written to Temporal history.
    """

    try:
        from fastapi import APIRouter, HTTPException, Request
    except ImportError as error:
        raise RuntimeError(
            "Install groundwater-temporal-workflow-runtime[api] to mount trigger routes"
        ) from error
    if not prefix.startswith("/"):
        raise WorkflowContractError("Temporal trigger route prefix must be absolute")
    # ``from __future__ import annotations`` keeps FastAPI optional at module
    # import time.  Route annotations are resolved later, however, so make the
    # lazily imported request type available in the module namespace then.
    globals()["Request"] = Request
    router = APIRouter(prefix=prefix)

    def failure(status: int, code: str) -> None:
        raise HTTPException(status_code=status, detail={"code": code})

    async def call(awaitable: Awaitable[TemporalDispatchCommand]) -> TemporalDispatchCommand:
        try:
            return await awaitable
        except TriggerAuthorizationError:
            failure(403, "TRIGGER_FORBIDDEN")
        except TriggerNotFoundError:
            failure(404, "TRIGGER_NOT_FOUND")
        except TriggerConflictError:
            failure(409, "TRIGGER_CONFLICT")
        except TriggerUnavailableError:
            failure(503, "TRIGGER_UNAVAILABLE")
        except WorkflowContractError:
            failure(422, "TRIGGER_REJECTED")
        raise AssertionError("unreachable")

    async def actor(request: Request) -> str:
        try:
            value = actor_resolver(request)
            if inspect.isawaitable(value):
                value = await value
        except (TriggerAuthorizationError, WorkflowContractError, TypeError):
            failure(401, "AUTHENTICATION_REQUIRED")
        if not _identifier(value):
            failure(401, "AUTHENTICATION_REQUIRED")
        return value

    def request_id(request: Request) -> str:
        """Require a stable caller intent key before allocating a V7 run.

        An HTTP retry after a committed platform transaction must receive the
        original frozen run/outbox record rather than create another business
        workflow. Webhook delivery identity is verified by its provider-aware
        service, while approval/reconcile/cancel dispatch identities derive
        from their already-frozen control payload.
        """

        value = request.headers.get("Idempotency-Key")
        if not _identifier(value):
            failure(400, "IDEMPOTENCY_KEY_REQUIRED")
        return value

    async def body(request: Request) -> dict[str, JsonValue]:
        raw = await request.body()
        if len(raw) > _MAX_JSON_REQUEST_BYTES:
            failure(413, "REQUEST_TOO_LARGE")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            failure(422, "INVALID_REQUEST")
        if not isinstance(value, dict):
            failure(422, "INVALID_REQUEST")
        # Deliberately reject unknown credential-shaped input before calling a
        # host service that might otherwise audit it. Credential references are
        # owned by reviewed plugin manifests, not public workflow inputs.
        if contains_secret_name(value):
            failure(422, "INVALID_REQUEST")
        return value

    @router.post("/runs/manual", status_code=202)
    async def start_manual(request: Request) -> dict[str, str]:
        payload = await body(request)
        if set(payload) != {"workflow_id", "version", "inputs"}:
            failure(422, "INVALID_REQUEST")
        workflow_id, version, inputs = _start_fields(payload, failure)
        command = await call(
            service.enqueue_start(
                actor_id=await actor(request),
                request_id=request_id(request),
                trigger=TriggerKind.MANUAL,
                workflow_id=workflow_id,
                version=version,
                inputs=inputs,
            )
        )
        return _receipt(command, TemporalDispatchKind.START, failure)

    @router.post("/runs/api", status_code=202)
    async def start_api(request: Request) -> dict[str, str]:
        payload = await body(request)
        if set(payload) != {"workflow_id", "version", "inputs"}:
            failure(422, "INVALID_REQUEST")
        workflow_id, version, inputs = _start_fields(payload, failure)
        command = await call(
            service.enqueue_start(
                actor_id=await actor(request),
                request_id=request_id(request),
                trigger=TriggerKind.API,
                workflow_id=workflow_id,
                version=version,
                inputs=inputs,
            )
        )
        return _receipt(command, TemporalDispatchKind.START, failure)

    @router.post("/webhooks/{webhook_id}", status_code=202)
    async def webhook(webhook_id: str, request: Request) -> dict[str, str]:
        if not _identifier(webhook_id):
            failure(422, "INVALID_REQUEST")
        raw_payload = await request.body()
        if len(raw_payload) > _MAX_WEBHOOK_REQUEST_BYTES:
            failure(413, "REQUEST_TOO_LARGE")
        command = await call(
            service.enqueue_webhook(
                webhook_id=webhook_id,
                headers=dict(request.headers),
                raw_payload=raw_payload,
            )
        )
        return _receipt(command, TemporalDispatchKind.START, failure)

    @router.post("/schedules", status_code=202)
    async def schedule(request: Request) -> dict[str, str]:
        payload = await body(request)
        required = {"workflow_id", "version", "inputs", "schedule_id", "every_seconds"}
        if set(payload) != required:
            failure(422, "INVALID_REQUEST")
        workflow_id, version, inputs = _start_fields(payload, failure)
        schedule_id = payload.get("schedule_id")
        every_seconds = payload.get("every_seconds")
        if not _identifier(schedule_id) or not isinstance(every_seconds, int) or every_seconds < 60:
            failure(422, "INVALID_REQUEST")
        command = await call(
            service.enqueue_schedule(
                actor_id=await actor(request),
                request_id=request_id(request),
                workflow_id=workflow_id,
                version=version,
                inputs=inputs,
                schedule_id=schedule_id,
                every_seconds=every_seconds,
            )
        )
        return _receipt(command, TemporalDispatchKind.SCHEDULE, failure)

    @router.post("/runs/{run_id}/approval", status_code=202)
    async def approval(run_id: str, request: Request) -> dict[str, str]:
        payload = await body(request)
        if set(payload) != {"node_key", "approved"}:
            failure(422, "INVALID_REQUEST")
        node_key = payload.get("node_key")
        approved = payload.get("approved")
        if not _identifier(run_id) or not _node_key(node_key) or not isinstance(approved, bool):
            failure(422, "INVALID_REQUEST")
        command = await call(
            service.enqueue_approval(
                actor_id=await actor(request),
                run_id=run_id,
                node_key=node_key,
                approved=approved,
            )
        )
        return _receipt(command, TemporalDispatchKind.APPROVE, failure)

    @router.post("/runs/{run_id}/reconcile", status_code=202)
    async def reconcile(run_id: str, request: Request) -> dict[str, str]:
        payload = await body(request)
        if set(payload) != {"node_key"} or not _identifier(run_id):
            failure(422, "INVALID_REQUEST")
        node_key = payload.get("node_key")
        if not _node_key(node_key):
            failure(422, "INVALID_REQUEST")
        command = await call(
            service.enqueue_reconciliation(
                actor_id=await actor(request), run_id=run_id, node_key=node_key
            )
        )
        return _receipt(command, TemporalDispatchKind.RECONCILE, failure)

    @router.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel(run_id: str, request: Request) -> dict[str, str]:
        payload = await body(request)
        if set(payload) != {"reason"} or not _identifier(run_id):
            failure(422, "INVALID_REQUEST")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 4_096:
            failure(422, "INVALID_REQUEST")
        command = await call(
            service.enqueue_cancellation(
                actor_id=await actor(request), run_id=run_id, reason=reason
            )
        )
        return _receipt(command, TemporalDispatchKind.CANCEL, failure)

    return router


def _start_fields(
    payload: Mapping[str, JsonValue], failure: Callable[[int, str], None]
) -> tuple[str, str, dict[str, JsonValue]]:
    if not {"workflow_id", "version", "inputs"} <= set(payload):
        failure(422, "INVALID_REQUEST")
    workflow_id = payload.get("workflow_id")
    version = payload.get("version")
    inputs = payload.get("inputs")
    if (
        not _identifier(workflow_id)
        or not _identifier(version)
        or str(version).lower() == "latest"
        or not isinstance(inputs, dict)
    ):
        failure(422, "INVALID_REQUEST")
    return workflow_id, version, dict(inputs)


def _receipt(
    command: TemporalDispatchCommand,
    expected_kind: TemporalDispatchKind,
    failure: Callable[[int, str], None],
) -> dict[str, str]:
    if command.kind is not expected_kind:
        failure(503, "TRIGGER_UNAVAILABLE")
    result = {"dispatch_id": command.dispatch_id, "kind": command.kind.value}
    if command.kind in {TemporalDispatchKind.START, TemporalDispatchKind.SCHEDULE}:
        invocation = command.payload.get("invocation")
        if not isinstance(invocation, Mapping) or not _identifier(invocation.get("run_id")):
            failure(503, "TRIGGER_UNAVAILABLE")
        result["run_id"] = str(invocation["run_id"])
    if command.kind is TemporalDispatchKind.SCHEDULE:
        schedule_id = command.payload.get("schedule_id")
        if not _identifier(schedule_id):
            failure(503, "TRIGGER_UNAVAILABLE")
        result["schedule_id"] = str(schedule_id)
    if command.kind in {
        TemporalDispatchKind.APPROVE,
        TemporalDispatchKind.RECONCILE,
        TemporalDispatchKind.CANCEL,
    }:
        run_id = command.payload.get("run_id")
        if not _identifier(run_id):
            failure(503, "TRIGGER_UNAVAILABLE")
        result["run_id"] = str(run_id)
    return result


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _node_key(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512 and "\x00" not in value
