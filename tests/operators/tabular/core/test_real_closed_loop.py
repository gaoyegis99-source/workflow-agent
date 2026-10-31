import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from platform_core.application.catalog import build_catalog, target

from tests.platform.database.processes import eventually


def parquet_bytes():
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


def setup_task(cluster, token):
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "P7.1a table pilot"})
    cluster.project = project["project_id"]
    cluster.p = "/api/v1/projects/" + cluster.project
    content = parquet_bytes()
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
    cluster.selection = {
        "kind": "TABLE",
        "asset": ready["asset"],
        "representation_id": "normalized",
        "representation_digest": normalized["digest"],
    }
    catalog = build_catalog(
        {
            "OPERATOR": cluster.env["V7_OPERATOR_IMAGE"],
            "QGIS": "sha256:" + "2" * 64,
            "SPATIAL_NATIVE": "sha256:" + "3" * 64,
            "TABULAR": cluster.env["V7_TABULAR_OPERATOR_IMAGE"],
        }
    )
    operators = {item["operator_id"]: target(item) for item in catalog["operators"]}
    cluster.targets = {name: operators[name] for name in ("table-profile", "table-validate")}
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "profile and validate a generic table"},
            "budget": {"execution_count": 2, "max_in_flight": 2},
        },
    )
    cluster.task = task["task_id"]
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": list(cluster.targets.values()),
            "resources": [ready["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )


def submit(cluster, operator, parameters, key):
    return cluster.request(
        "POST",
        cluster.p + "/executions",
        {
            "task_id": cluster.task,
            "expected_spec_revision": 1,
            "target": cluster.targets[operator],
            "inputs": {"table": cluster.selection},
            "parameters": parameters,
        },
        key=key,
    )


def test_real_profile_and_failed_validation_share_the_control_plane(cluster, owner):
    setup_task(cluster, owner[1])
    profile = submit(cluster, "table-profile", {"fields": ["f0", "f1", "f2", "f3"]}, "p")
    assert (
        submit(cluster, "table-profile", {"fields": ["f0", "f1", "f2", "f3"]}, "p")["command_id"]
        == profile["command_id"]
    )
    cluster.launch_platform_worker_lanes()

    def accepted():
        receipt = cluster.request("GET", profile["status_url"])
        return receipt if receipt.get("result") else None

    original = eventually(accepted, timeout=30)["result"]["execution_id"]
    cluster.restart("execution")
    assert eventually(accepted, timeout=10)["result"]["execution_id"] == original
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(profile)
    assert execution["state"] == "SUCCEEDED", execution
    assert execution["runtime"]["publication"] == "COMMITTED"
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    product = next(item for item in assets if item["ref"] != cluster.selection["asset"])
    assert product["manifest"]["result_kind"] == "OPERATOR_RESULT"
    assert product["manifest"]["source"]["provenance"]["ownership"] == "CURATED_PACKAGE"
    assert product["manifest"]["source"]["provenance"]["trust_level"] == "VERIFIED"

    failed = submit(
        cluster,
        "table-validate",
        {"rules": [{"rule_id": "no-duplicates", "kind": "UNIQUE", "field_ids": ["f0"]}]},
        "v",
    )
    rejected = cluster.completed_execution(failed)
    assert rejected["state"] == "FAILED", rejected
    assert rejected["runtime"]["error"] == "VALIDATION_FAILED"
    assert rejected["runtime"]["validation"]["failed_rule_count"] == 1
    assert rejected["runtime"]["publication"] == "ABORTED"
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == len(assets)
    budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget"))[
                "in_flight"
            ]
            == 0
            else None
        ),
        timeout=30,
    )
    assert budget["used"] == 2 and budget["reserved"] == 0


