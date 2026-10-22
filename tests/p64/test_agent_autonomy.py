import time
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.model_decision import ModelDecisionDriver
from platform_agent_runtime.model_providers import DeterministicModelProvider
from platform_agent_runtime.profiles import profile
from platform_core.application.action_reconciliation import AgentActionReconciler
from platform_core.application.action_tools import ActionToolGateway
from platform_core.application.catalog import OPERATORS, target
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.control import ControlService
from platform_core.application.model_catalog import build_catalog
from platform_core.application.tool_catalog import build_tool_catalog
from platform_infrastructure.services import ExecutionClient, Remote

from tests.p62.conftest import model_budget, model_profile


def decision(call):
    frame = call["request"]["frame"]
    context = call["request"]["context"]
    base = {
        "frame_id": frame["frame_id"],
        "expected_task_revision": frame["task_spec_revision"],
    }
    task = context["authoritative"]["task_control"]
    if task["lifecycle"] == "COMPLETED":
        return {
            "output": {
                **base,
                "kind": "EXPLANATION",
                "message": "Both frozen operators and the independent acceptance check passed.",
                "cited_refs": [],
            }
        }

    contents = [value["content"] for value in context["observations"]]
    commands = {
        value["action_id"]: value
        for value in contents
        if isinstance(value, dict) and "command_id" in value and "action_id" in value
    }
    action_facts = {
        value["action_id"]: value
        for value in contents
        if isinstance(value, dict) and value.get("kind") == "ACTION_STATUS"
    }
    deliverables = [
        value
        for value in contents
        if isinstance(value, dict) and "deliverable_id" in value and "payload" in value
    ]
    acceptances = [
        value
        for value in contents
        if isinstance(value, dict) and "acceptance_id" in value and "status_url" in value
    ]

    if len(commands) < 2:
        return {
            "output": {
                **base,
                "kind": "ACTION_REQUESTS",
                "requests": [
                    {
                        "call_id": "submit-completeness",
                        "tool_id": "execution.submit_operator",
                        "tool_version": "1.0.0",
                        "purpose": "Run the required completeness operator",
                        "arguments": {"operator_id": "timeseries-completeness"},
                        "expected_observation": "A stable command receipt",
                    },
                    {
                        "call_id": "submit-trend",
                        "tool_id": "execution.submit_operator",
                        "tool_version": "1.0.0",
                        "purpose": "Run the required descriptive trend operator",
                        "arguments": {"operator_id": "statistics-linear-trend"},
                        "expected_observation": "A stable command receipt",
                    },
                ],
            }
        }

    succeeded = [
        value
        for value in action_facts.values()
        if value["action_state"] == "SUCCEEDED" and value["execution_id"]
    ]
    if len(succeeded) < 2:
        return {
            "output": {
                **base,
                "kind": "WAIT_REQUEST",
                "reason": "EXECUTION",
                "dependencies": [
                    {"owner": "EXECUTION", "object_id": value} for value in sorted(commands)
                ],
            }
        }

    if not deliverables:
        execution_ids = sorted(value["execution_id"] for value in succeeded)
        return {
            "output": {
                **base,
                "kind": "ACTION_REQUESTS",
                "requests": [
                    {
                        "call_id": "register-deliverable",
                        "tool_id": "deliverable.register",
                        "tool_version": "1.0.0",
                        "purpose": "Bind both published outputs into one candidate",
                        "arguments": {"execution_ids": execution_ids},
                        "expected_observation": "An immutable deliverable receipt",
                    }
                ],
            }
        }

    if not acceptances:
        return {
            "output": {
                **base,
                "kind": "ACTION_REQUESTS",
                "requests": [
                    {
                        "call_id": "request-acceptance",
                        "tool_id": "acceptance.request",
                        "tool_version": "1.0.0",
                        "purpose": "Run the frozen independent acceptance check",
                        "arguments": {"deliverable_id": deliverables[-1]["deliverable_id"]},
                        "expected_observation": "A stable acceptance receipt",
                    }
                ],
            }
        }

    return {
        "output": {
            **base,
            "kind": "WAIT_REQUEST",
            "reason": "EXECUTION",
            "dependencies": [
                {
                    "owner": "ACCEPTANCE",
                    "object_id": acceptances[-1]["acceptance_id"],
                }
            ],
        }
    }


