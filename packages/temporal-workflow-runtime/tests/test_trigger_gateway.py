from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from platform_temporal_workflows import (
    GeneralWorkflowSpec,
    GovernanceFence,
    NodeKind,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    TemporalDispatchCommand,
    TriggerAuthorizationError,
    TriggerConflictError,
    TriggerKind,
    WorkflowInvocation,
    WorkflowNode,
    create_trigger_router,
)

RELEASE = "sha256:" + "c" * 64
PLUGIN = PluginReference("gateway.echo", "1", "sha256:" + "d" * 64)


def _invocation(run_id: str, trigger: TriggerKind) -> WorkflowInvocation:
    manifest = PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("remote.call",),
        execution_mode=PluginExecutionMode.REMOTE,
        timeout_seconds=10,
        max_attempts=2,
    )
    spec = GeneralWorkflowSpec(
        "gateway-flow",
        "1",
        RELEASE,
        (manifest,),
        (WorkflowNode("call", NodeKind.HTTP, {"plugin": PLUGIN.to_mapping(), "input": {}}),),
    )
    return WorkflowInvocation(
        run_id,
        spec,
        {},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
        trigger=trigger,
    )


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail: Exception | None = None

    def _record(self, method: str, **kwargs: object) -> None:
        if self.fail is not None:
            raise self.fail
        self.calls.append((method, kwargs))

    async def enqueue_start(self, **kwargs: object) -> TemporalDispatchCommand:
        self._record("start", **kwargs)
        return TemporalDispatchCommand.start(_invocation("run-start", kwargs["trigger"]))  # type: ignore[arg-type]

    async def enqueue_webhook(self, **kwargs: object) -> TemporalDispatchCommand:
        self._record("webhook", **kwargs)
        return TemporalDispatchCommand.start(_invocation("run-webhook", TriggerKind.WEBHOOK))

    async def enqueue_schedule(self, **kwargs: object) -> TemporalDispatchCommand:
        self._record("schedule", **kwargs)
        return TemporalDispatchCommand.schedule(
            str(kwargs["schedule_id"]),
            _invocation("run-schedule", TriggerKind.SCHEDULE),
            every_seconds=int(kwargs["every_seconds"]),
        )

    async def enqueue_approval(self, **kwargs: object) -> TemporalDispatchCommand:
        self._record("approval", **kwargs)
        return TemporalDispatchCommand.approve(
            str(kwargs["run_id"]),
            node_key=str(kwargs["node_key"]),
            approved=bool(kwargs["approved"]),
            actor_id=str(kwargs["actor_id"]),
        )

    async def enqueue_reconciliation(self, **kwargs: object) -> TemporalDispatchCommand:
        self._record("reconcile", **kwargs)
        return TemporalDispatchCommand.reconcile(
            str(kwargs["run_id"]), node_key=str(kwargs["node_key"])
        )

    async def enqueue_cancellation(self, **kwargs: object) -> TemporalDispatchCommand:
        self._record("cancel", **kwargs)
        return TemporalDispatchCommand.cancel(str(kwargs["run_id"]))


def _app(service: _Service) -> FastAPI:
    async def actor(request: Request) -> str:
        value = request.headers.get("X-Actor")
        if value is None:
            raise TriggerAuthorizationError("missing user")
        return value

    app = FastAPI()
    app.include_router(create_trigger_router(service, actor))
    return app


