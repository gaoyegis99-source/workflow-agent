import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
from platform_core.application.catalog import build_catalog, target

from tests.operators.filters.test_table_filter import node
from tests.operators.tabular.acceptance.test_real_agent_acceptance import (
    _agent_task,
    _completed_execution,
    _upload,
)
from tests.platform.database.processes import eventually


def filter_parameters(field_id="f1"):
    return {
        "predicate": {
            "semantics": "kleene-three-valued-v1",
            "root_id": "positive_finite",
            "nodes": [
                node(
                    "positive",
                    "COMPARE",
                    field_id=field_id,
                    operator="GT",
                    value_type="double",
                    value="1.5",
                ),
                node("finite", "IS_FINITE", field_id=field_id),
                node("positive_finite", "AND", arguments=["positive", "finite"]),
            ],
        }
    }


CASE = {
    "implementation": "table-filter-native-v1",
    "role": "filter_report",
    "schema": "table-filter-report-v1",
    "parameters": filter_parameters(),
    "key": "filter",
}


def test_real_agent_filter_execution_publication_and_verified_acceptance(cluster, owner):
    actor, token = owner
    selection = _upload(cluster, token)
    task, context, backend = _agent_task(cluster, actor, selection, "table-filter", CASE)
    analysis = backend.execute("research.create_analysis", {}, context, "analysis-filter")
    registration = backend.execute(
        "research.register_analysis",
        {"revision_id": analysis.value["revision_id"]},
        context,
        "registration-filter",
    )
    submitted = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-filter", "version_id": "1.0.0"},
        context,
        "submit-filter",
    )
    replay = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-filter", "version_id": "1.0.0"},
        context,
        "submit-filter",
    )
    assert replay.command_id == submitted.command_id
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    execution = _completed_execution(cluster, submitted.command_id, task["task_id"])
    assert execution["state"] == "SUCCEEDED", json.dumps(execution, indent=2)
    assert execution["runtime"]["publication"] == "COMMITTED"

    products = [
        item
        for item in cluster.request("GET", cluster.p + "/assets")["items"]
        if item["manifest"].get("source", {}).get("target", {}).get("operator_id") == "table-filter"
    ]
    assert len(products) == 1
    product = products[0]
    assert product["manifest"]["tabular"]["row_count"] == 2
    response = cluster.client.get(
        cluster.p
        + "/assets/"
        + product["ref"]["asset_id"]
        + "/versions/1/representations/table/content"
    )
    assert response.is_success
    table = pq.read_table(io.BytesIO(response.content))
    assert table["f0"].to_pylist() == ["B", "B"]
    assert table["f1"].to_pylist() == [2.0, 3.0]

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
        "deliverable-filter",
    )
    acceptance = backend.execute(
        "acceptance.request",
        {
            "deliverable_id": deliverable.value["deliverable_id"],
            "analysis_registration_id": registration.value["registration_id"],
        },
        context,
        "acceptance-filter",
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
        "tabular-filter-reproduction-v1",
    ]
    assert cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}")["lifecycle"] == (
        "COMPLETED"
    )
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)


def test_real_workflow_projects_filters_then_profiles_private_tables(cluster, owner):
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
        if item["operator_id"] in {"table-project", "table-filter", "table-profile"}
    }
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Project, filter and profile a generic table"},
            "budget": {"execution_count": 3, "max_in_flight": 1},
        },
    )
    package = json.loads(
        Path("examples/workflows/table-project-filter-profile-v1.json").read_text()
    )
    queued = cluster.request(
        "POST", cluster.p + "/workflow-packages", package, key="project-filter-package"
    )
    cluster.launch_platform_worker_lanes()

    def package_ready():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(package_ready, timeout=40)
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
                "filter_parameters": filter_parameters("f0"),
                "profile_parameters": {"fields": ["f0", "f1"]},
            },
            "max_node_executions": 3,
        },
        key="project-filter-run",
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=210)
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert execution["state"] == "SUCCEEDED", json.dumps(
        {"execution": execution, "steps": steps}, indent=2
    )
    assert {tuple(item["call_path"]) for item in steps} == {
        ("project",),
        ("filter",),
        ("profile",),
    }
    assert all(item["state"] == "SUCCEEDED" for item in steps)
    receipt = execution["runtime"]["publication_receipt"]
    assert receipt["state"] == "COMMITTED" and len(receipt["assets"]) == 3
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 4
    products = {
        item["manifest"]["source"]["target"]["operator_id"]: item
        for item in assets
        if item["manifest"].get("result_kind") == "OPERATOR_RESULT"
    }
    assert set(products) == {"table-project", "table-filter", "table-profile"}
    assert products["table-filter"]["manifest"]["tabular"]["row_count"] == 2
    profile_source = products["table-profile"]["manifest"]["source"]["inputs"]["table"]
    assert profile_source["derived_from"]["node_id"]
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
