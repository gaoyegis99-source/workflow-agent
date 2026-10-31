from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from platform_temporal_workflows import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceFence,
    GovernedActivityService,
    InMemoryGovernanceLedger,
    NodeKind,
    OutcomeStatus,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    ProcessPluginDefinition,
    ProcessPluginHost,
    WorkflowContractError,
    WorkflowInvocation,
    WorkflowNode,
)

RELEASE = "sha256:" + "5" * 64
PLUGIN = PluginReference("process.echo", "1", "sha256:" + "6" * 64)


def _manifest() -> PluginManifest:
    return PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("remote.call",),
        execution_mode=PluginExecutionMode.PROCESS,
        timeout_seconds=5,
        max_attempts=2,
    )


def _invocation(inputs: dict[str, object]) -> WorkflowInvocation:
    manifest = _manifest()
    return WorkflowInvocation(
        "run-process",
        GeneralWorkflowSpec(
            "process-host",
            "1",
            RELEASE,
            (manifest,),
            (
                WorkflowNode(
                    "call", NodeKind.HTTP, {"plugin": PLUGIN.to_mapping(), "input": inputs}
                ),
            ),
        ),
        {},
        GovernanceFence("project", "task", "actor", 1, 1, RELEASE),
    )


def _host() -> ProcessPluginHost:
    fixture = Path(__file__).parent / "fixtures" / "process_plugin.py"
    host = ProcessPluginHost()
    host.register(ProcessPluginDefinition(_manifest(), (sys.executable, str(fixture))))
    return host


def test_process_host_runs_an_exact_registered_plugin_without_parent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        invocation = _invocation({"value": "ok"})
        command = ActionCommand.create(invocation, "call", PLUGIN, {"value": "ok"})
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger,
            _host(),
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): _manifest()},
        )
        outcome = await service.execute(command)
        persisted = await ledger.action(command.action_id)

        assert outcome.status is OutcomeStatus.SUCCEEDED
        assert outcome.output == {
            "echo": {"value": "ok"},
            "parent_environment_was_present": False,
            "protocol": "v7-process-plugin-v1",
        }
        assert persisted is not None and persisted.reservation_state == "SETTLED"

    monkeypatch.setenv("V7_TEST_PARENT_SECRET", "must-not-reach-plugin")
    asyncio.run(scenario())


def test_process_host_reconciliation_and_malformed_results_are_fail_closed() -> None:
    async def scenario() -> None:
        known = _invocation({"known": True})
        command = ActionCommand.create(known, "call", PLUGIN, {"known": True})
        ledger = InMemoryGovernanceLedger(release_digests={RELEASE})
        service = GovernedActivityService(
            ledger,
            _host(),
            {(PLUGIN.plugin_id, PLUGIN.version, PLUGIN.package_digest): _manifest()},
        )
        assert (await ledger.authorize(command)).allowed
        assert (await ledger.mark_dispatched(command)).invoke
        reconciled = await service.execute(command)
        assert reconciled.status is OutcomeStatus.SUCCEEDED

        malformed = _invocation({"malformed": True})
        malformed_command = ActionCommand.create(malformed, "call", PLUGIN, {"malformed": True})
        failed = await service.execute(malformed_command)
        persisted = await ledger.action(malformed_command.action_id)
        assert failed.status is OutcomeStatus.RECONCILING
        assert persisted is not None and persisted.state == "UNKNOWN"

    asyncio.run(scenario())


def test_process_definition_rejects_unreviewed_modes_and_sensitive_environment() -> None:
    manifest = _manifest()
    with pytest.raises(WorkflowContractError, match="absolute"):
        ProcessPluginDefinition(manifest, ("python", "plugin.py"))
    with pytest.raises(WorkflowContractError, match="sensitive"):
        ProcessPluginDefinition(manifest, (sys.executable, "plugin.py"), {"API_KEY": "value"})
    with pytest.raises(WorkflowContractError, match="invalid"):
        ProcessPluginDefinition(
            manifest, (sys.executable, "plugin.py"), {"V7_PLUGIN_PROTOCOL": "override"}
        )
    container_manifest = PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("remote.call",),
        execution_mode=PluginExecutionMode.CONTAINER,
        timeout_seconds=5,
        max_attempts=2,
    )
    with pytest.raises(WorkflowContractError, match="process-mode"):
        ProcessPluginDefinition(container_manifest, (sys.executable, "plugin.py"))
