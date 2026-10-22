import subprocess
from pathlib import Path

import sqlalchemy as sa
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.model_decision import ModelDecisionDriver
from platform_agent_runtime.model_providers import DeterministicModelProvider
from platform_core.application.action_reconciliation import AgentActionReconciler
from platform_core.application.action_tools import ActionToolGateway
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.tool_catalog import build_tool_catalog
from platform_infrastructure.services import ExecutionClient, Remote

from tests.p2.processes import eventually
from tests.p3.test_execution import proxy as service_proxy
from tests.p62.conftest import model_profile
from tests.p432.scenario import prepare_mixed
from tests.p432.test_mixed_faults import accepted_remote, remote_child

USAGE = {
    "input_tokens": 100,
    "output_tokens": 40,
    "reasoning_tokens": 0,
    "cost_microunits": 1,
}


def _workflow_decision(package_id):
    def decide(call):
        frame = call["request"]["frame"]
        return {
            "output": {
                "kind": "ACTION_REQUESTS",
                "frame_id": frame["frame_id"],
                "expected_task_revision": frame["task_spec_revision"],
                "requests": [
                    {
                        "call_id": "submit-frozen-mixed-workflow",
                        "tool_id": "execution.submit_workflow",
                        "tool_version": "1.0.0",
                        "purpose": "Run the frozen local and remote workflow",
                        "arguments": {"package_id": package_id},
                        "expected_observation": "A stable workflow command receipt",
                    }
                ],
            }
        }

    return decide


def _agent_worker(cluster, provider, selected, runtime, owner):
    driver = ModelDecisionDriver(cluster.database, provider, selected)
    runtime._decide = driver
    catalog = build_tool_catalog("science")
    data = Remote(cluster.env["V7_DATA_URL"], cluster.env["V7_PLATFORM_DATA_TOKEN"])
    execution = ExecutionClient(
        cluster.env["V7_EXECUTION_URL"], cluster.env["V7_PLATFORM_EXECUTION_TOKEN"]
    )
    worker = KernelWorker(
        cluster.database,
        runtime,
        owner,
        model_decision=driver,
        context_compiler=ContextCompiler(catalog, max_context_tokens=20_000),
        action_tools=ActionToolGateway(
            cluster.database,
            catalog,
            PlatformActionTools(cluster.database, data=data, execution=execution),
        ),
        action_reconciler=AgentActionReconciler(cluster.database),
    )
    return worker, data, execution


def test_agent_combined_response_loss_and_mixed_runtime_recovery_keep_one_identity(
    fault_remote_cluster, owner, database
):
    cluster, peer, configure, original_peer = fault_remote_cluster
    controller = original_peer[5]
    assert controller is not None, "combined recovery requires a controllable peer"
    selected = model_profile()
    scenario = prepare_mixed(
        cluster,
        peer,
        database,
        owner[1],
        key="p69-agent-combined-recovery",
        agent_actor=owner[0],
        agent_model_profile=selected,
        submit=False,
    )
    provider = DeterministicModelProvider(
        [
            {
                "status": "UNKNOWN",
                "executed": None,
                "provider_ref": "p69-lost-model-response",
                "response": None,
                "usage": None,
            },
            {
                "status": "COMPLETED",
                "executed": True,
                "provider_ref": "p69-lost-model-response",
                "response_factory": _workflow_decision(
                    scenario["workflow"]["package_ref"]["package_id"]
                ),
                "usage": USAGE,
            },
        ]
    )
    runtime = LangGraphRuntime(
        ModelDecisionDriver(cluster.database, provider, selected), InMemorySaver()
    )
    first, first_data, first_execution = _agent_worker(
        cluster, provider, selected, runtime, "p69-agent-worker-before-loss"
    )
    try:
        assert first.process_one()
    finally:
        first_data.close()
        first_execution.close()

    # The first worker is abandoned after the provider accepted the stable call
    # but before any proposal or business Action was written. A new worker must
    # query that same model call and submit the frozen workflow exactly once.
    second, second_data, second_execution = _agent_worker(
        cluster, provider, selected, runtime, "p69-agent-worker-after-loss"
    )
    try:
        assert second.process_one()
    finally:
        second_data.close()
        second_execution.close()
    assert provider.submit_count == provider.query_count == 1

    with cluster.database.transaction() as uow:
        run = uow.agents.run_by_task(scenario["task"]["task_id"], required=True)
        calls = uow.agents.model_calls(run["agent_run_id"])
        links = uow.agents.action_links(run["agent_run_id"])
        observations = uow.agents.tool_observations(run["agent_run_id"])
    assert len(calls) == len(links) == len(observations) == 1
    assert links[0]["operation"] == "SUBMIT_WORKFLOW"
    receipt = observations[0]["result"]
    assert receipt["action_id"] == links[0]["action_id"]
    command = receipt | {"status_url": cluster.p + "/commands/" + receipt["command_id"]}

    # Combine the already accepted Agent Action with a lost remote submit
    # response, local worker/Toil leader termination, peer restart and a lost
    # Data commit response. All recovery paths must retain their original IDs.
    configure(drop_submit_response_once=True, query_404_always=True)
    data_url, data_commit_dropped = service_proxy(cluster, "data", "/commit")
    cluster.env["V7_DATA_URL"] = data_url
    execution_worker = cluster.launch("execution-worker")
    accepted = eventually(lambda: accepted_remote(cluster), timeout=150)

    def recoverable_parent():
        value = cluster.request("GET", cluster.p + "/executions/" + accepted["parent_execution_id"])
        root = Path(cluster.env["V7_EXECUTION_WORKDIR"]) / value["execution_id"]
        return (
            value
            if value["runtime"].get("phase") == "ENGINE_RUNNING"
            and (root / "jobstore/shared/rootJobStoreID").is_file()
            else None
        )

    parent = eventually(recoverable_parent, timeout=60)
    cluster.kill(execution_worker)
    engine = f"v7-p4-{parent['execution_id']}-e{parent['runtime']['engine_epoch']}"
    subprocess.run(["docker", "kill", engine], check=True, capture_output=True, timeout=10)
    controller["restart"]()
    configure()
    cluster.launch("execution-worker")
    eventually(data_commit_dropped.exists, timeout=180)
    cluster.restart("execution-worker")
    result = cluster.completed_execution(command, timeout=240)
    assert result["state"] == "SUCCEEDED", result
    assert result["runtime"]["publication"] == "COMMITTED"

    child = remote_child(cluster, result["execution_id"])
    assert child["action_id"] == accepted["action_id"]
    assert child["runtime"]["remote"]["remote_execution_id"] == accepted["remote_execution_id"]
    data_db, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("select count(*) from remote_calls where action_id=:action"),
                {"action": child["action_id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text("select count(*) from executions where parent_execution_id=:parent"),
                {"parent": result["execution_id"]},
            )
            == 2
        )
    with data_db.engine.connect() as connection:
        publication = (
            connection.execute(
                sa.text("select * from publications where execution_id=:execution"),
                {"execution": result["execution_id"]},
            )
            .mappings()
            .one()
        )
    assert publication["state"] == "COMMITTED"
    assert len(publication["receipt"]["assets"]) == 2
    budget = cluster.request("GET", cluster.p + f"/tasks/{scenario['task']['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 3
