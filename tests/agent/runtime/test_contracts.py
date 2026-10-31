import copy

import pytest
from platform_contracts.agent_runtime import AgentFrame, AgentRuntimeProfile, DecisionProposal
from platform_core.domain.agent_runtime import DecisionFrame
from platform_core.domain.agent_runtime import DecisionProposal as CoreProposal
from platform_core.domain.control import RuleViolation
from pydantic import TypeAdapter, ValidationError

DIGEST = "sha256:" + "1" * 64


def frame_payload():
    return {
        "agent_run_id": "agent-run-1",
        "frame_id": "frame-1",
        "task_ref": {"task_id": "task-1", "spec_revision": 2},
        "control_version": 3,
        "generation": 1,
        "context_manifest_digest": DIGEST,
        "payload": {"goal": "inspect", "facts": [1, True, None]},
    }


def action_proposal():
    return {
        "kind": "ACTION_REQUESTS",
        "frame_id": "frame-1",
        "expected_task_revision": 2,
        "requests": [
            {
                "call_id": "call-1",
                "tool_id": "execution-submit",
                "tool_version": "1.0.0",
                "purpose": "Run a registered operator",
                "arguments": {"operator": "trend", "count": 1},
                "expected_observation": "An accepted execution identity",
            }
        ],
    }


@pytest.mark.parametrize(
    "payload",
    [
        action_proposal(),
        {
            "kind": "WAIT_REQUEST",
            "frame_id": "frame-1",
            "expected_task_revision": 2,
            "reason": "EXECUTION",
            "dependencies": [
                {"owner": "EXECUTION", "object_id": "execution-1", "minimum_revision": 1}
            ],
        },
        {
            "kind": "CLARIFICATION_REQUEST",
            "frame_id": "frame-1",
            "expected_task_revision": 2,
            "gap_id": "unit-gap",
            "question": "Which unit is declared?",
            "why_needed": "The calculation depends on a physical unit",
            "answer_schema": {"type": "string"},
            "blocked_actions": ["call-1"],
        },
        {
            "kind": "UNABLE_TO_PROCEED",
            "frame_id": "frame-1",
            "expected_task_revision": 2,
            "code": "MISSING_INPUT",
            "reasons": ["No source asset was supplied"],
        },
    ],
)
def test_decision_proposal_is_a_strict_discriminated_union(payload):
    adapter = TypeAdapter(DecisionProposal)
    parsed = adapter.validate_python(payload)
    assert adapter.validate_json(adapter.dump_json(parsed)) == parsed


def test_agent_frame_and_core_digest_are_stable():
    wire = AgentFrame.model_validate(frame_payload())
    core = DecisionFrame(
        agent_run_id=wire.agent_run_id,
        frame_id=wire.frame_id,
        task_id=wire.task_ref.task_id,
        task_spec_revision=wire.task_ref.spec_revision,
        control_version=wire.control_version,
        generation=wire.generation,
        context_manifest_digest=wire.context_manifest_digest,
        payload=wire.payload,
    )
    assert core.digest == core.digest
    changed = DecisionFrame(
        agent_run_id=core.agent_run_id,
        frame_id=core.frame_id,
        task_id=core.task_id,
        task_spec_revision=core.task_spec_revision,
        control_version=core.control_version,
        generation=core.generation,
        context_manifest_digest=core.context_manifest_digest,
        payload={"facts": [1, True, None], "goal": "inspect"},
    )
    assert changed.digest == core.digest


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(kind="COMPLETE_TASK"),
        lambda value: value.update(extra="hidden"),
        lambda value: value["requests"][0].update(tool_version="latest"),
        lambda value: value["requests"][0].update(arguments={"bad": float("nan")}),
    ],
)
def test_invalid_or_open_ended_proposals_are_rejected(mutation):
    payload = copy.deepcopy(action_proposal())
    mutation(payload)
    with pytest.raises(ValidationError):
        TypeAdapter(DecisionProposal).validate_python(payload)


def test_core_proposal_rejects_non_json_and_unknown_completion_claim():
    with pytest.raises(RuleViolation):
        CoreProposal("COMPLETED", "frame-1", 1, {})
    with pytest.raises((TypeError, ValueError)):
        CoreProposal("EXPLANATION", "frame-1", 1, {"value": object()})


def test_runtime_profile_contract_rejects_inconsistent_framework():
    payload = {
        "schema_version": 1,
        "profile_id": "plain-loop-v1",
        "adapter": "PLAIN_LOOP",
        "framework": "langgraph",
        "framework_version": "1.2.15",
        "checkpoint_kind": "MEMORY",
        "control_mode": "YIELD_TO_KERNEL",
        "max_model_calls_per_advance": 1,
        "profile_digest": DIGEST,
    }
    with pytest.raises(ValidationError):
        AgentRuntimeProfile.model_validate(payload)