def test_deterministic_agent_runs_p3_to_completion_and_explains(cluster, owner):
    actor, token = owner
    cluster.bootstrap(token)
    selected = model_profile()
    control = ControlService(
        cluster.database,
        agent_runtime_profile=profile(),
        agent_model_catalog=build_catalog([selected], selected["profile_id"]),
    )
    task = control.create_task(
        actor,
        cluster.project,
        "p64-autonomous-task",
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Compute the frozen completeness and trend outputs",
                "requirements": [],
                "acceptance_spec": cluster.spec,
            },
            "budget": {"execution_count": 3, "max_in_flight": 2},
            "model_budget": model_budget(max_model_calls=30),
        },
    )
    control.grant(
        actor,
        cluster.project,
        "p64-autonomous-grant",
        {
            "task_id": task["task_id"],
            "targets": [target(value) for value in OPERATORS],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": ["p3-timeseries-v1"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    data = Remote(cluster.env["V7_DATA_URL"], cluster.env["V7_PLATFORM_DATA_TOKEN"])
    execution = ExecutionClient(
        cluster.env["V7_EXECUTION_URL"], cluster.env["V7_PLATFORM_EXECUTION_TOKEN"]
    )
    catalog = build_tool_catalog("actions")
    provider = DeterministicModelProvider(
        [
            {
                "status": "COMPLETED",
                "executed": True,
                "response_factory": decision,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "reasoning_tokens": 0,
                    "cost_microunits": 1,
                },
            }
            for _ in range(20)
        ]
    )
    driver = ModelDecisionDriver(cluster.database, provider, selected)
    worker = KernelWorker(
        cluster.database,
        LangGraphRuntime(driver, InMemorySaver()),
        "p64-autonomous-worker",
        model_decision=driver,
        context_compiler=ContextCompiler(catalog, max_context_tokens=20_000),
        action_tools=ActionToolGateway(
            cluster.database,
            catalog,
            PlatformActionTools(cluster.database, data=data, execution=execution),
        ),
        action_reconciler=AgentActionReconciler(cluster.database),
    )
    cluster.launch("platform-worker")
    cluster.launch("execution-worker")
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            worker.process_one()
            with cluster.database.transaction() as uow:
                run = uow.agents.run_by_task(task["task_id"], required=True)
                proposals = uow.agents.proposals(run["agent_run_id"])
                current = uow.tasks.get(cluster.project, task["task_id"])
            if current.lifecycle == "COMPLETED" and any(
                value["kind"] == "EXPLANATION" for value in proposals
            ):
                break
            time.sleep(0.05)
        else:
            with cluster.database.transaction() as uow:
                run = uow.agents.run_by_task(task["task_id"], required=True)
                details = {
                    "run": run,
                    "proposals": [
                        value["kind"] for value in uow.agents.proposals(run["agent_run_id"])
                    ],
                    "links": uow.agents.action_links(run["agent_run_id"]),
                    "events": uow.agents.events(run["agent_run_id"]),
                    "tools": uow.agents.tool_observations(run["agent_run_id"]),
                    "task": uow.tasks.get(cluster.project, task["task_id"]),
                }
            raise AssertionError(f"Agent closed loop did not converge: {details}")

        with cluster.database.transaction() as uow:
            run = uow.agents.run_by_task(task["task_id"], required=True)
            proposals = uow.agents.proposals(run["agent_run_id"])
            links = uow.agents.action_links(run["agent_run_id"])
            actions = [
                uow.actions.get(value["action_id"]) for value in uow.actions.list(task["task_id"])
            ]
        assert proposals[-1]["kind"] == "EXPLANATION"
        assert [value["kind"] for value in proposals].count("WAIT_REQUEST") == 2
        assert [value["operation"] for value in links].count("SUBMIT_OPERATOR") == 2
        assert len(links) == 4
        assert len(actions) == 3
        assert all(value["decision_source"] == "AGENT" for value in actions)
        with cluster.database.transaction() as uow:
            waits = uow.agents.wait_dependencies(run["agent_run_id"])
        assert waits and all(value["state"] == "SATISFIED" for value in waits)
        budget = control.budget(actor, cluster.project, task["task_id"])
        assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
    finally:
        data.close()
        execution.close()
