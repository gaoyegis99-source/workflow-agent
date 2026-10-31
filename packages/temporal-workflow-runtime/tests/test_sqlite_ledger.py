from __future__ import annotations

import asyncio

from platform_temporal_workflows import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceFence,
    GovernedActivityService,
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

RELEASE = "sha256:" + "e" * 64
PLUGIN = PluginReference("http.durable", "1", "sha256:" + "f" * 64)


def _manifest() -> PluginManifest:
    return PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("remote.write",),
        execution_mode=PluginExecutionMode.REMOTE,
        timeout_seconds=10,
        max_attempts=3,
    )


def _command() -> ActionCommand:
    spec = GeneralWorkflowSpec(
        workflow_id="durable",
        version="1",
        release_digest=RELEASE,
        plugins=(_manifest(),),
        steps=(
            WorkflowNode(
                "write",
                NodeKind.HTTP,
                {"plugin": PLUGIN.to_mapping(), "input": {"value": "one"}},
            ),
        ),
    )
    invocation = WorkflowInvocation(
        "run-durable",
        spec,
        {},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
    )
    return ActionCommand.create(invocation, "write", PLUGIN, {"value": "one"})


def test_sqlite_ledger_survives_restart_and_reconciles_without_reinvoking(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        command = _command()
        path = tmp_path / "governance.sqlite"

        before_restart = SQLiteGovernanceLedger(path, release_digests={RELEASE})
        assert (await before_restart.authorize(command)).allowed
        assert (await before_restart.mark_dispatched(command)).invoke

        # A fresh adapter represents a killed Worker/process.  It sees the
        # admitted action identity and asks the original external system for
        # its result; it must not issue a second side-effecting invoke.
        after_restart = SQLiteGovernanceLedger(path, release_digests={RELEASE})
        host = RegisteredPluginHost()
        calls = 0

        async def invoke(command, manifest):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={})

        async def reconcile(command, manifest):  # type: ignore[no-untyped-def]
            return NodeOutcome(
                command.action_id,
                OutcomeStatus.SUCCEEDED,
                output={"remote": "confirmed"},
            )

        manifest = _manifest()
        host.register(manifest, invoke=invoke, reconcile=reconcile)
        service = GovernedActivityService(
            after_restart,
            host,
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
        )

        outcome = await service.execute(command)
        persisted = await after_restart.action(command.action_id)
        assert outcome.status is OutcomeStatus.SUCCEEDED
        assert calls == 0
        assert persisted is not None
        assert persisted.reservation_state == "SETTLED"

    asyncio.run(scenario())


def test_sqlite_ledger_allows_only_one_concurrent_dispatch_admission(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        command = _command()
        path = tmp_path / "governance.sqlite"
        first = SQLiteGovernanceLedger(path, release_digests={RELEASE})
        second = SQLiteGovernanceLedger(path, release_digests={RELEASE})
        assert (await first.authorize(command)).allowed

        admissions = await asyncio.gather(
            first.mark_dispatched(command), second.mark_dispatched(command)
        )
        assert [entry.invoke for entry in admissions].count(True) == 1

    asyncio.run(scenario())
