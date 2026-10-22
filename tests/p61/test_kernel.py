from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.profiles import profile
from platform_api.app import create_app
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.application.control import ControlService
from platform_core.domain.agent_runtime import DecisionProposal
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables as t


class PlanDecision:
    def __init__(self):
        self.calls = []

    def __call__(self, frame, observations):
        self.calls.append(frame.frame_id)
        return DecisionProposal(
            "PLAN_REVISION",
            frame.frame_id,
            frame.task_spec_revision,
            {
                "reason": "Start with an explicit deterministic step",
                "steps": [
                    {"step_id": "inspect-task", "purpose": "Inspect requirements", "depends_on": []}
                ],
            },
        )


class QuestionDecision:
    def __call__(self, frame, observations):
        return DecisionProposal(
            "CLARIFICATION_REQUEST",
            frame.frame_id,
            frame.task_spec_revision,
            {
                "gap_id": "missing-unit",
                "question": "Which unit applies?",
                "why_needed": "The method requires an explicit unit",
                "answer_schema": {"type": "string"},
                "blocked_actions": [],
            },
        )


def view(setup):
    return AgentCoordinator(setup["database"]).view(
        setup["actor"], setup["project_id"], setup["agent_run_id"]
    )


def run_once(setup, decision=None, owner="agent-worker-1"):
    decision = decision or PlanDecision()
    runtime = LangGraphRuntime(decision, InMemorySaver())
    assert KernelWorker(setup["database"], runtime, owner).process_one()
    return runtime, decision


def proposal(frame, kind="PLAN_REVISION"):
    body = (
        {"reason": "replace", "steps": [{"step_id": "s1", "purpose": "p", "depends_on": []}]}
        if kind == "PLAN_REVISION"
        else {"message": "observed"}
    )
    return DecisionProposal(kind, frame.frame_id, frame.task_spec_revision, body)


def test_agent_task_is_test_enabled_only_and_creates_one_ledger(database, owner):
    actor, token = owner
    direct = ControlService(database)
    project = direct.create_project(actor, "p61-disabled-project", "disabled")["project_id"]
    body = {
        "execution_mode": "AGENT",
        "spec": {"goal": "Agent task", "requirements": []},
        "budget": {"execution_count": 1, "max_in_flight": 1},
    }
    with pytest.raises(RuleViolation) as error:
        direct.create_task(actor, project, "disabled-agent", body)
    assert error.value.code == "UNSUPPORTED_TARGET"

    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "enabled-agent"}
    with TestClient(create_app(database, agent_runtime_profile=profile())) as client:
        response = client.post(f"/api/v1/projects/{project}/tasks", json=body, headers=headers)
        assert response.status_code == 201, response.text
        assert response.json()["execution_mode"] == "AGENT"
        assert (
            client.post(f"/api/v1/projects/{project}/tasks", json=body, headers=headers).json()
            == response.json()
        )
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.agent_runs)) == 1


def test_one_frame_yields_one_plan_and_no_business_action(agent_setup):
    _, decision = run_once(agent_setup)
    saved = view(agent_setup)
    assert saved["status"] == "WAITING"
    assert saved["plan_revision"] == 1
    assert len(saved["frames"]) == len(saved["proposals"]) == 1
    assert saved["frames"][0]["state"] == "ACCEPTED"
    assert saved["proposals"][0]["kind"] == "PLAN_REVISION"
    assert len(decision.calls) == 1
    with agent_setup["database"].engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.actions)) == 0


def test_accepted_proposal_response_loss_replays_without_a_second_plan(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    claim = coordinator.claim("worker-response-loss", 30)
    frame = coordinator.prepare_frame(claim)
    decision = proposal(frame)
    accepted = coordinator.accept_proposal(claim, frame, decision)
    replay = coordinator.accept_proposal(claim, frame, decision)
    assert replay["proposal_id"] == accepted["proposal_id"]
    saved = view(agent_setup)
    assert len(saved["proposals"]) == 1
    with agent_setup["database"].engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(t.agent_plan_revisions)) == 1
        )


