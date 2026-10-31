from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from platform_temporal_workflows import (
    GeneralWorkflowSpec,
    NodeKind,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    PublishedWorkflow,
    SQLiteDevelopmentWorkflowPlatform,
    TemporalDispatchKind,
    TriggerConflictError,
    TriggerKind,
    VerifiedWebhook,
    WorkflowNode,
    create_trigger_router,
)

RELEASE = "sha256:" + "1" * 64
PLUGIN = PluginReference("development.echo", "1", "sha256:" + "2" * 64)


def _published() -> PublishedWorkflow:
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
    workflow = GeneralWorkflowSpec(
        "development-flow",
        "1",
        RELEASE,
        (manifest,),
        (WorkflowNode("call", NodeKind.HTTP, {"plugin": PLUGIN.to_mapping(), "input": {}}),),
    )
    return PublishedWorkflow(
        workflow,
        "project",
        "task",
        allowed_actors=frozenset({"operator", "reviewer"}),
    )


def _run_id(command) -> str:  # type: ignore[no-untyped-def]
    return str(command.payload["invocation"]["run_id"])


def test_local_platform_atomically_freezes_a_start_and_leases_the_same_outbox_command(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        path = tmp_path / "development.sqlite"
        first = SQLiteDevelopmentWorkflowPlatform(path, [_published()])
        command = await first.enqueue_start(
            actor_id="operator",
            request_id="request-one",
            trigger=TriggerKind.MANUAL,
            workflow_id="development-flow",
            version="1",
            inputs={"message": "hello"},
        )
        run_id = _run_id(command)
        # A fresh object represents API/process restart. It returns exactly the
        # committed command rather than allocating another run/outbox record.
        restarted = SQLiteDevelopmentWorkflowPlatform(path, [_published()])
        replay = await restarted.enqueue_start(
            actor_id="operator",
            request_id="request-one",
            trigger=TriggerKind.MANUAL,
            workflow_id="development-flow",
            version="1",
            inputs={"message": "hello"},
        )
        leases = await restarted.claim_outbox("worker-a")
        assert replay == command
        assert len(leases) == 1
        assert leases[0].command == command
        assert leases[0].command.kind is TemporalDispatchKind.START

        # A delivery failure leaves the immutable command pending; a later
        # lease has the same id/payload but a new generation.
        await restarted.release_outbox(leases[0], error_code="TEMPORARY_FAILURE")
        after_failure = SQLiteDevelopmentWorkflowPlatform(path, [_published()])
        retry = (await after_failure.claim_outbox("worker-b"))[0]
        assert retry.command == command
        assert retry.generation > leases[0].generation
        await after_failure.acknowledge_outbox(retry)
        assert await after_failure.claim_outbox("worker-c") == ()
        events = await after_failure.events(run_id)
        assert events == (
            {
                "sequence": 1,
                "event_type": "WORKFLOW_TRIGGERED",
                "payload": {
                    "trigger": "manual",
                    "dispatch_id": command.dispatch_id,
                    "workflow_id": "development-flow",
                    "version": "1",
                },
            },
        )

        with pytest.raises(TriggerConflictError, match="idempotency"):
            await after_failure.enqueue_start(
                actor_id="operator",
                request_id="request-one",
                trigger=TriggerKind.MANUAL,
                workflow_id="development-flow",
                version="1",
                inputs={"message": "different"},
            )

    asyncio.run(scenario())


def test_local_platform_deduplicates_verified_webhooks_and_keeps_control_audit_safe(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    class Verifier:
        seen: list[tuple[str, dict[str, str], bytes]] = []

        def verify(self, *, webhook_id, headers, raw_payload):  # type: ignore[no-untyped-def]
            self.seen.append((webhook_id, dict(headers), raw_payload))
            assert headers["x-provider-signature"] == "verified"
            return VerifiedWebhook(
                "delivery-1", "development-flow", "1", "operator", {"webhook": True}
            )

    async def scenario() -> None:
        verifier = Verifier()
        platform = SQLiteDevelopmentWorkflowPlatform(
            tmp_path / "development.sqlite", [_published()], webhook_verifier=verifier
        )
        first = await platform.enqueue_webhook(
            webhook_id="provider-hook",
            headers={"x-provider-signature": "verified"},
            raw_payload=b'{"untrusted":"raw"}',
        )
        replay = await platform.enqueue_webhook(
            webhook_id="provider-hook",
            headers={"x-provider-signature": "verified"},
            raw_payload=b'{"untrusted":"raw"}',
        )
        assert first == replay
        assert len(verifier.seen) == 2
        run_id = _run_id(first)

        approval = await platform.enqueue_approval(
            actor_id="reviewer", run_id=run_id, node_key="review", approved=True
        )
        assert approval == await platform.enqueue_approval(
            actor_id="reviewer", run_id=run_id, node_key="review", approved=True
        )
        with pytest.raises(TriggerConflictError, match="already decided"):
            await platform.enqueue_approval(
                actor_id="reviewer", run_id=run_id, node_key="review", approved=False
            )
        cancel = await platform.enqueue_cancellation(
            actor_id="operator", run_id=run_id, reason="api_key=never-persist-this"
        )
        assert cancel.kind is TemporalDispatchKind.CANCEL
        audit = json.dumps(await platform.events(run_id), sort_keys=True)
        assert "never-persist-this" not in audit
        assert "api_key" not in audit

    asyncio.run(scenario())


def test_local_platform_rejects_conflicting_schedule_identity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        platform = SQLiteDevelopmentWorkflowPlatform(
            tmp_path / "development.sqlite", [_published()]
        )
        command = await platform.enqueue_schedule(
            actor_id="operator",
            request_id="schedule-request",
            workflow_id="development-flow",
            version="1",
            inputs={},
            schedule_id="hourly",
            every_seconds=60,
        )
        assert command.kind is TemporalDispatchKind.SCHEDULE
        with pytest.raises(TriggerConflictError, match="schedule"):
            await platform.enqueue_schedule(
                actor_id="operator",
                request_id="other-request",
                workflow_id="development-flow",
                version="1",
                inputs={},
                schedule_id="hourly",
                every_seconds=60,
            )

    asyncio.run(scenario())


def test_public_trigger_router_and_durable_platform_share_the_same_idempotent_intake(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    platform = SQLiteDevelopmentWorkflowPlatform(tmp_path / "development.sqlite", [_published()])

    async def actor(_: Request) -> str:
        return "operator"

    app = FastAPI()
    app.include_router(create_trigger_router(platform, actor))
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "public-request"}
        body = {"workflow_id": "development-flow", "version": "1", "inputs": {"x": 1}}
        first = client.post("/api/v1/temporal-workflows/runs/manual", headers=headers, json=body)
        replay = client.post("/api/v1/temporal-workflows/runs/manual", headers=headers, json=body)
        conflict = client.post(
            "/api/v1/temporal-workflows/runs/manual",
            headers=headers,
            json=body | {"inputs": {"x": 2}},
        )
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    assert (
        asyncio.run(platform.claim_outbox("worker"))[0].command.dispatch_id
        == first.json()["dispatch_id"]
    )
