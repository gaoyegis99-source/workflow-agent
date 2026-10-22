from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.model_decision import ModelDecisionDriver
from platform_agent_runtime.model_providers import DeterministicModelProvider
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.application.model_calls import AgentModelCoordinator
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables as t

USAGE = {
    "input_tokens": 120,
    "output_tokens": 40,
    "reasoning_tokens": 10,
    "cost_microunits": 7,
}


def plan_response(call):
    frame = call["request"]["frame"]
    return {
        "output": {
            "kind": "PLAN_REVISION",
            "frame_id": frame["frame_id"],
            "expected_task_revision": frame["task_spec_revision"],
            "reason": "Use a deterministic registered plan",
            "steps": [{"step_id": "inspect-task", "purpose": "Inspect the Task", "depends_on": []}],
        }
    }


def completed(**changes):
    return {
        "status": "COMPLETED",
        "executed": True,
        "response_factory": plan_response,
        "usage": USAGE,
    } | changes


def make_worker(setup, provider, *, saver=None, owner="model-worker"):
    driver = ModelDecisionDriver(setup["database"], provider, setup["profile"])
    runtime = LangGraphRuntime(driver, saver or InMemorySaver())
    worker = KernelWorker(
        setup["database"], runtime, owner, model_decision=driver, lease_seconds=30
    )
    return worker, driver, runtime


def saved(setup):
    with setup["database"].transaction() as uow:
        run = uow.agents.run_by_task(setup["task"]["task_id"], required=True)
        return {
            "run": run,
            "calls": uow.agents.model_calls(run["agent_run_id"]),
            "usage": uow.agents.model_usage(run["agent_run_id"]),
            "proposals": uow.agents.proposals(run["agent_run_id"]),
            "reconciliation": [
                dict(value)
                for value in uow.connection.execute(
                    sa.select(t.model_provider_reconciliation)
                    .join(
                        t.agent_model_calls,
                        t.agent_model_calls.c.model_call_id
                        == t.model_provider_reconciliation.c.model_call_id,
                    )
                    .where(t.agent_model_calls.c.agent_run_id == run["agent_run_id"])
                ).mappings()
            ],
        }


def test_completed_model_call_is_reserved_settled_and_accepted_once(model_agent_setup):
    provider = DeterministicModelProvider([completed()])
    worker, _, _ = make_worker(model_agent_setup, provider)
    assert worker.process_one()
    state = saved(model_agent_setup)
    assert state["run"]["status"] == "WAITING"
    assert len(state["calls"]) == len(state["proposals"]) == 1
    assert state["calls"][0]["status"] == "COMPLETED"
    assert state["usage"]["used_calls"] == 1
    assert state["usage"]["reserved_calls"] == 0
    assert state["usage"]["used_input_tokens"] == USAGE["input_tokens"]
    assert state["reconciliation"] == []
    assert provider.submit_count == 1


def test_response_loss_and_worker_takeover_query_original_call(model_agent_setup):
    provider = DeterministicModelProvider(
        [
            {
                "status": "UNKNOWN",
                "executed": None,
                "provider_ref": "response-loss-ref",
                "response": None,
                "usage": None,
            },
            completed(provider_ref="response-loss-ref"),
        ]
    )
    saver = InMemorySaver()
    first, _, runtime = make_worker(model_agent_setup, provider, saver=saver, owner="worker-one")
    assert first.process_one()
    interim = saved(model_agent_setup)
    assert len(interim["calls"]) == 1
    assert interim["calls"][0]["status"] == "UNKNOWN"
    assert interim["usage"]["reserved_calls"] == 1
    assert len(interim["reconciliation"]) == 1

    driver = ModelDecisionDriver(
        model_agent_setup["database"], provider, model_agent_setup["profile"]
    )
    runtime._decide = driver
    second = KernelWorker(
        model_agent_setup["database"],
        runtime,
        "worker-two",
        model_decision=driver,
    )
    assert second.process_one()
    final = saved(model_agent_setup)
    assert len(final["calls"]) == len(final["proposals"]) == 1
    assert final["calls"][0]["provider_ref"] == "response-loss-ref"
    assert final["usage"]["used_calls"] == 1
    assert provider.submit_count == provider.query_count == 1


def test_rate_limit_retries_same_model_call_and_reservation(model_agent_setup):
    provider = DeterministicModelProvider(
        [
            {
                "status": "RETRYABLE",
                "accepted": False,
                "executed": False,
                "error_code": "RATE_LIMITED",
            },
            completed(),
        ]
    )
    saver = InMemorySaver()
    worker, driver, runtime = make_worker(model_agent_setup, provider, saver=saver)
    assert worker.process_one()
    original = saved(model_agent_setup)["calls"][0]["model_call_id"]
    runtime._decide = driver
    assert worker.process_one()
    state = saved(model_agent_setup)
    assert len(state["calls"]) == 1
    assert state["calls"][0]["model_call_id"] == original
    assert state["usage"]["used_calls"] == 1
    assert provider.submit_count == 2


