from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.errors import RuntimeConflict, RuntimeNotFound, RuntimeStopped
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.plain_loop import PlainLoopRuntime
from platform_agent_runtime.profiles import LANGGRAPH_PROFILE_ID, PLAIN_PROFILE_ID, profile
from platform_core.domain.agent_runtime import (
    DecisionFrame,
    DecisionProposal,
    WakeBatch,
    canonical_digest,
)

DIGEST = "sha256:" + "a" * 64


class ScriptedDecision:
    def __init__(self):
        self.calls: list[tuple[str, tuple[Mapping[str, object], ...]]] = []

    def __call__(
        self,
        frame: DecisionFrame,
        observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal:
        self.calls.append((frame.frame_id, tuple(observations)))
        return DecisionProposal(
            kind="ACTION_REQUESTS",
            frame_id=frame.frame_id,
            expected_task_revision=frame.task_spec_revision,
            body={
                "requests": [
                    {
                        "call_id": "call-" + frame.frame_id,
                        "tool_id": "execution-submit",
                        "arguments": {"asset": "asset-1"},
                    }
                ]
            },
        )


def frame(number: int, *, payload=None, runtime_ref="agent-run-1"):
    return DecisionFrame(
        agent_run_id=runtime_ref,
        frame_id=f"frame-{number}",
        task_id="task-1",
        task_spec_revision=1,
        control_version=1,
        generation=1,
        context_manifest_digest=DIGEST,
        payload=payload or {"sequence": number},
    )


def runtimes():
    plain_decision = ScriptedDecision()
    graph_decision = ScriptedDecision()
    return [
        (PlainLoopRuntime(plain_decision), plain_decision, profile(PLAIN_PROFILE_ID)),
        (
            LangGraphRuntime(graph_decision, InMemorySaver()),
            graph_decision,
            profile(LANGGRAPH_PROFILE_ID),
        ),
    ]


def deployment_profile(profile_id: str, *, operator_digest: str = "sha256:" + "b" * 64):
    unsigned = profile(profile_id) | {
        "tool_catalog_id": "p7-science-tools-v2",
        "tool_catalog_digest": "sha256:" + "a" * 64,
        "operator_catalog_digest": operator_digest,
    }
    return unsigned | {"deployment_profile_digest": canonical_digest(unsigned)}


def test_plain_and_langgraph_have_the_same_yield_and_replay_semantics():
    results = []
    for runtime, decision, selected in runtimes():
        receipt = runtime.start({"runtime_profile": selected}, frame(1))
        assert receipt.state == "WAITING_FRAME"
        proposal = runtime.advance("agent-run-1", frame(1), ({"kind": "FACT"},))
        replay = runtime.advance("agent-run-1", frame(1), ({"kind": "DIFFERENT"},))
        assert replay == proposal
        assert len(decision.calls) == 1
        status = runtime.inspect("agent-run-1")
        assert status.checkpoint_revision == 1
        assert status.last_frame_id == "frame-1"
        assert status.last_proposal_digest == proposal.digest
        assert runtime.export_visible_trace("agent-run-1").event_count == 2
        results.append(proposal)
    assert results[0] == results[1]


def test_adapter_yields_business_action_without_executing_it():
    external_side_effects = []
    runtime, _, selected = runtimes()[1]
    runtime.start({"runtime_profile": selected}, frame(1))
    proposal = runtime.advance("agent-run-1", frame(1), ())
    assert proposal.kind == "ACTION_REQUESTS"
    assert external_side_effects == []


@pytest.mark.parametrize("runtime_index", [0, 1])
def test_runtime_accepts_the_validated_p7_catalogue_binding(runtime_index):
    runtime, decision, selected = runtimes()[runtime_index]
    frozen = deployment_profile(selected["profile_id"])
    receipt = runtime.start({"runtime_profile": frozen}, frame(1))
    proposal = runtime.advance("agent-run-1", frame(1), ())
    assert receipt.profile_digest == selected["profile_digest"]
    assert proposal.frame_id == "frame-1"
    assert len(decision.calls) == 1


@pytest.mark.parametrize("runtime_index", [0, 1])
def test_runtime_identity_cannot_be_reused_with_another_catalogue_binding(runtime_index):
    runtime, _, selected = runtimes()[runtime_index]
    runtime.start(
        {"runtime_profile": deployment_profile(selected["profile_id"])},
        frame(1),
    )
    with pytest.raises(RuntimeConflict, match="another deployment profile"):
        runtime.start(
            {
                "runtime_profile": deployment_profile(
                    selected["profile_id"],
                    operator_digest="sha256:" + "c" * 64,
                )
            },
            frame(1),
        )


@pytest.mark.parametrize("runtime_index", [0, 1])
def test_same_frame_identity_with_other_content_is_a_conflict(runtime_index):
    runtime, _, selected = runtimes()[runtime_index]
    runtime.start({"runtime_profile": selected}, frame(1))
    runtime.advance("agent-run-1", frame(1), ())
    with pytest.raises(RuntimeConflict):
        runtime.advance("agent-run-1", frame(1, payload={"changed": True}), ())


@pytest.mark.parametrize("runtime_index", [0, 1])
def test_wake_batch_is_visible_to_exactly_one_new_decision(runtime_index):
    runtime, decision, selected = runtimes()[runtime_index]
    runtime.start({"runtime_profile": selected}, frame(1))
    wake = WakeBatch("wake-1", ("event-1", "event-2"), "EXECUTION")
    proposal = runtime.resume("agent-run-1", wake, frame(2), ())
    assert proposal.frame_id == "frame-2"
    assert len(decision.calls) == 1
    observed = decision.calls[0][1][-1]
    assert observed["wake_id"] == "wake-1"
    assert observed["event_ids"] == ["event-1", "event-2"]


@pytest.mark.parametrize("runtime_index", [0, 1])
def test_interrupt_is_idempotent_and_stops_future_decisions(runtime_index):
    runtime, decision, selected = runtimes()[runtime_index]
    runtime.start({"runtime_profile": selected}, frame(1))
    first = runtime.interrupt("agent-run-1", "task cancelled")
    second = runtime.interrupt("agent-run-1", "task cancelled")
    assert first.state == second.state == "STOPPED"
    with pytest.raises(RuntimeStopped):
        runtime.advance("agent-run-1", frame(1), ())
    assert decision.calls == []


def test_unknown_runtime_and_profile_mismatch_are_explicit():
    runtime = LangGraphRuntime(ScriptedDecision(), InMemorySaver())
    with pytest.raises(RuntimeNotFound):
        runtime.inspect("missing-run")
    with pytest.raises(RuntimeConflict):
        runtime.start({"runtime_profile": profile(PLAIN_PROFILE_ID)}, frame(1))
