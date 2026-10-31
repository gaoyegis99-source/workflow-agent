import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from platform_agent_runtime.action_tools import PlatformActionTools
from platform_agent_runtime.profiles import profile
from platform_contracts import DeploymentContext
from platform_core.application.catalog import OPEN_CHECK_TARGET, target
from platform_core.application.control import ControlService
from platform_core.application.tool_catalog import build_tool_catalog
from platform_core.domain.control import RuleViolation
from platform_infrastructure.services import ExecutionClient, Remote

from scripts.maintenance.project_environment import docker_executable
from tests.knowledge.checkers.test_composed_runs import analysis_request
from tests.platform.database.processes import eventually

CASES = {
    "table-profile": {
        "implementation": "table-profile-native-v1",
        "role": "profile_report",
        "schema": "table-profile-report-v1",
        "parameters": {"fields": ["f0", "f1", "f2", "f3"]},
        "key": "profile",
    },
    "table-validate": {
        "implementation": "table-validate-native-v1",
        "role": "validation_report",
        "schema": "table-validation-report-v1",
        "parameters": {
            "rules": [
                {"rule_id": "rows", "kind": "ROW_COUNT", "minimum": "3", "maximum": "3"},
                {
                    "rule_id": "type",
                    "kind": "TYPE",
                    "field_id": "f3",
                    "expected_type": "int64",
                },
                {"rule_id": "nonnull", "kind": "NOT_NULL", "field_id": "f3"},
                {"rule_id": "unique", "kind": "UNIQUE", "field_ids": ["f3"]},
                {
                    "rule_id": "range",
                    "kind": "RANGE",
                    "field_id": "f1",
                    "minimum": "1",
                    "maximum": "3",
                },
                {
                    "rule_id": "allowed",
                    "kind": "ALLOWED_VALUES",
                    "field_id": "f2",
                    "allowed_values": ["true", "false"],
                },
            ]
        },
        "key": "validation",
    },
}


def _table_bytes():
    stream = io.BytesIO()
    pq.write_table(
        pa.table(
            {
                "station": pa.array(["A", "B", "B"], type=pa.string()),
                "level": pa.array([1.0, 2.0, 3.0], type=pa.float64()),
                "active": pa.array([True, True, False], type=pa.bool_()),
                "count": pa.array([1, 2, 3], type=pa.int64()),
            }
        ),
        stream,
    )
    return stream.getvalue()


def _upload(cluster, token):
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "P7.1b verified table"})
    cluster.project = project["project_id"]
    cluster.p = "/api/v1/projects/" + cluster.project
    content = _table_bytes()
    upload = cluster.request(
        "POST",
        cluster.p + "/uploads",
        {
            "filename": "table.parquet",
            "format": "parquet",
            "size": len(content),
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        },
    )
    path = cluster.p + "/uploads/" + upload["upload_id"]
    cluster.put_content(path + "/content", content)
    cluster.request("POST", path + "/finalize", {})
    ready = eventually(
        lambda: value if (value := cluster.request("GET", path))["status"] == "READY" else None,
        timeout=20,
    )
    asset = cluster.request(
        "GET", cluster.p + "/assets/" + ready["asset"]["asset_id"] + "/versions/1"
    )
    normalized = next(
        item
        for item in asset["manifest"]["representations"]
        if item["representation_id"] == "normalized"
    )
    return {
        "kind": "TABLE",
        "asset": ready["asset"],
        "representation_id": "normalized",
        "representation_digest": normalized["digest"],
    }


def _catalog(cluster):
    return cluster.operator_catalog


def _acceptance(operator, case):
    return {
        "profile": "open-science-v1",
        "delivery_class": "VERIFIED",
        "requirements": [
            {
                "requirement_id": "tabular-reproduction",
                "description": "Independently reproduce the exact tabular result",
                "necessary": True,
                "checks": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
            }
        ],
        "required_outputs": [
            {
                "role_id": case["role"],
                "necessary": True,
                "expected": {
                    "result_kind": "OPERATOR_RESULT",
                    "operator_id": operator,
                    "operator_version": "1.0.0",
                    "output_port": "report",
                    "schema_id": case["schema"],
                },
            }
        ],
        "allow_manual_review": False,
        "max_check_executions": 2,
    }


