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
    WorkflowInvocation,
    WorkflowNode,
)
from platform_temporal_workflows.observability import activity_span, trigger_span

RELEASE = "sha256:" + "a" * 64
PLUGIN = PluginReference("telemetry.agent", "1", "sha256:" + "b" * 64)


class _Context:
    def __enter__(self) -> _Context:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, object]]] = []

    def start_as_current_span(self, name: str, *, attributes: dict[str, object]) -> _Context:
        self.spans.append((name, attributes))
        return _Context()


def _command() -> tuple[ActionCommand, PluginManifest]:
    manifest = PluginManifest(
        plugin=PLUGIN,
        kind=PluginKind.AGENT,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permissions=("agent.decide",),
        execution_mode=PluginExecutionMode.PROCESS,
        timeout_seconds=10,
        max_attempts=2,
    )
    invocation = WorkflowInvocation(
        "run-observe",
        GeneralWorkflowSpec(
            "observe-flow",
            "1",
            RELEASE,
            (manifest,),
            (
                WorkflowNode(
                    "plan",
                    NodeKind.AGENT,
                    {
                        "plugin": PLUGIN.to_mapping(),
                        "input": {"prompt": "secret user text must not trace"},
                        "max_turns": 2,
                    },
                ),
            ),
        ),
        {},
        GovernanceFence("project", "task", "operator", 1, 1, RELEASE),
    )
    return ActionCommand.create(
        invocation,
        "plan",
        PLUGIN,
        {"prompt": "secret user text must not trace"},
        agent_max_turns=2,
    ), manifest


def test_spans_only_receive_frozen_safe_identity_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry import trace

    tracer = _Tracer()
    monkeypatch.setattr(trace, "get_tracer", lambda _: tracer)
    command, manifest = _command()
    with activity_span(command, manifest):
        pass
    with trigger_span(
        "outbox-delivery",
        workflow_id="observe-flow",
        version="1",
        run_id=command.run_id,
        dispatch_id="td-identity",
    ):
        pass

    activity_name, activity_attributes = tracer.spans[0]
    assert activity_name == "v7.temporal.activity"
    assert activity_attributes == {
        "workflow.run_id": command.run_id,
        "workflow.action_id": command.action_id,
        "workflow.node_key": "plan",
        "workflow.definition_digest": command.workflow_digest,
        "plugin.id": "telemetry.agent",
        "plugin.version": "1",
        "plugin.kind": "agent",
        "plugin.execution_mode": "process",
    }
    assert "secret user text" not in repr(tracer.spans)
