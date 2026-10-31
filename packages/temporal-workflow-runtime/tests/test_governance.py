from __future__ import annotations

import asyncio

import pytest

from platform_temporal_workflows import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceError,
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
    WorkflowContractError,
    WorkflowInvocation,
    WorkflowNode,
)

RELEASE = "sha256:" + "c" * 64
PLUGIN = PluginReference("http.write", "1", "sha256:" + "d" * 64)


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


def _command(value: str = "one") -> ActionCommand:
    spec = GeneralWorkflowSpec(
        workflow_id="work",
        version="1",
        release_digest=RELEASE,
        plugins=(_manifest(),),
        steps=(
            WorkflowNode(
                "write", NodeKind.HTTP, {"plugin": PLUGIN.to_mapping(), "input": {"value": value}}
            ),
        ),
    )
    invocation = WorkflowInvocation(
        "run-1", spec, {}, GovernanceFence("project", "task", "operator", 1, 1, RELEASE)
    )
    return ActionCommand.create(invocation, "write", PLUGIN, {"value": value})


def test_activity_retry_reuses_one_business_action_and_one_budget_reservation() -> None:
    async def scenario() -> None:
        calls = 0
        manifest = _manifest()
        host = RegisteredPluginHost()

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={"written": True})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger, host, {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest}
        )
        command = _command()

        first = await service.execute(command)
        second = await service.execute(command)
        record = await ledger.action(command.action_id)
        assert first.status is OutcomeStatus.SUCCEEDED
        assert second.to_mapping() == first.to_mapping()
        assert calls == 1
        assert record is not None and record.reservation_state == "SETTLED"

    asyncio.run(scenario())


def test_unknown_external_outcome_reconciles_without_reinvoking_the_plugin() -> None:
    async def scenario() -> None:
        calls = 0
        discovered: NodeOutcome | None = None
        manifest = _manifest()
        host = RegisteredPluginHost()

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise ConnectionError("remote accepted request but dropped the receipt")

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return discovered

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger, host, {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest}
        )
        command = _command()

        assert (await service.execute(command)).status is OutcomeStatus.RECONCILING
        assert (await service.execute(command)).status is OutcomeStatus.RECONCILING
        assert calls == 1
        discovered = NodeOutcome(
            command.action_id, OutcomeStatus.SUCCEEDED, output={"remote": "confirmed"}
        )
        assert (await service.execute(command)).status is OutcomeStatus.SUCCEEDED
        assert calls == 1

    asyncio.run(scenario())


def test_permission_or_budget_change_blocks_a_new_action_at_execution_time() -> None:
    async def scenario() -> None:
        manifest = _manifest()
        host = RegisteredPluginHost()
        called = False

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(authorization_revision=2, release_digests={RELEASE})
        service = GovernedActivityService(
            ledger, host, {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest}
        )
        denied = await service.execute(_command())
        assert denied.status is OutcomeStatus.DENIED
        assert denied.error_code == "AUTHORIZATION_STALE"
        assert not called

    asyncio.run(scenario())


def test_schema_rejection_never_crosses_the_plugin_boundary() -> None:
    async def scenario() -> None:
        manifest = PluginManifest(
            plugin=PLUGIN,
            kind=PluginKind.HTTP,
            input_schema={
                "type": "object",
                "required": ["count"],
                "properties": {"count": {"type": "integer"}},
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            permissions=("remote.write",),
            execution_mode=PluginExecutionMode.REMOTE,
            timeout_seconds=10,
            max_attempts=3,
        )
        host = RegisteredPluginHost()
        called = False

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        service = GovernedActivityService(
            InMemoryGovernanceLedger(release_digests={RELEASE}),
            host,
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
        )

        outcome = await service.execute(_command("not-an-integer"))
        assert outcome.status is OutcomeStatus.FAILED
        assert outcome.error_code == "INPUT_SCHEMA_INVALID"
        assert not called

    asyncio.run(scenario())


def test_invalid_plugin_receipt_becomes_reconciling_not_a_terminal_success() -> None:
    async def scenario() -> None:
        manifest = PluginManifest(
            plugin=PLUGIN,
            kind=PluginKind.HTTP,
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "required": ["confirmed"],
                "properties": {"confirmed": {"type": "boolean"}},
                "additionalProperties": False,
            },
            permissions=("remote.write",),
            execution_mode=PluginExecutionMode.REMOTE,
            timeout_seconds=10,
            max_attempts=3,
        )
        host = RegisteredPluginHost()

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            return NodeOutcome(
                command.action_id,
                OutcomeStatus.SUCCEEDED,
                output={"confirmed": "not-a-boolean"},
            )

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger,
            host,
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
        )
        command = _command()

        outcome = await service.execute(command)
        persisted = await ledger.action(command.action_id)
        assert outcome.status is OutcomeStatus.RECONCILING
        assert persisted is not None and persisted.state == "UNKNOWN"
        assert persisted.reservation_state == "RESERVED"

    asyncio.run(scenario())


