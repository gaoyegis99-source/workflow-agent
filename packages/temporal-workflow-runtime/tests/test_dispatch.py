from __future__ import annotations

import asyncio

import pytest

from platform_temporal_workflows import (
    GeneralWorkflowSpec,
    GovernanceFence,
    NodeKind,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    TemporalDispatchCommand,
    TemporalOutboxDispatcher,
    TriggerKind,
    WorkflowContractError,
    WorkflowInvocation,
    WorkflowNode,
)

RELEASE = "sha256:" + "a" * 64
PLUGIN = PluginReference("dispatch.echo", "1", "sha256:" + "b" * 64)


def _invocation(trigger: TriggerKind) -> WorkflowInvocation:
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
    return WorkflowInvocation(
        "dispatch-run",
        GeneralWorkflowSpec(
            "dispatch-flow",
            "1",
            RELEASE,
            (manifest,),
            (WorkflowNode("call", NodeKind.HTTP, {"plugin": PLUGIN.to_mapping(), "input": {}}),),
        ),
        {},
        GovernanceFence("project", "task", "actor", 1, 1, RELEASE),
        trigger=trigger,
    )


class _TriggerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def start_manual(self, invocation: object) -> None:
        self.calls.append(("manual", (invocation,), {}))

    async def start_api(self, invocation: object) -> None:
        self.calls.append(("api", (invocation,), {}))

    async def start_webhook(self, invocation: object) -> None:
        self.calls.append(("webhook", (invocation,), {}))

    async def create_interval_schedule(
        self,
        schedule_id: str,
        invocation: object,
        *,
        every_seconds: int,
        dispatch_id: str | None = None,
    ) -> None:
        self.calls.append(
            (
                "schedule",
                (schedule_id, invocation),
                {"every_seconds": every_seconds, "dispatch_id": dispatch_id},
            )
        )

    async def approve(self, run_id: str, *, node_key: str, approved: bool, actor_id: str) -> None:
        self.calls.append(
            (
                "approve",
                (run_id,),
                {"node_key": node_key, "approved": approved, "actor_id": actor_id},
            )
        )

    async def reconcile(self, run_id: str, *, node_key: str) -> None:
        self.calls.append(("reconcile", (run_id,), {"node_key": node_key}))

    async def cancel(self, run_id: str, *, reason: str) -> None:
        self.calls.append(("cancel", (run_id,), {"reason": reason}))


def test_outbox_dispatch_replays_exact_frozen_commands() -> None:
    async def scenario() -> None:
        client = _TriggerClient()
        dispatcher = TemporalOutboxDispatcher(client)  # type: ignore[arg-type]
        commands = (
            TemporalDispatchCommand.start(_invocation(TriggerKind.MANUAL)),
            TemporalDispatchCommand.start(_invocation(TriggerKind.API)),
            TemporalDispatchCommand.start(_invocation(TriggerKind.WEBHOOK)),
            TemporalDispatchCommand.schedule(
                "hourly", _invocation(TriggerKind.SCHEDULE), every_seconds=60
            ),
            TemporalDispatchCommand.approve(
                "dispatch-run", node_key="review", approved=True, actor_id="reviewer"
            ),
            TemporalDispatchCommand.reconcile("dispatch-run", node_key="call"),
            TemporalDispatchCommand.cancel("dispatch-run"),
        )
        for command in commands:
            assert TemporalDispatchCommand.from_mapping(command.to_mapping()) == command
            await dispatcher.dispatch(command)
        assert [item[0] for item in client.calls] == [
            "manual",
            "api",
            "webhook",
            "schedule",
            "approve",
            "reconcile",
            "cancel",
        ]
        assert client.calls[-1][2] == {"reason": "CANCEL_REQUESTED"}
        assert client.calls[3][2]["dispatch_id"] == commands[3].dispatch_id

    asyncio.run(scenario())


def test_dispatch_identity_rejects_tampering_and_secret_payloads() -> None:
    command = TemporalDispatchCommand.approve(
        "dispatch-run", node_key="review", approved=True, actor_id="reviewer"
    )
    tampered = command.to_mapping() | {
        "payload": command.to_mapping()["payload"] | {"approved": False}
    }
    with pytest.raises(WorkflowContractError, match="identity"):
        TemporalDispatchCommand.from_mapping(tampered)
    with pytest.raises(WorkflowContractError, match="sensitive"):
        TemporalDispatchCommand(
            "td-" + "a" * 48,
            "cancel",
            {"run_id": "dispatch-run", "token": "not-allowed"},
        )