def test_completed_response_with_missing_usage_is_visible_and_conservative(model_agent_setup):
    provider = DeterministicModelProvider([completed(usage=None)])
    worker, _, _ = make_worker(model_agent_setup, provider)
    assert worker.process_one()
    state = saved(model_agent_setup)
    assert state["usage"]["used_calls"] == 1
    assert state["usage"]["unknown_usage_calls"] == 1
    assert state["usage"]["reserved_input_tokens"] > 0
    assert state["calls"][0]["usage_status"] == "UNKNOWN"
    assert len(state["reconciliation"]) == 1


def test_one_format_repair_is_a_distinct_budgeted_call(model_agent_setup):
    provider = DeterministicModelProvider(
        [
            {
                "status": "COMPLETED",
                "executed": True,
                "response": {"output": {"invalid": True}},
                "usage": USAGE,
            },
            completed(),
        ]
    )
    worker, _, _ = make_worker(model_agent_setup, provider)
    assert worker.process_one()
    state = saved(model_agent_setup)
    assert [call["attempt"] for call in state["calls"]] == [1, 2]
    assert state["calls"][0]["parse_errors"]
    assert state["calls"][1]["repair_of_model_call_id"] == state["calls"][0]["model_call_id"]
    assert state["usage"]["used_calls"] == 2
    assert len(state["proposals"]) == 1


def test_second_invalid_output_blocks_without_a_third_call(model_agent_setup):
    invalid = {
        "status": "COMPLETED",
        "executed": True,
        "response": {"output": {"invalid": True}},
        "usage": USAGE,
    }
    provider = DeterministicModelProvider([invalid, invalid])
    worker, _, _ = make_worker(model_agent_setup, provider)
    assert worker.process_one()
    state = saved(model_agent_setup)
    assert state["run"]["status"] == "BLOCKED"
    assert state["run"]["blocked_reason"] == "MODEL_OUTPUT_INVALID"
    assert len(state["calls"]) == 2
    assert state["proposals"] == []


def test_same_frame_concurrency_creates_one_call_and_one_reservation(model_agent_setup):
    coordinator = AgentCoordinator(model_agent_setup["database"])
    claim = coordinator.claim("concurrent-model-worker", 30)
    frame = coordinator.prepare_frame(claim)
    barrier = __import__("threading").Barrier(6)

    def reserve(_):
        barrier.wait()
        return AgentModelCoordinator(model_agent_setup["database"]).prepare_call(
            claim, frame, model_agent_setup["profile"]
        )["model_call_id"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        identities = list(pool.map(reserve, range(6)))
    assert len(set(identities)) == 1
    state = saved(model_agent_setup)
    assert len(state["calls"]) == 1
    assert state["usage"]["reserved_calls"] == 1


def test_old_worker_cannot_write_model_observation_after_takeover(model_agent_setup):
    coordinator = AgentCoordinator(model_agent_setup["database"])
    old = coordinator.claim("old-model-worker", 30)
    frame = coordinator.prepare_frame(old)
    call = AgentModelCoordinator(model_agent_setup["database"]).prepare_call(
        old, frame, model_agent_setup["profile"]
    )
    with model_agent_setup["database"].engine.begin() as connection:
        connection.execute(
            t.agent_leases.update()
            .where(t.agent_leases.c.agent_run_id == old.agent_run_id)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    replacement = coordinator.claim("new-model-worker", 30)
    assert replacement.generation > old.generation
    observation = DeterministicModelProvider([completed()]).submit(call)
    with pytest.raises(RuleViolation) as error:
        AgentModelCoordinator(model_agent_setup["database"]).observe(old, observation)
    assert error.value.code == "REVISION_CONFLICT"


def test_wall_clock_budget_blocks_a_new_model_call(model_agent_setup):
    setup = model_agent_setup
    with setup["database"].engine.begin() as connection:
        connection.execute(
            t.agent_model_budgets.update()
            .where(t.agent_model_budgets.c.agent_run_id == setup["agent_run_id"])
            .values(created_at=sa.func.clock_timestamp() - sa.text("interval '2 hours'"))
        )
    coordinator = AgentCoordinator(setup["database"])
    claim = coordinator.claim("expired-wall-budget", 30)
    frame = coordinator.prepare_frame(claim)
    with pytest.raises(RuleViolation) as error:
        AgentModelCoordinator(setup["database"]).prepare_call(claim, frame, setup["profile"])
    assert error.value.code == "BUDGET_BLOCKED"
    assert saved(setup)["calls"] == []