def test_two_workers_compete_for_one_lease_and_expired_lease_is_taken_over(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    barrier = Barrier(2)

    def compete(owner):
        barrier.wait()
        return coordinator.claim(owner, 30)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(compete, ["worker-one", "worker-two"]))
    winner = next(value for value in claims if value is not None)
    assert sum(value is not None for value in claims) == 1

    with agent_setup["database"].engine.begin() as connection:
        connection.execute(
            t.agent_leases.update()
            .where(t.agent_leases.c.agent_run_id == winner.agent_run_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    replacement = coordinator.claim("worker-replacement", 30)
    assert replacement is not None
    assert replacement.agent_run_id == winner.agent_run_id
    assert replacement.generation > winner.generation


def test_old_generation_and_task_revision_cannot_write(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    claim = coordinator.claim("worker-stale", 30)
    frame = coordinator.prepare_frame(claim)
    revised = agent_setup["control"].revise(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        "revise-agent-task",
        {
            "expected_control_version": 1,
            "expected_spec_revision": 1,
            "spec": {"goal": "Revised deterministic plan", "requirements": []},
        },
    )
    assert revised["spec_revision"] == 2
    with pytest.raises(RuleViolation) as error:
        coordinator.accept_proposal(claim, frame, proposal(frame))
    assert error.value.code == "REVISION_CONFLICT"
    saved = view(agent_setup)
    assert saved["frames"][0]["state"] == "STALE"
    assert saved["proposals"] == []
    assert saved["status"] == "READY"


def test_question_answer_and_revision_order_are_explicit(agent_setup):
    run_once(agent_setup, QuestionDecision())
    saved = view(agent_setup)
    question = saved["questions"][0]
    answer = AgentCoordinator(agent_setup["database"]).answer_question(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["agent_run_id"],
        question["question_id"],
        "answer-unit",
        1,
        {"value": "metre"},
    )
    replay = AgentCoordinator(agent_setup["database"]).answer_question(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["agent_run_id"],
        question["question_id"],
        "answer-unit",
        1,
        {"value": "metre"},
    )
    assert replay["answer_id"] == answer["answer_id"]
    revised = agent_setup["control"].revise(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        "revise-after-answer",
        {
            "expected_control_version": 1,
            "expected_spec_revision": 1,
            "spec": {"goal": "Use the answered unit", "requirements": []},
        },
    )
    assert revised["spec_revision"] == 2
    saved = view(agent_setup)
    assert saved["questions"][0]["status"] == "ANSWERED"
    assert saved["task_spec_revision"] == 2 and saved["status"] == "READY"


def test_revision_before_answer_rejects_old_answer(agent_setup):
    run_once(agent_setup, QuestionDecision())
    question = view(agent_setup)["questions"][0]
    agent_setup["control"].revise(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        "revise-before-answer",
        {
            "expected_control_version": 1,
            "expected_spec_revision": 1,
            "spec": {"goal": "Changed question scope", "requirements": []},
        },
    )
    with pytest.raises(RuleViolation) as error:
        AgentCoordinator(agent_setup["database"]).answer_question(
            agent_setup["actor"],
            agent_setup["project_id"],
            agent_setup["agent_run_id"],
            question["question_id"],
            "late-answer",
            1,
            {"value": "metre"},
        )
    assert error.value.code == "REVISION_CONFLICT"


def test_duplicate_out_of_order_and_conflicting_events(agent_setup):
    run_once(agent_setup)
    coordinator = AgentCoordinator(agent_setup["database"])
    values = dict(
        project_id=agent_setup["project_id"],
        task_id=agent_setup["task"]["task_id"],
        source="EXECUTION",
        stream_id="execution-one",
        kind="EXECUTION_CHANGED",
    )
    assert coordinator.register_event(
        **values, event_id="execution-event-2", source_revision=2, payload={"state": "RUNNING"}
    )
    assert not coordinator.register_event(
        **values, event_id="execution-event-2", source_revision=2, payload={"state": "RUNNING"}
    )
    assert not coordinator.register_event(
        **values, event_id="execution-event-1", source_revision=1, payload={"state": "QUEUED"}
    )
    with pytest.raises(RuleViolation) as error:
        coordinator.register_event(
            **values,
            event_id="execution-event-conflict",
            source_revision=2,
            payload={"state": "FAILED"},
        )
    assert error.value.code == "INVALID_BINDING"
    saved = view(agent_setup)
    assert len(saved["events"]) == 1
    assert saved["events"][0]["source_revision"] == 2
    assert saved["status"] == "READY"


def test_event_arrival_fences_in_progress_frame(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    claim = coordinator.claim("worker-event-race", 30)
    frame = coordinator.prepare_frame(claim)
    assert coordinator.register_event(
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        source="DATA",
        event_id="data-event-1",
        stream_id="asset-one",
        source_revision=1,
        kind="ASSET_CHANGED",
        payload={"state": "READY"},
    )
    with pytest.raises(RuleViolation) as error:
        coordinator.accept_proposal(claim, frame, proposal(frame))
    assert error.value.code == "REVISION_CONFLICT"
    assert view(agent_setup)["frames"][0]["state"] == "STALE"


def test_pause_resume_and_cancel_fence_agent_progress(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    claim = coordinator.claim("worker-control-race", 30)
    frame = coordinator.prepare_frame(claim)
    agent_setup["control"].control(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        "pause",
        "pause-agent",
        1,
    )
    assert coordinator.claim("worker-paused", 30) is None
    with pytest.raises(RuleViolation):
        coordinator.accept_proposal(claim, frame, proposal(frame))
    agent_setup["control"].control(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        "resume",
        "resume-agent",
        2,
    )
    resumed = coordinator.claim("worker-resumed", 30)
    assert resumed is not None
    agent_setup["control"].control(
        agent_setup["actor"],
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        "cancel",
        "cancel-agent",
        3,
    )
    assert coordinator.claim("worker-cancelled", 30) is None
    assert view(agent_setup)["status"] == "STOPPED"
