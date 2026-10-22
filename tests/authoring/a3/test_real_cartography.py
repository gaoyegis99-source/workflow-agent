import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platform_core.application.catalog import build_catalog, target

from tests.authoring.a3.test_real_workflow import (
    authoring_once,
    publish_release_in_studio,
    submit_trial_in_studio,
)
from tests.p2.processes import eventually
from tests.p42.conftest import spatial_images  # noqa: F401
from tests.p42.test_spatial_workflow_e2e import FIXTURE, p42_cluster  # noqa: F401


def test_real_buffer_summary_map_trial_releases_atomically(p42_cluster, owner):  # noqa: F811
    cluster = p42_cluster
    _, token = owner
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "A3 map trial"})
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
        "POST",
        upload_path + "/finalize",
        {"kind": "SPATIAL", "profile": "strict-spatial-v1"},
    )
    uploaded = eventually(
        lambda: (
            value if (value := cluster.request("GET", upload_path))["status"] == "READY" else None
        ),
        timeout=40,
    )
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
    source = json.loads(Path("examples/workflows/qgis-buffer-summary-map-v1.json").read_text())
    root = source["documents"]["main"]
    root["inputs"] = {"vector": "File"}
    root["steps"]["buffer"]["in"]["parameters"] = {
        "default": {
            "distance": 100,
            "unit": "metre",
            "output_crs": "EPSG:32650",
            "segments": 8,
        }
    }
    root["steps"]["summary"]["in"]["parameters"] = {"default": {"layer_id": "l0"}}
    root["steps"]["map"]["in"]["parameters"] = {
        "default": {
            "layer_id": "l0",
            "template_id": "v7-spatial-basic-v1",
            "output_crs": "EPSG:32650",
            "title": "A3 缓冲区成果",
            "legend": True,
            "palette": "blue",
        }
    }
    definition = cluster.request(
        "POST",
        cluster.p + "/workflow-definitions",
        {
            "expected_revision": 0,
            "stable_key": "a3-map",
            "metadata": {"title": "A3 map"},
            "layout": {},
        },
        key="a3-map-definition",
    )
    definition_path = cluster.p + f"/workflow-definitions/{definition['definition_id']}"
    revision = cluster.request(
        "POST",
        definition_path + "/revisions",
        {
            "expected_revision": 1,
            "source_digest": definition["head"]["source_digest"],
            "source": source,
            "metadata": {"title": "A3 map"},
            "layout": {},
        },
        key="a3-map-revision",
    )
    descriptor = cluster.request("GET", cluster.p + "/authoring/operator-descriptors")
    validation = cluster.request(
        "POST",
        definition_path + "/validations",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "semantic_contract_digest": descriptor["semantic_contract_digest"],
        },
        key="a3-map-validation",
    )
    authoring_once(cluster)
    validation_view = cluster.request("GET", validation["status_url"])
    assert validation_view["status"] == "PASSED", validation_view
    candidate = cluster.request(
        "POST",
        definition_path + "/candidates",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "validation_id": validation["validation_id"],
        },
        key="a3-map-candidate",
    )
    cluster.launch("platform-worker")
    candidate_view = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", candidate["status_url"]))["status"] == "READY"
            else None
        ),
        timeout=40,
    )
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Qualify the cartography workflow", "requirements": []},
            "budget": {"execution_count": 3, "max_in_flight": 2},
        },
        key="a3-map-task",
    )
    catalog = build_catalog(
        {
            "OPERATOR": cluster.env["V7_OPERATOR_IMAGE"],
            "QGIS": cluster.env["V7_SPATIAL_QGIS_IMAGE_ID"],
            "SPATIAL_NATIVE": cluster.env["V7_SPATIAL_NATIVE_IMAGE_ID"],
            "TABULAR": "sha256:" + "4" * 64,
        }
    )
    operators = {item["operator_id"]: item for item in catalog["operators"]}
    workflow_target = {"kind": "WORKFLOW", "package_ref": candidate_view["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": task["task_id"],
            "targets": [
                workflow_target,
                target(operators["qgis-buffer"]),
                target(operators["spatial-vector-summary"]),
                target(operators["qgis-static-map"]),
            ],
            "resources": [uploaded["asset"]],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        key="a3-map-grant",
    )
    trial = submit_trial_in_studio(
        cluster,
        token,
        definition,
        "a3-map",
        candidate["candidate_id"],
        task["task_id"],
        {"vector": selection},
        [],
        3,
    )
    cluster.launch("execution-worker")

    def completed():
        current = cluster.request("GET", definition_path + "/trial-runs/" + trial["trial_id"])
        if not current.get("execution_id"):
            return None
        execution = cluster.request("GET", cluster.p + "/executions/" + current["execution_id"])
        return execution if execution["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"} else None

    execution = eventually(completed, timeout=240)
    assert execution["state"] == "SUCCEEDED", execution
    authoring_once(cluster)
    trial_view = cluster.request("GET", definition_path + "/trial-runs/" + trial["trial_id"])
    assert trial_view["case_verdict"] == "PASS", trial_view
    release = publish_release_in_studio(
        cluster,
        token,
        definition,
        "a3-map",
        candidate["candidate_id"],
    )
    assert release["qualification"] == "SAMPLE_VERIFIED"
    assert len(execution["runtime"]["publication_receipt"]["assets"]) == 3
