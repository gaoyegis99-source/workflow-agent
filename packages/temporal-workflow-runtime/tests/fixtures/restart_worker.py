"""A deliberately killable Temporal Worker used by the restart integration test.

The parent test starts this as a separate OS process, waits until its governed
action and approval event are durable, then sends SIGKILL.  Keeping it in a
small executable fixture makes the test exercise the same failure boundary as
a real Worker process rather than a graceful ``Worker.shutdown()`` path.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from platform_temporal_workflows import (
    GovernedActivityService,
    NodeOutcome,
    OutcomeStatus,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    RegisteredPluginHost,
    SQLiteGovernanceLedger,
)
from platform_temporal_workflows.activities import V7TemporalActivities
from platform_temporal_workflows.temporal_runtime import GeneralWorkflow


async def _run(
    target_host: str,
    task_queue: str,
    ledger_path: Path,
    release_digest: str,
    plugin_id: str,
    plugin_version: str,
    plugin_digest: str,
) -> None:
    plugin = PluginReference(plugin_id, plugin_version, plugin_digest)
    manifest = PluginManifest(
        plugin=plugin,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("http.invoke",),
        execution_mode=PluginExecutionMode.REMOTE,
        timeout_seconds=10,
        max_attempts=2,
    )
    ledger = SQLiteGovernanceLedger(ledger_path, release_digests={release_digest})
    host = RegisteredPluginHost()

    async def invoke(command, registered):  # type: ignore[no-untyped-def]
        del registered
        return NodeOutcome(
            command.action_id,
            OutcomeStatus.SUCCEEDED,
            output={"echo": command.inputs},
        )

    async def reconcile(command, registered):  # type: ignore[no-untyped-def]
        del command, registered
        return None

    host.register(manifest, invoke=invoke, reconcile=reconcile)
    service = GovernedActivityService(
        ledger,
        host,
        {(plugin.plugin_id, plugin.version, plugin.package_digest): manifest},
    )
    activities = V7TemporalActivities(service, ledger)
    client = await Client.connect(target_host, namespace="default")
    async with Worker(
        client,
        task_queue=task_queue,
        identity="v7-restart-test-worker",
        # The fixture must not leave a test-server sticky task pinned to the
        # process that is about to be SIGKILLed. Production workers retain
        # their usual cache; this setting makes the process-loss test exercise
        # history replay through the ordinary task queue deterministically.
        max_cached_workflows=0,
        workflows=[GeneralWorkflow],
        activities=[activities.execute_governed_node, activities.record_workflow_event],
    ):
        await asyncio.Event().wait()


def main() -> int:
    if len(sys.argv) != 8:
        return 2
    asyncio.run(
        _run(
            sys.argv[1],
            sys.argv[2],
            Path(sys.argv[3]),
            sys.argv[4],
            sys.argv[5],
            sys.argv[6],
            sys.argv[7],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
