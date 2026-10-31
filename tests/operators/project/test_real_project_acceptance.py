import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
from platform_core.application.catalog import build_catalog, target

from tests.operators.tabular.acceptance.test_real_agent_acceptance import (
    _agent_task,
    _completed_execution,
    _upload,
)
from tests.platform.database.processes import eventually

CASE = {
    "implementation": "table-project-native-v1",
    "role": "project_report",
    "schema": "table-project-report-v1",
    "parameters": {
        "fields": [
            {"source_field_id": "f1", "display_name": "水位"},
            {"source_field_id": "f0", "display_name": "监测点"},
        ]
    },
    "key": "project",
}


def test_real_agent_project_execution_publication_and_verified_acceptance(cluster, owner):
    actor, token = owner
    selection = _upload(cluster, token)
    task, context, backend = _agent_task(cluster, actor, selection, "table-project", CASE)
    analysis = backend.execute("research.create_analysis", {}, context, "analysis-project")
    registration = backend.execute(
        "research.register_analysis",
        {"revision_id": analysis.value["revision_id"]},
        context,
        "registration-project",
    )
    submitted = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-project", "version_id": "1.0.0"},
        context,
        "submit-project",
    )
    replay = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-project", "version_id": "1.0.0"},
        context,
        "submit-project",
    )
    assert replay.command_id == submitted.command_id
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    execution = _completed_execution(cluster, submitted.command_id, task["task_id"])
    assert execution["state"] == "SUCCEEDED", json.dumps(execution, indent=2)
    assert execution["runtime"]["publication"] == "COMMITTED"

    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    product = next(
        item
        for item in assets
        if item["manifest"].get("source", {}).get("target", {}).get("operator_id")
        == "table-project"
    )
    descriptor = product["manifest"]["tabular"]
    assert descriptor == {
        "row_count": 3,
        "fields": [
            {
                "field_id": "f0",
                "name": "水位",
                "type": "double",
                "source_field_id": "f1",
            },
            {
                "field_id": "f1",
                "name": "监测点",
                "type": "string",
                "source_field_id": "f0",
            },
        ],
    }
    table_response = cluster.client.get(
        cluster.p
        + "/assets/"
        + product["ref"]["asset_id"]
        + "/versions/1/representations/table/content"
    )
    assert table_response.is_success
    projected = pq.read_table(io.BytesIO(table_response.content))
    assert projected.column_names == ["f0", "f1"]
    assert projected["f0"].to_pylist() == [1.0, 2.0, 3.0]
    assert projected["f1"].to_pylist() == ["A", "B", "B"]

    eventually(
        lambda: (
            value
            if (value := cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget"))[
                "in_flight"
            ]
            == 0
            else None
        ),
        timeout=30,
    )
    deliverable = backend.execute(
        "deliverable.register_exact",
        {
            "execution_id": execution["execution_id"],
            "role_id": CASE["role"],
            "output_port": "report",
        },
        context,
        "deliverable-project",
    )
    acceptance = backend.execute(
        "acceptance.request",
        {
            "deliverable_id": deliverable.value["deliverable_id"],
            "analysis_registration_id": registration.value["registration_id"],
        },
        context,
        "acceptance-project",
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
        "tabular-project-reproduction-v1",
    ]
    assert cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}")["lifecycle"] == (
        "COMPLETED"
    )
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)


def test_real_workflow_projects_then_profiles_private_derived_table(cluster, owner):
    _, token = owner
    selection = _upload(cluster, token)
    catalog = build_catalog(
        {
            "OPERATOR": cluster.env["V7_OPERATOR_IMAGE"],
            "QGIS": "sha256:" + "2" * 64,
            "SPATIAL_NATIVE": "sha256:" + "3" * 64,
            "TABULAR": cluster.env["V7_TABULAR_OPERATOR_IMAGE"],
        }
    )
    selected = {
        item["operator_id"]: target(item)
        for item in catalog["operators"]
        if item["operator_id"] in {"table-project", "table-profile"}
    }
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Project and profile a generic table"},
            "budget": {"execution_count": 2, "max_in_flight": 1},
        },
    )
    package = json.loads(
        Path("examples/workflows/table-project-profile-v1.json").read_text(encoding="utf-8")
    )
    queued = cluster.request(
        "POST", cluster.p + "/workflow-packages", package, key="project-profile-package"
    )
    workers = cluster.launch_platform_worker_lanes()

    def package_ready():
        assert workers.poll() is None, "A Platform worker lane exited before package readiness"
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(package_ready, timeout=90)
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": task["task_id"],
            "targets": [workflow_target, *selected.values()],
            "resources": [selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    command = cluster.request(
        "POST",
        cluster.p + "/executions",
        {
            "task_id": task["task_id"],
            "expected_spec_revision": 1,
            "target": workflow_target,
            "inputs": {
                "table": selection,
                "project_parameters": {
                    "fields": [
                        {"source_field_id": "f1", "display_name": "水位"},
                        {"source_field_id": "f0", "display_name": "站点"},
                    ]
                },
                "profile_parameters": {"fields": ["f0", "f1"]},
            },
            "max_node_executions": 2,
        },
        key="project-profile-run",
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=180)
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert execution["state"] == "SUCCEEDED", json.dumps(
        {"execution": execution, "steps": steps}, indent=2
    )
    assert {tuple(item["call_path"]) for item in steps} == {("project",), ("profile",)}
    assert all(item["state"] == "SUCCEEDED" for item in steps)
    receipt = execution["runtime"]["publication_receipt"]
    assert receipt["state"] == "COMMITTED" and len(receipt["assets"]) == 2
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 3
    products = {
        item["manifest"]["source"]["target"]["operator_id"]: item
        for item in assets
        if item["manifest"].get("result_kind") == "OPERATOR_RESULT"
    }
    assert set(products) == {"table-project", "table-profile"}
    assert products["table-project"]["manifest"]["tabular"]["fields"][0] == {
        "field_id": "f0",
        "name": "水位",
        "type": "double",
        "source_field_id": "f1",
    }
    profile_source = products["table-profile"]["manifest"]["source"]["inputs"]["table"]
    assert profile_source["derived_from"]["representation_id"] == "table"
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)
