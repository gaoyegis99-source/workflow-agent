from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("temporalio")

from temporalio.exceptions import WorkflowAlreadyStartedError

from platform_temporal_workflows import (
    GeneralWorkflowSpec,
    GovernanceFence,
    NodeKind,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    TemporalTriggerClient,
    WorkflowInvocation,
    WorkflowNode,
)

RELEASE = "sha256:" + "a" * 64
PLUGIN = PluginReference("http.client", "1", "sha256:" + "b" * 64)


def _invocation() -> WorkflowInvocation:
    manifest = PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("http.invoke",),
        execution_mode=PluginExecutionMode.REMOTE,
        timeout_seconds=10,
        max_attempts=2,
    )
    workflow = GeneralWorkflowSpec(
        "client-flow",
        "1",
        RELEASE,
        (manifest,),
        (
            WorkflowNode(
                "call",
                NodeKind.HTTP,
                {"plugin": PLUGIN.to_mapping(), "input": {}},
            ),
        ),
    )
    return WorkflowInvocation(
        "run-client",
        workflow,
        {},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
    )


class _Handle:
    def __init__(self) -> None:
        self.signals: list[tuple[object, object]] = []

    async def signal(self, name: object, payload: object) -> None:
        self.signals.append((name, payload))


class _Client:
    def __init__(self) -> None:
        self.started: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.schedules: list[tuple[str, object]] = []
        self.handle = _Handle()
        self.already_started = False

    async def start_workflow(self, *args: object, **kwargs: object) -> object:
        if self.already_started:
            raise WorkflowAlreadyStartedError(str(kwargs["id"]), "V7GeneralWorkflowV1")
        self.started.append((args, kwargs))
        return {"started": kwargs["id"]}

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        assert workflow_id == "v7-run-client"
        return self.handle

    async def create_schedule(self, schedule_id: str, schedule: object) -> object:
        self.schedules.append((schedule_id, schedule))
        return {"schedule": schedule_id}


def test_trigger_client_freezes_trigger_source_signals_and_schedule() -> None:
    async def scenario() -> None:
        client = _Client()
        trigger = TemporalTriggerClient(client)
        invocation = _invocation()

        assert await trigger.start_manual(invocation) == {"started": "v7-run-client"}
        assert await trigger.start_api(invocation) == {"started": "v7-run-client"}
        assert await trigger.start_webhook(invocation) == {"started": "v7-run-client"}
        assert [entry[0][1]["trigger"] for entry in client.started] == [
            "manual",
            "api",
            "webhook",
        ]
        await trigger.approve("run-client", node_key="review", approved=True, actor_id="reviewer")
        await trigger.reconcile("run-client", node_key="call")
        await trigger.cancel("run-client", reason="stop")
        assert client.handle.signals == [
            ("approve", {"node_key": "review", "approved": True, "actor_id": "reviewer"}),
            ("reconcile", {"node_key": "call"}),
            ("cancel", "CANCEL_REQUESTED"),
        ]
        assert await trigger.create_interval_schedule("hourly", invocation, every_seconds=60) == {
            "schedule": "hourly"
        }
        assert [entry[0] for entry in client.schedules] == ["hourly"]
        client.already_started = True
        assert await trigger.start_manual(invocation) is client.handle

    asyncio.run(scenario())
