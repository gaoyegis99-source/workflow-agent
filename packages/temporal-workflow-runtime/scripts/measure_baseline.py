#!/usr/bin/env python3
"""Measure the V7 Temporal SDK baseline without a production deployment.

This runs a local time-skipping Temporal test environment, measures one
governed Activity round trip and one completed-history replay at an approval
wait. It is a development baseline only; Compose fault drills remain the
release evidence for real process failure recovery.
"""

from __future__ import annotations

import asyncio
import json
import platform
import resource
import sys
import time

from temporalio import __version__ as temporal_version
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from platform_temporal_workflows import (
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
    WorkflowInvocation,
    WorkflowNode,
)
from platform_temporal_workflows.activities import V7TemporalActivities
from platform_temporal_workflows.temporal_runtime import GeneralWorkflow

RELEASE = "sha256:" + "b" * 64
PLUGIN = PluginReference("http.baseline", "1", "sha256:" + "c" * 64)
QUEUE = "v7-temporal-baseline"


def _rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


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
    steps = [
        WorkflowNode(
            "call",
            NodeKind.HTTP,
            {"plugin": PLUGIN.to_mapping(), "input": {"sample": True}},
        )
    ]
    if approval:
        steps.append(WorkflowNode("review", NodeKind.APPROVAL, {"prompt": "approve"}))
    return WorkflowInvocation(
        "baseline-approval" if approval else "baseline-run",
        GeneralWorkflowSpec("baseline", "1", RELEASE, (manifest,), tuple(steps)),
        {},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
        task_queue=QUEUE,
    )


async def _measure() -> dict[str, object]:
    ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
    host = RegisteredPluginHost()
    manifest = _invocation(approval=False).workflow.plugins[0]

    async def invoke(command, registered):  # type: ignore[no-untyped-def]
        del registered
        return NodeOutcome(
            command.action_id, OutcomeStatus.SUCCEEDED, output={"echo": command.inputs}
        )

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
    standard = _invocation(approval=False)
    approval = _invocation(approval=True)
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue=QUEUE,
            workflows=[GeneralWorkflow],
            activities=[activities.execute_governed_node, activities.record_workflow_event],
        ):
            started = time.perf_counter_ns()
            standard_handle = await environment.client.start_workflow(
                GeneralWorkflow.run,
                standard.to_mapping(),
                id="v7-baseline-run",
                task_queue=QUEUE,
            )
            standard_result = await standard_handle.result()
            completed = time.perf_counter_ns()

            approval_handle = await environment.client.start_workflow(
                GeneralWorkflow.run,
                approval.to_mapping(),
                id="v7-baseline-approval",
                task_queue=QUEUE,
            )
            for _ in range(100):
                if any(event["type"] == "APPROVAL_REQUESTED" for event in ledger.events):
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("approval workflow did not reach a durable wait")
            replay_started = time.perf_counter_ns()
            replay = await Replayer(workflows=[GeneralWorkflow]).replay_workflow(
                await approval_handle.fetch_history(),
                raise_on_replay_failure=False,
            )
            replay_completed = time.perf_counter_ns()
            if replay.replay_failure is not None:
                raise RuntimeError("Temporal history replay failed")
            await approval_handle.signal(
                GeneralWorkflow.approve,
                {"node_key": "review", "approved": True, "actor_id": "reviewer"},
            )
            approval_result = await approval_handle.result()
    return {
        "schema_version": "v7-temporal-sdk-baseline-v1",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "temporalio_version": temporal_version,
        "mode": "time-skipping-sdk-local",
        "governed_activity_round_trip_ms": round((completed - started) / 1_000_000, 3),
        "approval_wait_history_replay_ms": round(
            (replay_completed - replay_started) / 1_000_000, 3
        ),
        "process_peak_rss_bytes": _rss_bytes(),
        "standard_status": standard_result["status"],
        "approval_status": approval_result["status"],
    }


def main() -> int:
    print(json.dumps(asyncio.run(_measure()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
