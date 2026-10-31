import sqlalchemy as sa
from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.profiles import profile
from platform_core.application.catalog import build_catalog as build_operator_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.control import ControlService
from platform_core.application.model_catalog import build_catalog
from platform_core.application.tool_catalog import build_tool_catalog
from platform_infrastructure import database as database_module

from tests.agent.model.conftest import model_budget, model_profile


class UnusedRemote:
    def json(self, *args, **kwargs):
        raise AssertionError("This frozen research template has no external sources")


def frozen_task(setup):
    selected = model_profile()
    control = ControlService(
        setup["database"],
        agent_runtime_profile=profile(),
        agent_model_catalog=build_catalog([selected], selected["profile_id"]),
    )
    task = control.create_task(
        setup["actor"],
        setup["project_id"],
        "p66-science-task",
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Produce a registered descriptive analysis without inventing facts",
                "requirements": [],
                "agent_analysis": {
                    "question": "Describe the explicitly supplied observations.",
                    "research_objects": ["frozen task observations"],
                    "input_roles": [],
                    "methods": [],
                    "primary_metrics": ["declared descriptive outputs"],
                    "comparison_rules": [],
                    "data_splits": [],
                    "stopping_rules": ["stop after registered outputs are checked"],
                    "allowed_deviations": [],
                    "checker_versions": [],
                    "observed_sources": [],
                },
                "agent_claim": {
                    "proposition_type": "DESCRIPTIVE",
                    "statement": "A descriptive result will be evaluated by the frozen checks.",
                    "scope": {"objects": ["frozen task observations"]},
                    "limitations": ["This proposed claim is not yet independently supported."],
                    "sources": [],
                    "support_status": "PROPOSED",
                },
            },
            "budget": {"execution_count": 2, "max_in_flight": 1},
            "model_budget": model_budget(),
        },
    )
    with setup["database"].transaction() as uow:
        run = uow.agent_runs.run_by_task(task["task_id"], required=True)
    context = {
        "project_id": setup["project_id"],
        "task_id": task["task_id"],
        "task_spec_revision": 1,
        "principal_id": setup["actor"].principal_id,
        "agent_run_id": run["agent_run_id"],
        "proposal_id": "p66-proposal",
        "frame_id": "p66-frame",
    }
    return task, context


def test_science_catalog_is_additive_and_does_not_expose_payloads():
    catalog = build_tool_catalog("science")
    tools = {value["tool_id"]: value for value in catalog["tools"]}
    assert catalog["catalog_id"] == "p6-science-tools-v1"
    assert {
        "execution.submit_workflow",
        "research.create_analysis",
        "research.register_analysis",
        "research.create_claim",
    } <= set(tools)
    assert tools["research.create_analysis"]["arguments"]["required"] == []
    assert all(
        name not in tools["execution.submit_workflow"]["arguments"]["required"]
        for name in ("command", "image", "inputs", "parameters")
    )


def test_frozen_analysis_registration_and_proposed_claim_use_existing_p5_rules(
    model_agent_setup,
):
    _, context = frozen_task(model_agent_setup)
    backend = PlatformActionTools(
        model_agent_setup["database"],
        data=UnusedRemote(),
        execution=UnusedRemote(),
        knowledge=UnusedRemote(),
        operator_catalog=build_operator_catalog(),
        checker_catalog=build_checker_catalog(),
    )
    analysis = backend.execute("research.create_analysis", {}, context, "p66-create-analysis")
    assert analysis.operation == "CREATE_ANALYSIS"
    assert analysis.value["status"] == "EXPLORATORY"
    assert analysis.value["policy_decision"]["result"] == "NOT_APPLICABLE"

    registration = backend.execute(
        "research.register_analysis",
        {"revision_id": analysis.value["revision_id"]},
        context,
        "p66-register-analysis",
    )
    assert registration.operation == "REGISTER_ANALYSIS"
    assert registration.value["observation_watermark"] == []

    claim = backend.execute(
        "research.create_claim",
        {"registration_id": registration.value["registration_id"]},
        context,
        "p66-create-claim",
    )
    assert claim.operation == "CREATE_CLAIM"
    assert claim.value["support_status"] == "PROPOSED"
    assert claim.value["result_assertion"] is None