def test_admitted_action_is_rechecked_after_permission_revocation() -> None:
    async def scenario() -> None:
        manifest = _manifest()
        host = RegisteredPluginHost()
        called = False

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger,
            host,
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
        )
        command = _command()
        assert (await ledger.authorize(command)).allowed
        ledger.revoked = True

        outcome = await service.execute(command)
        record = await ledger.action(command.action_id)
        assert outcome.status is OutcomeStatus.DENIED
        assert not called
        assert record is not None and record.reservation_state == "RELEASED"

    asyncio.run(scenario())


def test_concurrent_activity_deliveries_do_not_invoke_the_plugin_twice() -> None:
    async def scenario() -> None:
        manifest = _manifest()
        host = RegisteredPluginHost()
        invoke_started = asyncio.Event()
        finish_invoke = asyncio.Event()
        calls = 0

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            invoke_started.set()
            await finish_invoke.wait()
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger,
            host,
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
        )
        command = _command()
        first = asyncio.create_task(service.execute(command))
        await invoke_started.wait()
        duplicate = await service.execute(command)
        finish_invoke.set()
        original = await first

        assert original.status is OutcomeStatus.SUCCEEDED
        assert duplicate.status is OutcomeStatus.RECONCILING
        assert calls == 1

    asyncio.run(scenario())


def test_action_identity_cannot_be_reused_with_different_inputs() -> None:
    async def scenario() -> None:
        manifest = _manifest()
        host = RegisteredPluginHost()

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger, host, {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest}
        )
        original = _command("one")
        await service.execute(original)
        with pytest.raises(WorkflowContractError, match="identity"):
            ActionCommand(
                run_id=original.run_id,
                node_key=original.node_key,
                action_id=original.action_id,
                idempotency_key=original.idempotency_key,
                workflow_digest=original.workflow_digest,
                plugin=original.plugin,
                inputs={"value": "different"},
                fence=original.fence,
            )

    asyncio.run(scenario())


def test_late_worker_outcome_cannot_overwrite_a_settled_business_fact() -> None:
    async def scenario() -> None:
        manifest = _manifest()
        host = RegisteredPluginHost()

        async def invoke(command, registered):  # type: ignore[no-untyped-def]
            return NodeOutcome(command.action_id, OutcomeStatus.SUCCEEDED, output={"ok": True})

        async def reconcile(command, registered):  # type: ignore[no-untyped-def]
            return None

        host.register(manifest, invoke=invoke, reconcile=reconcile)
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger,
            host,
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): manifest},
        )
        command = _command()
        assert (await service.execute(command)).status is OutcomeStatus.SUCCEEDED

        with pytest.raises(GovernanceError, match="conflicts"):
            await ledger.record_outcome(
                command,
                NodeOutcome(command.action_id, OutcomeStatus.FAILED, error_code="LATE_FAILURE"),
            )
        persisted = await ledger.action(command.action_id)
        assert persisted is not None
        assert persisted.outcome is not None
        assert persisted.outcome.status is OutcomeStatus.SUCCEEDED
        assert persisted.reservation_state == "SETTLED"

    asyncio.run(scenario())