def test_trigger_gateway_queues_all_public_trigger_types_without_temporal_client() -> None:
    service = _Service()
    with TestClient(_app(service)) as client:
        headers = {"X-Actor": "operator", "Idempotency-Key": "manual-request"}
        manual = client.post(
            "/api/v1/temporal-workflows/runs/manual",
            headers=headers,
            json={"workflow_id": "gateway-flow", "version": "1", "inputs": {"x": 1}},
        )
        api = client.post(
            "/api/v1/temporal-workflows/runs/api",
            headers={"X-Actor": "operator", "Idempotency-Key": "api-request"},
            json={"workflow_id": "gateway-flow", "version": "1", "inputs": {}},
        )
        webhook = client.post(
            "/api/v1/temporal-workflows/webhooks/provider-hook",
            headers={"X-Signature": "opaque", "Content-Type": "application/octet-stream"},
            content=b'{"not": "parsed-by-gateway"}',
        )
        schedule = client.post(
            "/api/v1/temporal-workflows/schedules",
            headers={"X-Actor": "operator", "Idempotency-Key": "schedule-request"},
            json={
                "workflow_id": "gateway-flow",
                "version": "1",
                "inputs": {},
                "schedule_id": "hourly",
                "every_seconds": 60,
            },
        )
        approval = client.post(
            "/api/v1/temporal-workflows/runs/run-1/approval",
            headers=headers,
            json={"node_key": "review", "approved": True},
        )
        reconcile = client.post(
            "/api/v1/temporal-workflows/runs/run-1/reconcile",
            headers=headers,
            json={"node_key": "call"},
        )
        cancellation = client.post(
            "/api/v1/temporal-workflows/runs/run-1/cancel",
            headers=headers,
            json={"reason": "operator requested"},
        )

    assert [
        response.status_code
        for response in (manual, api, webhook, schedule, approval, reconcile, cancellation)
    ] == [202] * 7
    assert manual.json()["run_id"] == "run-start"
    assert schedule.json() == {
        "dispatch_id": schedule.json()["dispatch_id"],
        "kind": "schedule",
        "run_id": "run-schedule",
        "schedule_id": "hourly",
    }
    assert [method for method, _ in service.calls] == [
        "start",
        "start",
        "webhook",
        "schedule",
        "approval",
        "reconcile",
        "cancel",
    ]
    webhook_call = service.calls[2][1]
    assert webhook_call["raw_payload"] == b'{"not": "parsed-by-gateway"}'
    assert webhook_call["headers"] and webhook_call["headers"]["x-signature"] == "opaque"  # type: ignore[index]
    assert service.calls[0][1]["actor_id"] == "operator"
    assert service.calls[0][1]["request_id"] == "manual-request"
    assert service.calls[0][1]["trigger"] is TriggerKind.MANUAL


def test_trigger_gateway_rejects_untrusted_identity_unknown_fields_secrets_and_latest() -> None:
    service = _Service()
    with TestClient(_app(service)) as client:
        url = "/api/v1/temporal-workflows/runs/manual"
        base = {"workflow_id": "gateway-flow", "version": "1", "inputs": {}}
        assert client.post(url, headers={"Idempotency-Key": "request"}, json=base).json() == {
            "detail": {"code": "AUTHENTICATION_REQUIRED"}
        }
        headers = {"X-Actor": "operator", "Idempotency-Key": "request"}
        assert client.post(url, headers=headers, json=base | {"actor_id": "spoof"}).json() == {
            "detail": {"code": "INVALID_REQUEST"}
        }
        assert client.post(
            url, headers=headers, json=base | {"inputs": {"token": "secret-value"}}
        ).json() == {"detail": {"code": "INVALID_REQUEST"}}
        assert client.post(url, headers=headers, json=base | {"version": "latest"}).json() == {
            "detail": {"code": "INVALID_REQUEST"}
        }
        assert client.post(url, headers={"X-Actor": "operator"}, json=base).json() == {
            "detail": {"code": "IDEMPOTENCY_KEY_REQUIRED"}
        }
    assert service.calls == []


def test_trigger_gateway_returns_stable_errors_without_echoing_service_details() -> None:
    service = _Service()
    service.fail = TriggerConflictError("credential=never-echo")
    with TestClient(_app(service)) as client:
        response = client.post(
            "/api/v1/temporal-workflows/runs/manual",
            headers={"X-Actor": "operator", "Idempotency-Key": "conflict-request"},
            json={"workflow_id": "gateway-flow", "version": "1", "inputs": {}},
        )
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "TRIGGER_CONFLICT"}}
    assert "credential" not in response.text
