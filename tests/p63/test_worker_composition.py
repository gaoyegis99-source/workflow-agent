from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.model_providers import DeterministicModelProvider
from platform_agent_runtime.profiles import profile
from platform_agent_runtime.worker import compose_worker
from platform_core.application.tool_catalog import build_tool_catalog


def test_production_worker_composes_frozen_context_model_and_local_read(model_agent_setup):
    def response(call):
        frame = call["request"]["frame"]
        return {
            "output": {
                "kind": "READ_TOOL_REQUESTS",
                "frame_id": frame["frame_id"],
                "expected_task_revision": frame["task_spec_revision"],
                "requests": [
                    {
                        "call_id": "task-state",
                        "tool_id": "task.read_current",
                        "tool_version": "1.0.0",
                        "purpose": "Read current Task state",
                        "arguments": {},
                        "expected_observation": "Current state and budget",
                    }
                ],
            }
        }

    provider = DeterministicModelProvider(
        [
            {
                "status": "COMPLETED",
                "executed": True,
                "response_factory": response,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 30,
                    "reasoning_tokens": 0,
                    "cost_microunits": 1,
                },
            }
        ]
    )
    worker = compose_worker(
        model_agent_setup["database"],
        InMemorySaver(),
        profile(),
        model_agent_setup["profile"],
        provider,
        build_tool_catalog(),
        {"data": None, "knowledge": None, "execution": None},
        "composed-agent-worker",
    )
    assert worker.process_one()
    with model_agent_setup["database"].transaction() as uow:
        observations = uow.agents.tool_observations(model_agent_setup["agent_run_id"])
    assert observations[0]["result"]["lifecycle"] == "OPEN"
    assert observations[0]["contains_untrusted_data"] is False
