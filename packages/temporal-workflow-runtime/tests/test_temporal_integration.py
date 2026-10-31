from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("temporalio")

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from platform_temporal_workflows import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceFence,
    GovernedActivityService,
    InMemoryGovernanceLedger,
    NodeKind,
    NodeOutcome,
    OutcomeStatus,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    RegisteredPluginHost,
    SQLiteGovernanceLedger,
    WorkflowInvocation,
    WorkflowNode,
)
from platform_temporal_workflows.activities import V7TemporalActivities
from platform_temporal_workflows.temporal_runtime import GeneralWorkflow

RELEASE = "sha256:" + "e" * 64
CHILD_RELEASE = "sha256:" + "d" * 64
PLUGIN = PluginReference("http.echo", "1", "sha256:" + "f" * 64)
AGENT_PLUGIN = PluginReference("agent.planner", "1", "sha256:" + "c" * 64)


def _invocation(*, approval: bool) -> WorkflowInvocation:
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
    nodes = [
        WorkflowNode(
            "call", NodeKind.HTTP, {"plugin": PLUGIN.to_mapping(), "input": {"message": "hello"}}
        )
    ]
    if approval:
        nodes.append(WorkflowNode("review", NodeKind.APPROVAL, {"prompt": "approve the result"}))
    return WorkflowInvocation(
        "run-approval" if approval else "run-basic",
        GeneralWorkflowSpec("temporal-demo", "1", RELEASE, (manifest,), tuple(nodes)),
        {},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
        task_queue="v7-temporal-test",
    )


def _activities(ledger: InMemoryGovernanceLedger) -> V7TemporalActivities:
    manifest = _invocation(approval=False).workflow.plugins[0]
    host = RegisteredPluginHost()

    async def invoke(command, registered):  # type: ignore[no-untyped-def]
        return NodeOutcome(
            command.action_id, OutcomeStatus.SUCCEEDED, output={"echo": command.inputs}
        )

    async def reconcile(command, registered):  # type: ignore[no-untyped-def]
        return None

    host.register(manifest, invoke=invoke, reconcile=reconcile)
    service = GovernedActivityService(
        ledger,
        host,
        {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
    )
    return V7TemporalActivities(service, ledger)


def _control_flow_invocation() -> WorkflowInvocation:
    manifest = _invocation(approval=False).workflow.plugins[0]
    child = GeneralWorkflowSpec(
        "child-flow",
        "1",
        CHILD_RELEASE,
        (manifest,),
        (
            WorkflowNode(
                "child-call",
                NodeKind.HTTP,
                {"plugin": PLUGIN.to_mapping(), "input": {"child": True}},
            ),
        ),
    )
    root = GeneralWorkflowSpec(
        "control-flow",
        "1",
        RELEASE,
        (manifest,),
        (
            WorkflowNode(
                "fanout",
                NodeKind.PARALLEL,
                {},
                (
                    (
                        WorkflowNode(
                            "left",
                            NodeKind.HTTP,
                            {"plugin": PLUGIN.to_mapping(), "input": {"side": "left"}},
                        ),
                    ),
                    (WorkflowNode("right", NodeKind.TIMER, {"seconds": 5}),),
                ),
            ),
            WorkflowNode(
                "route",
                NodeKind.CONDITION,
                {"path": "/fanout~10~1left/echo/side", "equals": "left"},
                (
                    (WorkflowNode("selected", NodeKind.TIMER, {"seconds": 1}),),
                    (WorkflowNode("unexpected", NodeKind.TIMER, {"seconds": 1}),),
                ),
            ),
            WorkflowNode(
                "iterate",
                NodeKind.LOOP,
                {"items_path": "/items", "max_iterations": 2},
                (
                    (
                        WorkflowNode(
                            "each",
                            NodeKind.HTTP,
                            {
                                "plugin": PLUGIN.to_mapping(),
                                "input": {"value": {"$item": True}},
                            },
                        ),
                    ),
                ),
            ),
            WorkflowNode(
                "child",
                NodeKind.SUBWORKFLOW,
                {"workflow_id": "child-flow", "version": "1"},
            ),
        ),
    )
    return WorkflowInvocation(
        "run-control",
        root,
        {"items": ["one", "two"]},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
        task_queue="v7-temporal-control-test",
        subworkflows=(child,),
    )


def test_temporal_history_replay_preserves_approval_wait_and_action_identity() -> None:
    async def scenario() -> None:
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        activities = _activities(ledger)
        invocation = _invocation(approval=True)
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue=invocation.task_queue,
                workflows=[GeneralWorkflow],
                activities=[activities.execute_governed_node, activities.record_workflow_event],
            ):
                handle = await environment.client.start_workflow(
                    GeneralWorkflow.run,
                    invocation.to_mapping(),
                    id="approval-worker-restart",
                    task_queue=invocation.task_queue,
                )
                for _ in range(50):
                    if any(event["type"] == "APPROVAL_REQUESTED" for event in ledger.events):
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("workflow did not reach durable approval wait")
                # Replayer is the SDK's process-independent worker-restart
                # check: completed Activities are read from history and the
                # workflow must deterministically return to the same wait.
                replay = await Replayer(workflows=[GeneralWorkflow]).replay_workflow(
                    await handle.fetch_history(), raise_on_replay_failure=False
                )
                assert replay.replay_failure is None
                await handle.signal(
                    GeneralWorkflow.approve,
                    {"node_key": "review", "approved": True, "actor_id": "reviewer"},
                )
                result = await handle.result()
        assert result["status"] == "SUCCEEDED"
        assert result["outputs"]["review"] == {"approved": True, "actor_id": "reviewer"}
        command = ActionCommand.create(invocation, "call", PLUGIN, {"message": "hello"})
        record = await ledger.action(command.action_id)
        assert record is not None and record.reservation_state == "SETTLED"

    asyncio.run(scenario())


