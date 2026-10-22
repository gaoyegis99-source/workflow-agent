import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from platform_core.application.catalog import OPERATORS, target
from platform_core.application.workflow_compiler import compile_package
from platform_execution.implementations import REMOTE_SPATIAL_SUMMARY_CONTRACT

from tests.p2.processes import eventually
from tests.p42.test_spatial_workflow_e2e import package


def remote_target(binding_id="p432-peer", revision=1):
    return {
        "kind": "OPERATOR",
        "operator_id": "spatial-vector-summary",
        "version_id": "1.0.0",
        "execution_binding": {
            "kind": "REMOTE",
            "service_binding_id": binding_id,
            "binding_revision": revision,
        },
    }


def binding(peer):
    return {
        "service_binding_id": "p432-peer",
        "protocol": "v7-remote-execution-v2",
        "endpoint": peer[0],
        "credential_ref": "p43-peer-token",
        "tls_ca_ref": "p43-test-ca",
        "targets": [
            {
                "operator_id": "spatial-vector-summary",
                "version_id": "1.0.0",
                "remote_implementation_id": "spatial-summary-remote-v1",
                "contract_digest": REMOTE_SPATIAL_SUMMARY_CONTRACT,
                "implementation_digest": peer[4],
            }
        ],
        "limits": {
            "input_bytes": 20 * 1024 * 1024,
            "output_bytes": 100 * 1024 * 1024,
            "timeout_seconds": 180,
        },
    }


def test_remote_leaf_compiles_without_exposing_an_endpoint():
    local = target(next(item for item in OPERATORS if item["operator_id"] == "qgis-buffer"))
    compiled = compile_package(package(local, remote_target()))
    frozen = compiled["invocations"]["summary"]["target"]
    assert frozen == remote_target()
    assert "endpoint" not in str(compiled) and "credential" not in str(compiled)


def test_local_buffer_to_remote_summary_is_atomic(remote_cluster, owner, database):
    (cluster, peer), (_, token) = remote_cluster, owner
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "Mixed workflow"})
    cluster.project = project["project_id"]
    cluster.p = "/api/v1/projects/" + cluster.project
    database.service_binding_create(cluster.project, binding(peer))

    raw = Path("tests/p42/fixtures/points-utm.gpkg").read_bytes()
    upload = cluster.request(
        "POST",
        cluster.p + "/uploads",
        {
            "filename": "points.gpkg",
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

    def imported():
        value = cluster.request("GET", upload_path)
        assert value["status"] != "FAILED", value
        return value if value["status"] == "READY" else None

    ready = eventually(imported, timeout=30)
    ref = ready["asset"]
    asset = cluster.request("GET", cluster.p + f"/assets/{ref['asset_id']}/versions/1")
    original = asset["manifest"]["representations"][0]
    selection = {
        "asset": ref,
        "representation_id": "original",
        "representation_digest": original["digest"],
        "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
        "semantic_revision": None,
    }
    task = cluster.request(
        "POST",
        cluster.p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Local buffer and remote summary", "requirements": []},
            "budget": {"execution_count": 2, "max_in_flight": 2},
        },
    )
    local = target(next(item for item in OPERATORS if item["operator_id"] == "qgis-buffer"))
    remote = remote_target()
    queued = cluster.request(
        "POST",
        cluster.p + "/workflow-packages",
        package(local, remote),
        key="mixed-package",
    )
    cluster.launch("platform-worker")

    def frozen():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    package_view = eventually(frozen, timeout=40)
    workflow = {"kind": "WORKFLOW", "package_ref": package_view["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": task["task_id"],
            "targets": [workflow, local, remote],
            "resources": [ref],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    request = {
        "task_id": task["task_id"],
        "expected_spec_revision": 1,
        "target": workflow,
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
    command = cluster.request("POST", cluster.p + "/executions", request, key="mixed-run")
    replay = cluster.request("POST", cluster.p + "/executions", request, key="mixed-run")
    assert replay["action_id"] == command["action_id"]
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=200)
    assert execution["state"] == "SUCCEEDED", execution
    assert "_remote_bindings" not in execution["request"]
    steps = cluster.request("GET", cluster.p + f"/executions/{execution['execution_id']}/steps")[
        "items"
    ]
    assert len(steps) == 2 and all(item["state"] == "SUCCEEDED" for item in steps)
    assert {tuple(item["call_path"]) for item in steps} == {("buffer",), ("summary",)}
    assert execution["runtime"]["publication"] == "COMMITTED"
    assert set(execution["runtime"]["publication_receipt"]["outputs"]) == {
        "buffer",
        "buffer_manifest",
        "buffer_method",
        "summary",
        "summary_method",
    }
    budget = cluster.request("GET", cluster.p + f"/tasks/{task['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        child = (
            connection.execute(
                sa.text(
                    "select action_id, payload from executions "
                    "where parent_execution_id=:parent "
                    "and payload->'target'->'execution_binding'->>'kind'='REMOTE'"
                ),
                {"parent": execution["execution_id"]},
            )
            .mappings()
            .one()
        )
        assert "_remote_binding" in child["payload"]
        assert (
            connection.scalar(
                sa.text("select count(*) from remote_calls where action_id=:action"),
                {"action": child["action_id"]},
            )
            == 1
        )