def test_p66_migration_is_current(database):
    assert database_module.SCHEMA_VERSION == "0026_worker_lane_leases"
    with database.engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0026_worker_lane_leases"
        )
        token = next(
            value
            for value in sa.inspect(connection).get_columns("acceptance_check_envelopes")
            if value["name"] == "token_id"
        )
        assert token["nullable"] is True


def test_agent_identity_runs_frozen_workflow_through_real_toil(p4_cluster, owner):
    from datetime import UTC, datetime, timedelta

    from platform_agent_runtime.profiles import profile
    from platform_contracts import DeploymentContext
    from platform_infrastructure.services import ExecutionClient, Remote

    from tests.execution.workflows.test_workflow_e2e import package
    from tests.platform.database.processes import eventually

    cluster = p4_cluster
    actor, token = owner
    cluster.bootstrap(token, budget=2)
    queued = cluster.request("POST", cluster.p + "/workflow-packages", package(cluster.targets))
    cluster.launch_platform_worker_lanes()

    def ready_package():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(ready_package, timeout=30)
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    workflow = {
        "target": workflow_target,
        "inputs": {
            "observations": cluster.selection,
            "parameters": cluster.parameters,
        },
        "max_node_executions": 2,
    }
    control = ControlService(cluster.database, agent_runtime_profile=profile())
    task = control.create_task(
        actor,
        cluster.project,
        "p66-agent-real-workflow",
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Run the exact frozen two-operator workflow",
                "requirements": [],
                "agent_workflow": workflow,
            },
            "budget": {"execution_count": 2, "max_in_flight": 2},
        },
    )
    control.grant(
        actor,
        cluster.project,
        "p66-agent-real-workflow-grant",
        {
            "task_id": task["task_id"],
            "targets": [workflow_target, *cluster.targets],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    with cluster.database.transaction() as uow:
        run = uow.agent_runs.run_by_task(task["task_id"], required=True)
    context = {
        "project_id": cluster.project,
        "task_id": task["task_id"],
        "task_spec_revision": 1,
        "principal_id": actor.principal_id,
        "agent_run_id": run["agent_run_id"],
        "proposal_id": "p66-real-workflow-proposal",
        "frame_id": "p66-real-workflow-frame",
    }
    data = Remote(
        cluster.env["V7_DATA_URL"],
        cluster.env["V7_PLATFORM_DATA_TOKEN"],
        deployment=DeploymentContext(),
    )
    execution = ExecutionClient(
        cluster.env["V7_EXECUTION_URL"],
        cluster.env["V7_PLATFORM_EXECUTION_TOKEN"],
        deployment=DeploymentContext(),
    )
    result = PlatformActionTools(
        cluster.database,
        data=data,
        execution=execution,
        operator_catalog=cluster.operator_catalog,
        checker_catalog=cluster.checker_catalog,
    ).execute(
        "execution.submit_workflow",
        {"package_id": frozen["package_ref"]["package_id"]},
        context,
        "p66-real-workflow-request",
    )
    cluster.launch("execution-worker")
    command = result.value | {"status_url": cluster.p + "/commands/" + result.value["command_id"]}
    completed = cluster.completed_execution(command, timeout=150)
    assert completed["state"] == "SUCCEEDED", completed
    steps = cluster.request(
        "GET", cluster.p + "/executions/" + completed["execution_id"] + "/steps"
    )["items"]
    assert len(steps) == 2
    assert all(step["state"] == "SUCCEEDED" for step in steps)
    with cluster.database.transaction() as uow:
        action = uow.actions.get(result.action_id)
        budget = uow.budgets.get(task["task_id"])
    assert action["decision_source"] == "AGENT"
    assert action["token_id"] is None
    assert action["frame_id"] == "p66-real-workflow-request"
    assert (budget.used, budget.reserved, budget.in_flight) == (2, 0, 0)


def test_agent_registers_exact_output_and_completes_real_open_acceptance(
    r2_cluster, owner, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from platform_agent_runtime.profiles import profile
    from platform_contracts import DeploymentContext
    from platform_core.application.catalog import OPEN_CHECK_TARGET
    from platform_infrastructure.services import ExecutionClient, Remote

    from tests.knowledge.checkers.test_composed_runs import analysis_request
    from tests.knowledge.identity.test_contracts import EXPECTED
    from tests.platform.database.processes import eventually

    cluster = r2_cluster
    actor, token = owner
    for name in ("V7_CHECKER_CATALOG", "V7_CHECKER_IMAGE", "V7_GDAL_CHECKER_IMAGE"):
        monkeypatch.setenv(name, cluster.env[name])
    cluster.bootstrap(token, budget=1, project_name="P6 Agent open acceptance")
    acceptance_spec = {
        "profile": "open-science-v1",
        "delivery_class": "VERIFIED",
        "requirements": [
            {
                "requirement_id": "reproduce",
                "description": "Independently reproduce the published trend",
                "necessary": True,
                "checks": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
            }
        ],
        "required_outputs": [{"role_id": "trend_result", "necessary": True, "expected": EXPECTED}],
        "allow_manual_review": False,
        "max_check_executions": 3,
    }
    frozen_analysis = analysis_request()
    frozen_analysis.pop("expected_spec_revision")
    control = ControlService(cluster.database, agent_runtime_profile=profile())
    task = control.create_task(
        actor,
        cluster.project,
        "p66-agent-open-acceptance",
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Verify the exact published trend with independent checkers",
                "requirements": [],
                "acceptance_spec": acceptance_spec,
                "agent_analysis": frozen_analysis,
            },
            "budget": {"execution_count": 3, "max_in_flight": 2},
        },
    )
    control.grant(
        actor,
        cluster.project,
        "p66-agent-open-grant",
        {
            "task_id": task["task_id"],
            "targets": [cluster.targets[1], OPEN_CHECK_TARGET],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    cluster.task = task["task_id"]
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    trend = cluster.completed_execution(cluster.submit(index=1, key="p66-agent-trend"), timeout=90)
    assert trend["state"] == "SUCCEEDED", trend
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")["in_flight"] == 0
        ),
        timeout=30,
    )
    with cluster.database.transaction() as uow:
        run = uow.agent_runs.run_by_task(task["task_id"], required=True)
    context = {
        "project_id": cluster.project,
        "task_id": task["task_id"],
        "task_spec_revision": 1,
        "principal_id": actor.principal_id,
        "agent_run_id": run["agent_run_id"],
        "proposal_id": "p66-open-proposal",
        "frame_id": "p66-open-frame",
    }
    backend = PlatformActionTools(
        cluster.database,
        data=Remote(
            cluster.env["V7_DATA_URL"],
            cluster.env["V7_PLATFORM_DATA_TOKEN"],
            deployment=DeploymentContext(),
        ),
        execution=ExecutionClient(
            cluster.env["V7_EXECUTION_URL"],
            cluster.env["V7_PLATFORM_EXECUTION_TOKEN"],
            deployment=DeploymentContext(),
        ),
        operator_catalog=cluster.operator_catalog,
        checker_catalog=cluster.checker_catalog,
    )
    analysis = backend.execute("research.create_analysis", {}, context, "p66-open-analysis")
    registration = backend.execute(
        "research.register_analysis",
        {"revision_id": analysis.value["revision_id"]},
        context,
        "p66-open-registration",
    )
    deliverable = backend.execute(
        "deliverable.register_exact",
        {
            "execution_id": trend["execution_id"],
            "role_id": "trend_result",
            "output_port": "result",
        },
        context,
        "p66-open-deliverable",
    )
    acceptance = backend.execute(
        "acceptance.request",
        {
            "deliverable_id": deliverable.value["deliverable_id"],
            "analysis_registration_id": registration.value["registration_id"],
        },
        context,
        "p66-open-acceptance",
    )

    def accepted():
        value = cluster.request(
            "GET", cluster.p + f"/tasks/{task['task_id']}/acceptance/{acceptance.business_id}"
        )
        return value if value["status"] != "QUEUED" else None

    report = eventually(accepted, timeout=150)
    assert report["status"] == "PASS", report
    assert [run["capability_id"] for run in report["check_runs"]] == [
        "platform-runtime-integrity-v1",
        "p3-timeseries-reproduction-v1",
    ]
    completed_task = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}")
    assert completed_task["lifecycle"] == "COMPLETED"


