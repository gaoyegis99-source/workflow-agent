import pytest
from platform_agent_runtime.profiles import profile as runtime_profile
from platform_core.application.control import ControlService
from platform_core.application.model_catalog import build_catalog
from platform_core.domain.agent_runtime import canonical_digest

from tests.p2.conftest import database, owner, postgres  # noqa: F401


def model_profile(provider="DETERMINISTIC_FAKE"):
    unsigned = {
        "profile_id": "approved-model-v1",
        "provider": provider,
        "model": "test-model-snapshot-1",
        "request_mode": "RESPONSES",
        "supports_structured_output": True,
        "supports_query_by_response_id": True,
        "data_policy": "LOCAL_TEST_ONLY",
        "max_context_tokens": 100_000,
        "max_output_tokens": 2_000,
        "max_reasoning_tokens": 500,
        "timeout_seconds": 5,
        "pricing_revision": "test-1",
        "pricing": {
            "input_microunits_per_million": 10_000,
            "output_microunits_per_million": 20_000,
            "reasoning_microunits_per_million": 30_000,
        },
        "credential_env": "V7_TEST_MODEL_API_KEY",
        "base_url": "http://127.0.0.1:18099/v1",
    }
    return unsigned | {"profile_digest": canonical_digest(unsigned)}


def model_budget(max_model_calls=4):
    return {
        "max_model_calls": max_model_calls,
        "max_input_tokens": 100_000,
        "max_output_tokens": 10_000,
        "max_reasoning_tokens": 4_000,
        "max_estimated_cost_microunits": 1_000_000,
        "max_agent_wall_seconds": 3_600,
        "max_decision_frames": 20,
    }


@pytest.fixture
def model_agent_setup(database, owner):  # noqa: F811
    actor, token = owner
    selected = model_profile()
    catalog = build_catalog([selected], selected["profile_id"])
    control = ControlService(
        database,
        agent_runtime_profile=runtime_profile(),
        agent_model_catalog=catalog,
    )
    project_id = control.create_project(actor, "p62-project", "P6.2 model tests")["project_id"]
    task = control.create_task(
        actor,
        project_id,
        "p62-agent-task",
        {
            "execution_mode": "AGENT",
            "spec": {"goal": "Produce one deterministic model decision", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
            "model_budget": model_budget(),
        },
    )
    with database.transaction() as uow:
        run = uow.agents.run_by_task(task["task_id"], required=True)
    return {
        "database": database,
        "actor": actor,
        "token": token,
        "project_id": project_id,
        "task": task,
        "agent_run_id": run["agent_run_id"],
        "control": control,
        "profile": selected,
        "catalog": catalog,
    }
