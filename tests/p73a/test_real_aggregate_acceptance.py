import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from platform_core.application.catalog import build_catalog, target

from tests.p2.processes import eventually
from tests.p71b.test_real_agent_acceptance import (
    _agent_task,
    _completed_execution,
    _upload,
)
from tests.p72b.test_real_filter_acceptance import filter_parameters

AGGREGATE_PARAMETERS = {
    "semantics": "strict-table-aggregate-v1",
    "group_by": ["f0"],
    "null_key_policy": "GROUP",
    "measures": [
        {"measure_id": "rows", "operation": "COUNT_ROWS"},
        {"measure_id": "mean_level", "operation": "MEAN", "field_id": "f1"},
        {"measure_id": "total_count", "operation": "SUM", "field_id": "f3"},
    ],
}
CASE = {
    "implementation": "table-aggregate-native-v1",
    "role": "aggregate_report",
    "schema": "table-aggregate-report-v1",
    "parameters": AGGREGATE_PARAMETERS,
    "key": "aggregate",
}


def _catalog(cluster):
    return build_catalog(
        {
            "OPERATOR": cluster.env["V7_OPERATOR_IMAGE"],
            "QGIS": "sha256:" + "2" * 64,
            "SPATIAL_NATIVE": "sha256:" + "3" * 64,
            "TABULAR": cluster.env["V7_TABULAR_OPERATOR_IMAGE"],
        }
    )


def _upload_table(cluster, content):
    upload = cluster.request(
        "POST",
        cluster.p + "/uploads",
        {
            "filename": "aggregate-source.parquet",
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


def test_real_agent_aggregate_execution_publication_and_verified_acceptance(cluster, owner):
    actor, token = owner
    selection = _upload(cluster, token)
    task, context, backend = _agent_task(cluster, actor, selection, "table-aggregate", CASE)
    analysis = backend.execute("research.create_analysis", {}, context, "analysis-aggregate")
    registration = backend.execute(
        "research.register_analysis",
        {"revision_id": analysis.value["revision_id"]},
        context,
        "registration-aggregate",
    )
    submitted = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-aggregate", "version_id": "1.0.0"},
        context,
        "submit-aggregate",
    )
    replay = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-aggregate", "version_id": "1.0.0"},
        context,
        "submit-aggregate",
    )
    assert replay.command_id == submitted.command_id
    cluster.launch("platform-worker")
    cluster.launch("execution-worker")
    execution = _completed_execution(cluster, submitted.command_id, task["task_id"])
    assert execution["state"] == "SUCCEEDED", json.dumps(execution, indent=2)
    assert execution["runtime"]["publication"] == "COMMITTED"

    products = [
        item
        for item in cluster.request("GET", cluster.p + "/assets")["items"]
        if item["manifest"].get("source", {}).get("target", {}).get("operator_id")
        == "table-aggregate"
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
    assert table.to_pylist() == [
        {"f0": "A", "f1": 1, "f2": 1.0, "f3": 1},
        {"f0": "B", "f1": 2, "f2": 2.5, "f3": 5},
    ]

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
        "deliverable-aggregate",
    )
    acceptance = backend.execute(
        "acceptance.request",
        {
            "deliverable_id": deliverable.value["deliverable_id"],
            "analysis_registration_id": registration.value["registration_id"],
        },
        context,
        "acceptance-aggregate",
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
        "tabular-aggregate-reproduction-v1",
    ]
    assert cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}")["lifecycle"] == (
        "COMPLETED"
    )
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)


def test_real_null_group_error_fails_once_without_publication(cluster, owner):
    _, token = owner
    _upload(cluster, token)
    stream = io.BytesIO()
    pq.write_table(
        pa.table(
            {
                "station": pa.array(["A", None], type=pa.string()),
                "level": pa.array([1.0, 2.0], type=pa.float64()),
                "active": pa.array([True, False], type=pa.bool_()),
                "count": pa.array([1, 2], type=pa.int64()),
            }
        ),
        stream,
    )
    selection = _upload_table(cluster, stream.getvalue())
    operator = next(
        item for item in _catalog(cluster)["operators"] if item["operator_id"] == "table-aggregate"
    )
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Reject null group keys"},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": task["task_id"],
            "targets": [target(operator)],
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
            "target": target(operator),
            "inputs": {"table": selection},
            "parameters": {
                "semantics": "strict-table-aggregate-v1",
                "group_by": ["f0"],
                "null_key_policy": "ERROR",
                "measures": [{"measure_id": "rows", "operation": "COUNT_ROWS"}],
            },
        },
        key="aggregate-error",
    )
    cluster.launch("platform-worker")
    cluster.launch("execution-worker")
    execution = _completed_execution(cluster, command["command_id"], task["task_id"])
    assert execution["state"] == "FAILED"
    assert execution["runtime"]["error"] == "AGGREGATION_FAILED"
    assert execution["runtime"]["aggregation"]["violation_count"] == 1
    products = [
        item
        for item in cluster.request("GET", cluster.p + "/assets")["items"]
        if item["manifest"].get("source", {}).get("target", {}).get("operator_id")
        == "table-aggregate"
    ]
    assert products == []
    budget = eventually(
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
    assert (budget["used"], budget["reserved"]) == (1, 0)


def test_real_workflow_filters_aggregates_then_profiles_atomically(cluster, owner):
    _, token = owner
    selection = _upload(cluster, token)
    catalog = _catalog(cluster)
    selected = {
        item["operator_id"]: target(item)
        for item in catalog["operators"]
        if item["operator_id"] in {"table-filter", "table-aggregate", "table-profile"}
    }
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Filter, aggregate and profile a generic table"},
            "budget": {"execution_count": 3, "max_in_flight": 1},
        },
    )
    package = json.loads(
        Path("examples/workflows/table-filter-aggregate-profile-v1.json").read_text()
    )
    queued = cluster.request(
        "POST", cluster.p + "/workflow-packages", package, key="aggregate-package"
    )
    cluster.launch("platform-worker")

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
                "filter_parameters": filter_parameters("f1"),
                "aggregate_parameters": AGGREGATE_PARAMETERS,
                "profile_parameters": {"fields": ["f0", "f1", "f2", "f3"]},
            },
            "max_node_executions": 3,
        },
        key="filter-aggregate-profile-run",
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=240)
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert execution["state"] == "SUCCEEDED", json.dumps(
        {"execution": execution, "steps": steps}, indent=2
    )
    assert {tuple(item["call_path"]) for item in steps} == {
        ("filter",),
        ("aggregate",),
        ("profile",),
    }
    assert all(item["state"] == "SUCCEEDED" for item in steps)
    receipt = execution["runtime"]["publication_receipt"]
    assert receipt["state"] == "COMMITTED" and len(receipt["assets"]) == 3
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    products = {
        item["manifest"]["source"]["target"]["operator_id"]: item
        for item in assets
        if item["manifest"].get("result_kind") == "OPERATOR_RESULT"
    }
    assert set(products) == {"table-filter", "table-aggregate", "table-profile"}
    assert products["table-aggregate"]["manifest"]["tabular"]["row_count"] == 1
    assert products["table-profile"]["manifest"]["tabular"]["row_count"] == 4
    aggregate_source = products["table-aggregate"]["manifest"]["source"]["inputs"]["table"]
    profile_source = products["table-profile"]["manifest"]["source"]["inputs"]["table"]
    assert aggregate_source["derived_from"]["node_id"]
    assert profile_source["derived_from"]["node_id"]
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