def test_agent_identity_runs_frozen_qgis_summary_map_workflow(p42_cluster, owner):
    from datetime import UTC, datetime, timedelta

    from platform_agent_runtime.profiles import profile
    from platform_contracts import DeploymentContext
    from platform_core.application.catalog import OPERATORS, target
    from platform_infrastructure.services import ExecutionClient, Remote

    from tests.spatial.cartography.test_cartography_workflow_e2e import bootstrap

    cluster = p42_cluster
    actor, token = owner
    selection, workflow_target = bootstrap(cluster, token)
    inputs = {
        "vector": selection,
        "buffer_parameters": {
            "distance": 100,
            "unit": "metre",
            "output_crs": "EPSG:32650",
            "segments": 8,
        },
        "summary_parameters": {"layer_id": "l0"},
        "map_parameters": {
            "layer_id": "l0",
            "template_id": "v7-spatial-basic-v1",
            "output_crs": "EPSG:32650",
            "title": "Agent 冻结制图成果",
            "legend": True,
            "palette": "blue",
        },
    }
    control = ControlService(cluster.database, agent_runtime_profile=profile())
    task = control.create_task(
        actor,
        cluster.project,
        "p66-agent-spatial-workflow",
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Run the frozen trusted spatial workflow",
                "requirements": [],
                "agent_workflow": {
                    "target": workflow_target,
                    "inputs": inputs,
                    "max_node_executions": 3,
                },
            },
            "budget": {"execution_count": 3, "max_in_flight": 2},
        },
    )
    items = {item["operator_id"]: item for item in OPERATORS}
    leaves = [
        target(items["qgis-buffer"]),
        target(items["spatial-vector-summary"]),
        target(items["qgis-static-map"]),
    ]
    control.grant(
        actor,
        cluster.project,
        "p66-agent-spatial-grant",
        {
            "task_id": task["task_id"],
            "targets": [workflow_target, *leaves],
            "resources": [selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    with cluster.database.transaction() as uow:
        run = uow.agent_runs.run_by_task(task["task_id"], required=True)
    context = {
        "project_id": cluster.project,
        "task_id": task["task_id"],
        "task_spec_revision": 1,
        "principal_id": actor.principal_id,
        "agent_run_id": run["agent_run_id"],
        "proposal_id": "p66-spatial-proposal",
        "frame_id": "p66-spatial-frame",
    }
    result = PlatformActionTools(
        cluster.database,
        data=Remote(
            cluster.env["V7_DATA_URL"],
            cluster.env["V7_PLATFORM_DATA_TOKEN"],
            deployment=DeploymentContext(),
        ),
        execution=ExecutionClient(
            cluster.env["V7_EXECUTION_URL"],
            cluster.env["V7_PLATFORM_EXECUTION_TOKEN"],
            deployment=DeploymentContext(),
        ),
        operator_catalog=cluster.operator_catalog,
        checker_catalog=cluster.checker_catalog,
    ).execute(
        "execution.submit_workflow",
        {"package_id": workflow_target["package_ref"]["package_id"]},
        context,
        "p66-spatial-workflow-request",
    )
    cluster.launch("execution-worker")
    command = result.value | {"status_url": cluster.p + "/commands/" + result.value["command_id"]}
    completed = cluster.completed_execution(command, timeout=220)
    assert completed["state"] == "SUCCEEDED", completed
    steps = cluster.request("GET", cluster.p + f"/executions/{completed['execution_id']}/steps")[
        "items"
    ]
    assert {tuple(item["call_path"]) for item in steps} == {
        ("buffer",),
        ("summary",),
        ("map",),
    }
    assert all(item["state"] == "SUCCEEDED" for item in steps)
    assert completed["runtime"]["publication_receipt"]["state"] == "COMMITTED"
    with cluster.database.transaction() as uow:
        action = uow.actions.get(result.action_id)
        budget = uow.budgets.get(task["task_id"])
    assert action["decision_source"] == "AGENT" and action["token_id"] is None
    assert (budget.used, budget.reserved, budget.in_flight) == (3, 0, 0)
