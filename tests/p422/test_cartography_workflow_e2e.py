import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platform_core.application.catalog import OPERATORS, target
from platform_execution.cartography import verify_png

from tests.p2.processes import eventually
from tests.p42.test_spatial_workflow_e2e import FIXTURE, p42_cluster  # noqa: F401


def bootstrap(cluster, token):
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "Cartographic workflow"})
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
    path = cluster.p + "/uploads/" + upload["upload_id"]
    cluster.put_content(path + "/content", raw)
    cluster.request("POST", path + "/finalize", {"kind": "SPATIAL", "profile": "strict-spatial-v1"})

    def ready():
        value = cluster.request("GET", path)
        assert value["status"] != "FAILED", value
        return value if value["status"] == "READY" else None

    uploaded = eventually(ready, timeout=30)
    asset = cluster.request(
        "GET", cluster.p + f"/assets/{uploaded['asset']['asset_id']}/versions/1"
    )
    selection = {
        "asset": uploaded["asset"],
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
            "spec": {"goal": "Buffer, summarize and render a trusted map", "requirements": []},
            "budget": {"execution_count": 3, "max_in_flight": 2},
        },
    )
    cluster.task = task["task_id"]
    source = json.loads(Path("examples/workflows/qgis-buffer-summary-map-v1.json").read_text())
    queued = cluster.request(
        "POST", cluster.p + "/workflow-packages", source, key="cartography-package"
    )
    cluster.launch("platform-worker")

    def frozen():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    package = eventually(frozen, timeout=40)
    workflow_target = {"kind": "WORKFLOW", "package_ref": package["package_ref"]}
    items = {item["operator_id"]: item for item in OPERATORS}
    leaves = [
        target(items["qgis-buffer"]),
        target(items["spatial-vector-summary"]),
        target(items["qgis-static-map"]),
    ]
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [workflow_target, *leaves],
            "resources": [uploaded["asset"]],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    return selection, workflow_target


def test_buffer_summary_map_workflow_is_atomic_and_unique(
    p42_cluster,  # noqa: F811
    owner,
):
    cluster = p42_cluster
    selection, workflow_target = bootstrap(cluster, owner[1])
    body = {
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
            "map_parameters": {
                "layer_id": "l0",
                "template_id": "v7-spatial-basic-v1",
                "output_crs": "EPSG:32650",
                "title": "缓冲区成果",
                "legend": True,
                "palette": "blue",
            },
        },
        "max_node_executions": 3,
    }
    command = cluster.request("POST", cluster.p + "/executions", body, key="map-run")
    replay = cluster.request("POST", cluster.p + "/executions", body, key="map-run")
    assert (command["action_id"], command["command_id"]) == (
        replay["action_id"],
        replay["command_id"],
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=200)
    assert execution["state"] == "SUCCEEDED", execution
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert len(steps) == 3 and all(item["state"] == "SUCCEEDED" for item in steps)
    assert {tuple(item["call_path"]) for item in steps} == {
        ("buffer",),
        ("summary",),
        ("map",),
    }
    receipt = execution["runtime"]["publication_receipt"]
    assert receipt["state"] == "COMMITTED"
    assert set(receipt["outputs"]) == {
        "buffer",
        "buffer_manifest",
        "buffer_method",
        "summary",
        "summary_method",
        "map_image",
        "map_manifest",
        "map_method",
    }
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 4
    map_asset = next(
        item for item in assets if item["manifest"].get("result_kind") == "CARTOGRAPHIC_PRODUCT"
    )
    assert map_asset["manifest"]["cartography"]["title"] == "缓冲区成果"
    response = cluster.client.get(
        cluster.p
        + f"/assets/{map_asset['ref']['asset_id']}/versions/1/representations/image/content"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    verify_png(response.content, "blue", True)
    budget = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (3, 0, 0)
    task = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}")
    assert task["lifecycle"] == "OPEN"


def test_map_failure_does_not_publish_partial_workflow_outputs(
    p42_cluster,  # noqa: F811
    owner,
):
    cluster = p42_cluster
    selection, workflow_target = bootstrap(cluster, owner[1])
    command = cluster.request(
        "POST",
        cluster.p + "/executions",
        {
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
                "map_parameters": {
                    "layer_id": "l0",
                    "template_id": "v7-spatial-basic-v1",
                    "output_crs": "EPSG:4326",
                    "title": "Must fail",
                    "legend": True,
                    "palette": "blue",
                },
            },
            "max_node_executions": 3,
        },
        key="map-failure",
    )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=200)
    assert execution["state"] == "FAILED", execution
    assert execution["runtime"]["publication"] == "ABORTED"
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 1
    assert all(item["manifest"].get("result_kind") is None for item in assets)