def _agent_task(cluster, actor, selection, operator, case):
    catalog = _catalog(cluster)
    selected = next(value for value in catalog["operators"] if value["operator_id"] == operator)
    frozen_analysis = analysis_request()
    frozen_analysis.pop("expected_spec_revision")
    control = ControlService(
        cluster.database,
        agent_runtime_profile=profile(),
        agent_tool_catalog=build_tool_catalog("p7-science"),
        operator_catalog=catalog,
    )
    task = control.create_task(
        actor,
        cluster.project,
        "agent-" + case["key"],
        {
            "execution_mode": "AGENT",
            "spec": {
                "goal": "Produce and independently verify the frozen tabular result",
                "requirements": [],
                "acceptance_spec": _acceptance(operator, case),
                "agent_analysis": frozen_analysis,
                "agent_operator_intents": [
                    {
                        "operator_id": operator,
                        "version_id": "1.0.0",
                        "input": selection,
                        "parameters": case["parameters"],
                    }
                ],
            },
            "budget": {"execution_count": 3, "max_in_flight": 2},
        },
    )
    control.grant(
        actor,
        cluster.project,
        "grant-" + case["key"],
        {
            "task_id": task["task_id"],
            "targets": [target(selected), OPEN_CHECK_TARGET],
            "resources": [selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
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
        "proposal_id": "proposal-" + case["key"],
        "frame_id": "frame-" + case["key"],
        "tool_version": "2.0.0",
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
        operator_catalog=catalog,
        checker_catalog=cluster.checker_catalog,
    )
    return task, context, backend


def _completed_execution(cluster, command_id, task_id, timeout=180):
    # Docker CLI calls are bounded at 30 seconds, but a rootless daemon may
    # commit an accepted create after the client deadline.  CREATE_INTENT then
    # reconciles that deterministic container on a later worker generation, so
    # this service-level wait must span several bounded reconciliation turns.
    def done():
        receipt = cluster.request("GET", cluster.p + "/commands/" + command_id)
        if not receipt.get("result"):
            return None
        execution = cluster.request(
            "GET", cluster.p + "/executions/" + receipt["result"]["execution_id"]
        )
        return execution if execution["state"] in {"SUCCEEDED", "FAILED"} else None

    return eventually(done, timeout=timeout)


@pytest.mark.parametrize("operator", ["table-profile", "table-validate"])
def test_agent_v2_completes_verified_tabular_result(cluster, owner, operator):
    actor, token = owner
    selection = _upload(cluster, token)
    case = CASES[operator]
    task, context, backend = _agent_task(cluster, actor, selection, operator, case)
    analysis = backend.execute("research.create_analysis", {}, context, "analysis-" + case["key"])
    registration = backend.execute(
        "research.register_analysis",
        {"revision_id": analysis.value["revision_id"]},
        context,
        "registration-" + case["key"],
    )
    submission = backend.execute(
        "execution.submit_operator",
        {"operator_id": operator, "version_id": "1.0.0"},
        context,
        "submit-" + case["key"],
    )
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    execution = _completed_execution(cluster, submission.command_id, task["task_id"])
    assert execution["state"] == "SUCCEEDED", execution
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
            if cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")["in_flight"]
            == 0
            else None
        ),
        timeout=30,
    )
    deliverable = backend.execute(
        "deliverable.register_exact",
        {
            "execution_id": execution["execution_id"],
            "role_id": case["role"],
            "output_port": "report",
        },
        context,
        "deliverable-" + case["key"],
    )
    if operator == "table-profile":
        cluster.kill(cluster.running["platform-worker-lanes"])
        cluster.kill(cluster.running["execution-worker"])
    acceptance = backend.execute(
        "acceptance.request",
        {
            "deliverable_id": deliverable.value["deliverable_id"],
            "analysis_registration_id": registration.value["registration_id"],
        },
        context,
        "acceptance-" + case["key"],
    )
    if operator == "table-profile":
        replay = backend.execute(
            "acceptance.request",
            {
                "deliverable_id": deliverable.value["deliverable_id"],
                "analysis_registration_id": registration.value["registration_id"],
            },
            context,
            "acceptance-" + case["key"],
        )
        assert replay.business_id == acceptance.business_id
        cluster.launch_platform_worker_lanes()
        cluster.launch("execution-worker")

    def accepted():
        value = cluster.request(
            "GET", cluster.p + f"/tasks/{task['task_id']}/acceptance/{acceptance.business_id}"
        )
        return value if value["status"] != "QUEUED" else None

    report = eventually(accepted, timeout=150)
    assert report["status"] == "PASS", report
    expected_capability = (
        "tabular-profile-reproduction-v1"
        if operator == "table-profile"
        else "tabular-validation-reproduction-v1"
    )
    assert [run["capability_id"] for run in report["check_runs"]] == [
        "platform-runtime-integrity-v1",
        expected_capability,
    ]
    assert {run["status"] for run in report["check_runs"]} == {"PASS"}
    with cluster.database.transaction() as uow:
        saved_runs = uow.science.check_runs(acceptance.business_id)
        assert len(saved_runs) == len({run["action_id"] for run in saved_runs}) == 2
    assert (
        cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}")["lifecycle"] == "COMPLETED"
    )
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)