def _event_was_persisted(path: Path, run_id: str, event_type: str) -> bool:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM v7_temporal_workflow_events WHERE run_id = ? AND event_type = ? LIMIT 1",
            (run_id, event_type),
        ).fetchone()
    return row is not None


def test_temporal_worker_kill_while_waiting_for_approval_preserves_the_original_action(
    tmp_path: Path,
) -> None:
    """A SIGKILLed Worker is replaced without repeating its action Activity."""

    async def scenario() -> None:
        invocation = _invocation(approval=True)
        command = ActionCommand.create(invocation, "call", PLUGIN, {"message": "hello"})
        ledger_path = tmp_path / "restart-governance.sqlite"
        ledger = SQLiteGovernanceLedger(ledger_path, release_digests={RELEASE})
        fixture = Path(__file__).with_name("fixtures") / "restart_worker.py"
        child: subprocess.Popen[bytes] | None = None
        try:
            async with await WorkflowEnvironment.start_time_skipping() as environment:
                target_host = environment.client.service_client.config.target_host
                handle = await environment.client.start_workflow(
                    GeneralWorkflow.run,
                    invocation.to_mapping(),
                    id="approval-real-worker-kill",
                    task_queue=invocation.task_queue,
                )
                child = subprocess.Popen(
                    [
                        sys.executable,
                        str(fixture),
                        target_host,
                        invocation.task_queue,
                        str(ledger_path),
                        RELEASE,
                        PLUGIN.plugin_id,
                        PLUGIN.version,
                        PLUGIN.package_digest,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(100):
                    if _event_was_persisted(ledger_path, invocation.run_id, "APPROVAL_REQUESTED"):
                        break
                    if child.poll() is not None:
                        raise AssertionError("restart Worker exited before approval was durable")
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("workflow did not reach durable approval wait")

                # This is intentionally not Worker.shutdown(): an OS kill
                # loses memory without giving the runtime an eviction signal.
                child.kill()
                child.wait(timeout=5)
                child = None

                replacement_ledger = SQLiteGovernanceLedger(ledger_path, release_digests={RELEASE})
                replacement_host = RegisteredPluginHost()
                replacement_invocations = 0

                async def invoke(replayed, manifest):  # type: ignore[no-untyped-def]
                    nonlocal replacement_invocations
                    del manifest
                    replacement_invocations += 1
                    return NodeOutcome(
                        replayed.action_id,
                        OutcomeStatus.SUCCEEDED,
                        output={"unexpected": True},
                    )

                async def reconcile(replayed, manifest):  # type: ignore[no-untyped-def]
                    del replayed, manifest
                    return None

                manifest = invocation.workflow.plugins[0]
                replacement_host.register(manifest, invoke=invoke, reconcile=reconcile)
                replacement_service = GovernedActivityService(
                    replacement_ledger,
                    replacement_host,
                    {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
                )
                replacement_activities = V7TemporalActivities(
                    replacement_service, replacement_ledger
                )
                async with Worker(
                    environment.client,
                    task_queue=invocation.task_queue,
                    identity="v7-restart-test-worker",
                    workflows=[GeneralWorkflow],
                    activities=[
                        replacement_activities.execute_governed_node,
                        replacement_activities.record_workflow_event,
                    ],
                ):
                    await handle.signal(
                        GeneralWorkflow.approve,
                        {"node_key": "review", "approved": True, "actor_id": "reviewer"},
                    )
                    # A SIGKILL can leave a closed long-poll visible to the
                    # test server briefly before the replacement poller owns
                    # the replay task.  Keep the assertion bounded while
                    # allowing that real process-loss handoff to settle.
                    result = await asyncio.wait_for(handle.result(), timeout=30)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)

        persisted = await ledger.action(command.action_id)
        assert result["status"] == "SUCCEEDED"
        assert persisted is not None and persisted.reservation_state == "SETTLED"
        assert replacement_invocations == 0

    asyncio.run(scenario())


def test_temporal_runs_parallel_timer_condition_loop_and_versioned_child_workflow() -> None:
    async def scenario() -> None:
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE, CHILD_RELEASE})
        activities = _activities(ledger)
        invocation = _control_flow_invocation()
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue=invocation.task_queue,
                workflows=[GeneralWorkflow],
                activities=[activities.execute_governed_node, activities.record_workflow_event],
            ):
                handle = await environment.client.start_workflow(
                    GeneralWorkflow.run,
                    invocation.to_mapping(),
                    id="control-flow-workflow",
                    task_queue=invocation.task_queue,
                )
                result = await handle.result()
        assert result["status"] == "SUCCEEDED"
        outputs = result["outputs"]
        assert outputs["fanout/0/left"] == {"echo": {"side": "left"}}
        assert outputs["route"]["branch"] == "then"
        assert outputs["iterate"]["iterations"] == [
            {"iterate/0/each": {"echo": {"value": "one"}}},
            {"iterate/1/each": {"echo": {"value": "two"}}},
        ]
        assert outputs["child"]["outputs"]["child-call"] == {"echo": {"child": True}}

    asyncio.run(scenario())