def test_real_workflow_mixes_two_curated_nodes_and_publishes_atomically(cluster, owner):
    setup_task(cluster, owner[1])
    package = json.loads(
        Path("examples/workflows/table-profile-validate-v1.json").read_text(encoding="utf-8")
    )
    queued = cluster.request(
        "POST", cluster.p + "/workflow-packages", package, key="tabular-package"
    )
    cluster.launch_platform_worker_lanes()

    def ready():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(ready, timeout=40)
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [workflow_target, *cluster.targets.values()],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    body = {
        "task_id": cluster.task,
        "expected_spec_revision": 1,
        "target": workflow_target,
        "inputs": {
            "table": cluster.selection,
            "profile_parameters": {"fields": ["f0", "f1"]},
            "validate_parameters": {
                "rules": [
                    {
                        "rule_id": "three-rows",
                        "kind": "ROW_COUNT",
                        "field_id": None,
                        "field_ids": None,
                        "expected_type": None,
                        "minimum": "3",
                        "maximum": "3",
                        "allowed_values": None,
                    }
                ]
            },
        },
        "max_node_executions": 2,
    }
    command = cluster.request("POST", cluster.p + "/executions", body, key="tabular-workflow")
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=180)
    assert execution["state"] == "SUCCEEDED", execution
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert len(steps) == 2 and all(item["state"] == "SUCCEEDED" for item in steps)
    receipt = execution["runtime"]["publication_receipt"]
    assert receipt["state"] == "COMMITTED"
    assert len(receipt["assets"]) == 2
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 3
    assert all(
        item["manifest"].get("result_kind") == "OPERATOR_RESULT"
        for item in assets
        if item["ref"] != cluster.selection["asset"]
    )
    budget = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)


def test_failed_validation_aborts_the_whole_workflow_result(cluster, owner):
    setup_task(cluster, owner[1])
    package = json.loads(
        Path("examples/workflows/table-profile-validate-v1.json").read_text(encoding="utf-8")
    )
    queued = cluster.request("POST", cluster.p + "/workflow-packages", package, key="fail-package")
    cluster.launch_platform_worker_lanes()

    def ready():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(ready, timeout=40)
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [workflow_target, *cluster.targets.values()],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    command = cluster.request(
        "POST",
        cluster.p + "/executions",
        {
            "task_id": cluster.task,
            "expected_spec_revision": 1,
            "target": workflow_target,
            "inputs": {
                "table": cluster.selection,
                "profile_parameters": {"fields": ["f0"]},
                "validate_parameters": {
                    "rules": [
                        {
                            "rule_id": "unique-station",
                            "kind": "UNIQUE",
                            "field_id": None,
                            "field_ids": ["f0"],
                            "expected_type": None,
                            "minimum": None,
                            "maximum": None,
                            "allowed_values": None,
                        }
                    ]
                },
            },
            "max_node_executions": 2,
        },
        key="fail-run",
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=180)
    assert execution["state"] == "FAILED", execution
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert {item["state"] for item in steps} == {"SUCCEEDED", "FAILED"}
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1
    assert execution["runtime"].get("publication") != "COMMITTED"


def test_real_workflow_chains_builtin_result_into_curated_operator(cluster, owner):
    cluster.bootstrap(owner[1], budget=2, max_in_flight=1, project_name="Mixed operators")
    package = json.loads(
        Path("examples/workflows/completeness-profile-v1.json").read_text(encoding="utf-8")
    )
    queued = cluster.request("POST", cluster.p + "/workflow-packages", package, key="mixed-package")
    cluster.launch_platform_worker_lanes()

    def ready():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(ready, timeout=40)
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    leaves = [
        item
        for item in cluster.targets
        if item["operator_id"] in {"timeseries-completeness", "table-profile"}
    ]
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [workflow_target, *leaves],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "acceptance_profiles": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    command = cluster.request(
        "POST",
        cluster.p + "/executions",
        {
            "task_id": cluster.task,
            "expected_spec_revision": 1,
            "target": workflow_target,
            "inputs": {
                "observations": cluster.selection,
                "analysis_parameters": cluster.parameters,
                "profile_parameters": {"fields": ["group", "expected_periods"]},
            },
            "max_node_executions": 2,
        },
        key="mixed-run",
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=180)
    assert execution["state"] == "SUCCEEDED", execution
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert {tuple(item["call_path"]) for item in steps} == {
        ("completeness",),
        ("profile",),
    }
    assert all(item["state"] == "SUCCEEDED" for item in steps)
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 3
    product = next(
        item
        for item in assets
        if item["manifest"].get("source", {}).get("target", {}).get("operator_id")
        == "table-profile"
    )
    assert product["manifest"]["source"]["provenance"]["ownership"] == "CURATED_PACKAGE"
