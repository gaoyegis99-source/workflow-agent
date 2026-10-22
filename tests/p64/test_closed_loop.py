from datetime import UTC, datetime, timedelta

from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.profiles import profile
from platform_core.application.catalog import OPERATORS, target
from platform_core.application.control import ControlService
from platform_core.application.model_catalog import build_catalog
from platform_infrastructure.services import ExecutionClient, Remote

from tests.p2.processes import eventually
from tests.p62.conftest import model_budget, model_profile


def test_agent_tools_complete_real_p3_closed_loop(cluster, owner):
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
        "p64-agent-closed-loop",
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Compute the frozen completeness and trend outputs",
                "requirements": [],
                "acceptance_spec": cluster.spec,
            },
            "budget": {"execution_count": 3, "max_in_flight": 2},
            "model_budget": model_budget(max_model_calls=12),
        },
    )
    targets = [target(value) for value in OPERATORS]
    control.grant(
        actor,
        cluster.project,
        "p64-agent-grant",
        {
            "task_id": task["task_id"],
            "targets": targets,
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": ["p3-timeseries-v1"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    with cluster.database.transaction() as uow:
        run = uow.agents.run_by_task(task["task_id"], required=True)
    context = {
        "project_id": cluster.project,
        "task_id": task["task_id"],
        "task_spec_revision": 1,
        "principal_id": actor.principal_id,
        "agent_run_id": run["agent_run_id"],
        "proposal_id": "p64-e2e-proposal",
        "frame_id": "p64-e2e-frame",
    }
    data = Remote(cluster.env["V7_DATA_URL"], cluster.env["V7_PLATFORM_DATA_TOKEN"])
    execution = ExecutionClient(
        cluster.env["V7_EXECUTION_URL"], cluster.env["V7_PLATFORM_EXECUTION_TOKEN"]
    )
    backend = PlatformActionTools(cluster.database, data=data, execution=execution)
    try:
        submitted = [
            backend.execute(
                "execution.submit_operator",
                {"operator_id": operator_id},
                context,
                "p64-submit-" + operator_id,
            )
            for operator_id in ("timeseries-completeness", "statistics-linear-trend")
        ]
        from platform_infrastructure.worker import Worker

        dispatcher = Worker(cluster.database, execution)
        for _ in range(20):
            dispatcher.process_one()
            if all(
                control.command(actor, cluster.project, value.command_id)["status"] == "RESOLVED"
                for value in submitted
            ):
                break
        command_states = [
            control.command(actor, cluster.project, value.command_id) for value in submitted
        ]
        with cluster.database.transaction() as uow:
            action_states = uow.actions.list(task["task_id"])
        assert all(value["status"] == "RESOLVED" for value in command_states), str(
            [
                {
                    "command": value,
                    "action": action_states[index],
                }
                for index, value in enumerate(command_states)
            ]
        )
        cluster.launch("platform-worker")
        cluster.launch("execution-worker")

        def command_execution(result):
            record = control.command(actor, cluster.project, result.command_id)
            return record["execution_id"] if record["status"] == "RESOLVED" else None

        execution_ids = [
            eventually(lambda result=result: command_execution(result), timeout=40)
            for result in submitted
        ]

        def execution_done(execution_id):
            value = backend.science.execution_view(actor, cluster.project, execution_id)
            terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}
            return value if value["state"] in terminal else None

        executions = [
            eventually(lambda value=value: execution_done(value), timeout=60)
            for value in execution_ids
        ]
        assert [value["state"] for value in executions] == ["SUCCEEDED", "SUCCEEDED"]
        eventually(
            lambda: control.budget(actor, cluster.project, task["task_id"])["in_flight"] == 0,
            timeout=30,
        )

        deliverable = backend.execute(
            "deliverable.register",
            {"execution_ids": execution_ids},
            context,
            "p64-register-deliverable",
        )
        acceptance = backend.execute(
            "acceptance.request",
            {"deliverable_id": deliverable.business_id},
            context,
            "p64-request-acceptance",
        )

        def accepted():
            value = backend.science.checks(
                actor, cluster.project, task["task_id"], acceptance.business_id
            )[0]
            return value if value["status"] != "QUEUED" else None

        report = eventually(accepted, timeout=60)
        assert report["status"] == "PASS", report
        completed = control.task(actor, cluster.project, task["task_id"])
        assert completed["lifecycle"] == "COMPLETED"
        budget = control.budget(actor, cluster.project, task["task_id"])
        assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
        with cluster.database.transaction() as uow:
            actions = [
                uow.actions.get(value["action_id"]) for value in uow.actions.list(task["task_id"])
            ]
        assert len(actions) == 3
        assert all(value["decision_source"] == "AGENT" for value in actions)
        assert all(value["token_id"] is None for value in actions)
    finally:
        data.close()
        execution.close()
