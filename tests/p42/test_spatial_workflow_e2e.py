import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from platform_core.application.catalog import OPERATORS, target

from tests.p2.processes import eventually
from tests.p3.harness import Cluster, cleanup_test_containers

FIXTURE = Path(__file__).parent / "fixtures" / "points-utm.gpkg"


@pytest.fixture
def p42_cluster(tmp_path, database, service_databases, spatial_images):
    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env["V7_CWL_IMAGE"] = Path(".p4-runtime/cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
    cluster.env["V7_SPATIAL_QGIS_IMAGE_ID"] = spatial_images["qgis"]
    cluster.env["V7_SPATIAL_NATIVE_IMAGE_ID"] = spatial_images["native"]
    cluster.env["V7_DATA_SPATIAL_WORK_ROOT"] = str(tmp_path / "spatial-import")
    try:
        cluster.start_services()
        cluster.launch("data-spatial-worker")
        yield cluster
    finally:
        cluster.close()
        cleanup_test_containers(cluster.test_run_id)


def package(buffer_target, summary_target):
    buffer_parameters = {
        "type": "record",
        "name": "BufferParameters",
        "fields": [
            {"name": "distance", "type": "double"},
            {"name": "unit", "type": "string"},
            {"name": "output_crs", "type": "string"},
            {"name": "segments", "type": "int"},
        ],
    }
    summary_parameters = {
        "type": "record",
        "name": "SpatialSummaryParameters",
        "fields": [{"name": "layer_id", "type": "string"}],
    }
    return {
        "name": "qgis-buffer-summary-v1",
        "entrypoint": "main",
        "profile": "v7-cwl-p4-v1",
        "documents": {
            "main": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "inputs": {
                    "vector": "File",
                    "buffer_parameters": buffer_parameters,
                    "summary_parameters": summary_parameters,
                },
                "outputs": {
                    "buffer": {"type": "File", "outputSource": "buffer/vector"},
                    "buffer_manifest": {"type": "File", "outputSource": "buffer/manifest"},
                    "buffer_method": {"type": "File", "outputSource": "buffer/method"},
                    "summary": {"type": "File", "outputSource": "summary/summary"},
                    "summary_method": {"type": "File", "outputSource": "summary/method"},
                },
                "steps": {
                    "buffer": {
                        "run": "buffer_operator",
                        "in": {"vector": "vector", "parameters": "buffer_parameters"},
                        "out": ["vector", "manifest", "method"],
                    },
                    "summary": {
                        "run": "summary_operator",
                        "in": {
                            "vector": "buffer/vector",
                            "parameters": "summary_parameters",
                        },
                        "out": ["summary", "method"],
                    },
                },
            }
        },
        "implementations": {
            "buffer_operator": buffer_target,
            "summary_operator": summary_target,
        },
    }


def bootstrap(cluster, token):
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "Synthetic spatial"})
    cluster.project = project["project_id"]
    cluster.p = "/api/v1/projects/" + cluster.project
    raw = FIXTURE.read_bytes()
    upload = cluster.request(
        "POST",
        cluster.p + "/uploads",
        {
            "filename": "points-utm.gpkg",
            "format": "geopackage",
            "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        },
    )
    upload_path = cluster.p + "/uploads/" + upload["upload_id"]
    cluster.put_content(upload_path + "/content", raw)
    cluster.request(
        "POST", upload_path + "/finalize", {"kind": "SPATIAL", "profile": "strict-spatial-v1"}
    )

    def ready_asset():
        value = cluster.request("GET", upload_path)
        assert value["status"] != "FAILED", value
        return value if value["status"] == "READY" else None

    uploaded = eventually(ready_asset, timeout=30)
    ref = uploaded["asset"]
    asset = cluster.request("GET", cluster.p + f"/assets/{ref['asset_id']}/versions/1")
    selection = {
        "asset": ref,
        "representation_id": "original",
        "representation_digest": asset["manifest"]["representations"][0]["digest"],
        "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
        "semantic_revision": None,
    }
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Buffer and summarize checked spatial input", "requirements": []},
            "budget": {"execution_count": 2, "max_in_flight": 2},
        },
    )
    cluster.task = task["task_id"]
    items = {item["operator_id"]: item for item in OPERATORS}
    leaves = [target(items["qgis-buffer"]), target(items["spatial-vector-summary"])]
    queued = cluster.request(
        "POST", cluster.p + "/workflow-packages", package(*leaves), key="spatial-package"
    )
    cluster.launch("platform-worker")

    def validated():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(validated, timeout=40)
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [workflow_target, *leaves],
            "resources": [ref],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    return selection, workflow_target


def test_real_qgis_to_native_workflow_is_atomic_and_unique(p42_cluster, owner):
    cluster = p42_cluster
    selection, workflow_target = bootstrap(cluster, owner[1])
    request = {
        "task_id": cluster.task,
        "expected_spec_revision": 1,
        "target": workflow_target,
        "inputs": {
            "vector": selection,
            "buffer_parameters": {
                "distance": 100,
                "unit": "metre",
                "output_crs": "EPSG:32650",
                "segments": 8,
            },
            "summary_parameters": {"layer_id": "l0"},
        },
        "max_node_executions": 2,
    }
    command = cluster.request("POST", cluster.p + "/executions", request, key="spatial-run")
    replay = cluster.request("POST", cluster.p + "/executions", request, key="spatial-run")
    assert (replay["action_id"], replay["command_id"]) == (
        command["action_id"],
        command["command_id"],
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=180)
    assert execution["state"] == "SUCCEEDED", execution
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert len(steps) == 2 and all(item["state"] == "SUCCEEDED" for item in steps)
    assert {tuple(item["call_path"]) for item in steps} == {("buffer",), ("summary",)}
    receipt = execution["runtime"]["publication_receipt"]
    assert receipt["state"] == "COMMITTED"
    assert set(receipt["outputs"]) == {
        "buffer",
        "buffer_manifest",
        "buffer_method",
        "summary",
        "summary_method",
    }
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 3  # one input plus one asset per executed node
    spatial = next(
        item for item in assets if item["manifest"].get("result_kind") == "SPATIAL_VECTOR"
    )
    assert spatial["manifest"]["driver"] == "GPKG"
    assert spatial["manifest"]["spatial"]["layers"][0]["feature_count"] == 2
    budget = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)
    task = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}")
    assert task["lifecycle"] == "OPEN"