def test_temporal_agent_node_passes_the_frozen_turn_limit_to_its_plugin() -> None:
    async def scenario() -> None:
        manifest = PluginManifest(
            plugin=AGENT_PLUGIN,
            kind=PluginKind.AGENT,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permissions=("agent.decide",),
            execution_mode=PluginExecutionMode.PROCESS,
            timeout_seconds=10,
            max_attempts=2,
        )
        invocation = WorkflowInvocation(
            "run-agent-turns",
            GeneralWorkflowSpec(
                "agent-turns",
                "1",
                RELEASE,
                (manifest,),
                (
                    WorkflowNode(
                        "plan",
                        NodeKind.AGENT,
                        {
                            "plugin": AGENT_PLUGIN.to_mapping(),
                            "input": {"goal": "draft"},
                            "max_turns": 3,
                        },
                    ),
                ),
            ),
            {},
            GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
            task_queue="v7-temporal-agent-test",
        )
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        host = RegisteredPluginHost()
        observed_limits: list[int | None] = []

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            del registered
            observed_limits.append(command.agent_max_turns)
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={"ok": True})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            del command, registered
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        service = GovernedActivityService(
            ledger,
            host,
            {
                (
                    AGENT_PLUGIN.plugin_id,
                    AGENT_PLUGIN.version,
                    AGENT_PLUGIN.package_digest,
                ): manifest
            },
        )
        activities = V7TemporalActivities(service, ledger)
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue=invocation.task_queue,
                workflows=[GeneralWorkflow],
                activities=[activities.execute_governed_node, activities.record_workflow_event],
            ):
                handle = await environment.client.start_workflow(
                    GeneralWorkflow.run,
                    invocation.to_mapping(),
                    id="agent-turn-limit",
                    task_queue=invocation.task_queue,
                )
                result = await handle.result()
        assert result["status"] == "SUCCEEDED"
        assert observed_limits == [3]

    asyncio.run(scenario())


def test_cancellation_during_an_unknown_external_outcome_keeps_the_reservation() -> None:
    async def scenario() -> None:
        invocation = _invocation(approval=False)
        manifest = invocation.workflow.plugins[0]
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        host = RegisteredPluginHost()
        calls = 0

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            nonlocal calls
            del command, registered
            calls += 1
            raise TimeoutError("external request timed out after submission")

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            del command, registered
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        service = GovernedActivityService(
            ledger,
            host,
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
        )
        activities = V7TemporalActivities(service, ledger)
        command = ActionCommand.create(invocation, "call", PLUGIN, {"message": "hello"})
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue=invocation.task_queue,
                workflows=[GeneralWorkflow],
                activities=[activities.execute_governed_node, activities.record_workflow_event],
            ):
                handle = await environment.client.start_workflow(
                    GeneralWorkflow.run,
                    invocation.to_mapping(),
                    id="cancel-unknown-action",
                    task_queue=invocation.task_queue,
                )
                for _ in range(50):
                    record = await ledger.action(command.action_id)
                    if record is not None and record.state == "UNKNOWN":
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("workflow did not enter reconciliation")
                await handle.signal(GeneralWorkflow.cancel, "CANCEL_REQUESTED")
                result = await handle.result()
        persisted = await ledger.action(command.action_id)
        assert result["status"] == "CANCELLED"
        assert calls == 1
        assert persisted is not None
        assert persisted.state == "UNKNOWN"
        assert persisted.reservation_state == "RESERVED"
        assert any(event["type"] == "WORKFLOW_CANCELLED" for event in ledger.events)

    asyncio.run(scenario())