def test_agent_cannot_deliver_failed_validation(cluster, owner):
    actor, token = owner
    selection = _upload(cluster, token)
    case = dict(CASES["table-validate"])
    case["parameters"] = {
        "rules": [{"rule_id": "unique-station", "kind": "UNIQUE", "field_ids": ["f0"]}]
    }
    task, context, backend = _agent_task(cluster, actor, selection, "table-validate", case)
    submission = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-validate", "version_id": "1.0.0"},
        context,
        "submit-failing-validation",
    )
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    execution = _completed_execution(cluster, submission.command_id, task["task_id"])
    assert execution["state"] == "FAILED"
    assert execution["runtime"]["error"] == "VALIDATION_FAILED"
    with pytest.raises(RuleViolation):
        backend.execute(
            "deliverable.register_exact",
            {
                "execution_id": execution["execution_id"],
                "role_id": case["role"],
                "output_port": "report",
            },
            context,
            "deliver-failing-validation",
        )
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1
    with cluster.database.transaction() as uow:
        assert uow.science.candidates(task["task_id"]) == []
        assert uow.science.checks(task["task_id"]) == []
        assert uow.tasks.get(cluster.project, task["task_id"]).lifecycle == "OPEN"


def test_workflow_outputs_use_three_checker_runs_and_five_budget_units(cluster, owner):
    actor, token = owner
    selection = _upload(cluster, token)
    catalog = _catalog(cluster)
    selected = {
        value["operator_id"]: value
        for value in catalog["operators"]
        if value["operator_id"] in CASES
    }
    requirements = [
        {
            "role_id": case["role"],
            "necessary": True,
            "expected": {
                "result_kind": "OPERATOR_RESULT",
                "operator_id": operator,
                "operator_version": "1.0.0",
                "output_port": "report",
                "schema_id": case["schema"],
            },
        }
        for operator, case in CASES.items()
    ]
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {
                "goal": "Verify both outputs of one tabular workflow",
                "requirements": [],
                "acceptance_spec": {
                    "profile": "open-science-v1",
                    "delivery_class": "VERIFIED",
                    "requirements": [
                        {
                            "requirement_id": "tabular-reproduction",
                            "description": "Independently reproduce both tabular results",
                            "necessary": True,
                            "checks": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
                        }
                    ],
                    "required_outputs": requirements,
                    "allow_manual_review": False,
                    "max_check_executions": 3,
                },
            },
            "budget": {"execution_count": 5, "max_in_flight": 2},
        },
        "workflow-task",
    )
    cluster.task = task["task_id"]
    task_path = cluster.p + "/tasks/" + cluster.task
    created = cluster.request(
        "POST", task_path + "/analysis-revisions", analysis_request(), "workflow-analysis"
    )
    registration = cluster.request(
        "POST",
        task_path + f"/analysis-revisions/{created['revision_id']}/register",
        {"expected_spec_revision": 1},
        "workflow-registration",
    )
    package = json.loads(
        Path("examples/workflows/table-profile-validate-v1.json").read_text(encoding="utf-8")
    )
    queued = cluster.request("POST", cluster.p + "/workflow-packages", package, "workflow-package")
    cluster.launch_platform_worker_lanes()
    frozen = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", queued["status_url"]))["status"] == "READY"
            else None
        ),
        timeout=40,
    )
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [
                workflow_target,
                target(selected["table-profile"]),
                target(selected["table-validate"]),
                OPEN_CHECK_TARGET,
            ],
            "resources": [selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        "workflow-grant",
    )
    command = cluster.request(
        "POST",
        cluster.p + "/executions",
        {
            "task_id": cluster.task,
            "expected_spec_revision": 1,
            "target": workflow_target,
            "inputs": {
                "table": selection,
                "profile_parameters": CASES["table-profile"]["parameters"],
                "validate_parameters": CASES["table-validate"]["parameters"],
            },
            "max_node_executions": 2,
        },
        "workflow-execution",
    )
    cluster.launch("execution-worker")
    workflow = cluster.completed_execution(command, timeout=180)
    assert workflow["state"] == "SUCCEEDED", workflow
    receipt = workflow["runtime"]["publication_receipt"]
    with cluster.database.transaction() as uow:
        parent = uow.actions.by_execution(cluster.project, workflow["execution_id"])
        nodes = uow.workflows.nodes(parent["action_id"])
    nodes_by_id = {node["node_id"]: node for node in nodes}
    bindings = []
    for _operator, case in CASES.items():
        published_port = case["key"] + "_report"
        published = receipt["outputs"][published_port]
        ref = published["asset"]
        asset = cluster.request(
            "GET", cluster.p + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
        )
        representation = next(
            value
            for value in asset["manifest"]["representations"]
            if value["representation_id"] == "report"
        )
        node = nodes_by_id[asset["manifest"]["workflow"]["member_id"]]
        bindings.append(
            {
                "role_id": case["role"],
                "output_ref": {
                    "kind": "WORKFLOW_OUTPUT",
                    "parent_action_id": parent["action_id"],
                    "parent_execution_id": workflow["execution_id"],
                    "node_action_id": node["node_id"],
                    "node_execution_id": node["observation"]["execution_id"],
                    "call_path": node["request"]["call_path"],
                    "scatter_path": node["request"]["scatter_indices"],
                    "published_output_port": published_port,
                    "published_path": [],
                    "output_port": "report",
                    "asset_ref": ref,
                    "representation_id": "report",
                    "representation_digest": representation["digest"],
                    "schema_id": case["schema"],
                    "schema_source": "MANIFEST",
                },
            }
        )
    deliverable = cluster.request(
        "POST",
        task_path + "/deliverables",
        {"expected_spec_revision": 1, "role_bindings": bindings},
        "workflow-deliverable",
    )
    acceptance = cluster.request(
        "POST",
        task_path + "/acceptance",
        {
            "expected_spec_revision": 1,
            "deliverable_id": deliverable["deliverable_id"],
            "analysis_registration_id": registration["registration_id"],
        },
        "workflow-acceptance",
    )
    report = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", acceptance["status_url"]))["status"] != "QUEUED"
            else None
        ),
        timeout=180,
    )
    assert report["status"] == "PASS", report
    assert [run["capability_id"] for run in report["check_runs"]] == [
        "platform-runtime-integrity-v1",
        "tabular-profile-reproduction-v1",
        "tabular-validation-reproduction-v1",
    ]
    budget = cluster.request("GET", task_path + "/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (5, 0, 0)


def test_checker_image_does_not_contain_the_production_tabular_package(cluster):
    inspected = subprocess.run(
        [
            docker_executable(),
            "run",
            "--rm",
            "--entrypoint",
            "python",
            cluster.env["V7_CHECKER_IMAGE"],
            "-c",
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('platform_tabular_operators') is None"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert inspected.returncode == 0, inspected.stderr
