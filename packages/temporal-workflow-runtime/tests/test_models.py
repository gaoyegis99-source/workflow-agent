from __future__ import annotations

import pytest

from platform_temporal_workflows import (
    ActionCommand,
    GeneralWorkflowSpec,
    GovernanceFence,
    NodeKind,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginReference,
    WorkflowContractError,
    WorkflowInvocation,
    WorkflowNode,
)

RELEASE = "sha256:" + "a" * 64
PLUGIN = PluginReference("http.echo", "1.0.0", "sha256:" + "b" * 64)
AGENT_PLUGIN = PluginReference("agent.planner", "1.0.0", "sha256:" + "c" * 64)


def _manifest() -> PluginManifest:
    return PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.HTTP,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("http.invoke",),
        execution_mode=PluginExecutionMode.REMOTE,
        timeout_seconds=10,
        max_attempts=3,
        credential_ref="service_binding",
    )


def _workflow() -> GeneralWorkflowSpec:
    return GeneralWorkflowSpec(
        workflow_id="integration",
        version="1.0.0",
        release_digest=RELEASE,
        plugins=(_manifest(),),
        steps=(
            WorkflowNode(
                "call", NodeKind.HTTP, {"plugin": PLUGIN.to_mapping(), "input": {"message": "hi"}}
            ),
        ),
    )


def _fence() -> GovernanceFence:
    return GovernanceFence("project", "task", "operator", 1, 1, RELEASE)


def test_frozen_definition_requires_an_audited_exact_plugin_version() -> None:
    spec = _workflow()
    invocation = WorkflowInvocation("run-1", spec, {"input": "ok"}, _fence())
    assert invocation.workflow.definition_digest.startswith("sha256:")
    assert invocation.to_mapping()["workflow"]["version"] == "1.0.0"

    with pytest.raises(WorkflowContractError, match="fixed"):
        PluginReference("http.echo", "latest", "sha256:" + "b" * 64)
    with pytest.raises(WorkflowContractError, match="incompatible"):
        GeneralWorkflowSpec(
            workflow_id="bad",
            version="1",
            release_digest=RELEASE,
            plugins=(_manifest(),),
            steps=(
                WorkflowNode("wrong", NodeKind.MCP, {"plugin": PLUGIN.to_mapping(), "input": {}}),
            ),
        )


def test_workflow_enforces_bounded_loop_and_never_accepts_credentials() -> None:
    with pytest.raises(WorkflowContractError, match="max_iterations"):
        WorkflowNode("loop", NodeKind.LOOP, {"items_path": "/items", "max_iterations": 0}, ((),))
    with pytest.raises(WorkflowContractError, match="credential"):
        WorkflowInvocation("run-1", _workflow(), {"api_key": "bad"}, _fence())
    with pytest.raises(WorkflowContractError, match="credential"):
        PluginManifest(
            plugin=PLUGIN,
            kind=PluginKind.HTTP,
            input_schema={"api_key": {"type": "string"}},
            output_schema={"type": "object"},
            permissions=("http.invoke",),
            execution_mode=PluginExecutionMode.REMOTE,
            timeout_seconds=10,
            max_attempts=1,
        )


def test_action_identity_binds_the_exact_frozen_definition() -> None:
    first = _workflow()
    second = GeneralWorkflowSpec(
        workflow_id="integration",
        version="1.0.0",
        release_digest=RELEASE,
        plugins=(_manifest(),),
        steps=(
            WorkflowNode(
                "call",
                NodeKind.HTTP,
                {"plugin": PLUGIN.to_mapping(), "input": {"message": "changed"}},
            ),
        ),
    )
    first_invocation = WorkflowInvocation("run-1", first, {}, _fence())
    second_invocation = WorkflowInvocation("run-1", second, {}, _fence())
    first_command = ActionCommand.create(first_invocation, "call", PLUGIN, {"message": "same"})
    second_command = ActionCommand.create(second_invocation, "call", PLUGIN, {"message": "same"})

    assert first.definition_digest != second.definition_digest
    assert first_command.workflow_digest == first.definition_digest
    assert first_command.action_id != second_command.action_id


def test_agent_action_binds_its_turn_limit_into_the_frozen_command() -> None:
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
    spec = GeneralWorkflowSpec(
        workflow_id="agent-turns",
        version="1",
        release_digest=RELEASE,
        plugins=(manifest,),
        steps=(
            WorkflowNode(
                "plan",
                NodeKind.AGENT,
                {
                    "plugin": AGENT_PLUGIN.to_mapping(),
                    "input": {"goal": "plan"},
                    "max_turns": 3,
                },
            ),
        ),
    )
    invocation = WorkflowInvocation("run-agent", spec, {}, _fence())
    command = ActionCommand.create(
        invocation,
        "plan",
        AGENT_PLUGIN,
        {"goal": "plan"},
        agent_max_turns=3,
    )

    assert ActionCommand.from_mapping(command.to_mapping()).agent_max_turns == 3
    tampered = command.to_mapping()
    tampered["agent_max_turns"] = 4
    with pytest.raises(WorkflowContractError, match="identity"):
        ActionCommand.from_mapping(tampered)
    with pytest.raises(WorkflowContractError, match="agent_max_turns"):
        ActionCommand.create(
            invocation,
            "plan",
            AGENT_PLUGIN,
            {"goal": "plan"},
            agent_max_turns=0,
        )


def test_subworkflow_catalog_rejects_recursive_definitions() -> None:
    first = GeneralWorkflowSpec(
        workflow_id="first",
        version="1",
        release_digest=RELEASE,
        plugins=(),
        steps=(
            WorkflowNode(
                "call-second",
                NodeKind.SUBWORKFLOW,
                {"workflow_id": "second", "version": "1"},
            ),
        ),
    )
    second = GeneralWorkflowSpec(
        workflow_id="second",
        version="1",
        release_digest="sha256:" + "f" * 64,
        plugins=(),
        steps=(
            WorkflowNode(
                "call-first",
                NodeKind.SUBWORKFLOW,
                {"workflow_id": "first", "version": "1"},
            ),
        ),
    )

    with pytest.raises(WorkflowContractError, match="cycle"):
        WorkflowInvocation("run-1", first, {}, _fence(), subworkflows=(second,))
