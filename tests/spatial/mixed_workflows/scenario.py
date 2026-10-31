import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platform_core.application.catalog import OPERATORS, target

from tests.platform.database.processes import eventually
from tests.spatial.mixed_workflows.test_mixed_workflow import binding, remote_target
from tests.spatial.native.test_spatial_workflow_e2e import (
    SPATIAL_IMPORT_READY_TIMEOUT,
    SPATIAL_PACKAGE_READY_TIMEOUT,
    package,
)


def prepare_mixed(
    cluster,
    peer,
    database,
    token,
    *,
    key="mixed-reliability",
    agent_actor=None,
    agent_model_profile=None,
    submit=True,
):
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "Mixed reliability"})
    cluster.project = project["project_id"]
    cluster.p = "/api/v1/projects/" + cluster.project
    database.service_binding_create(cluster.project, binding(peer))

    raw = Path("tests/spatial/native/fixtures/points-utm.gpkg").read_bytes()
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
        data_worker = cluster.running.get("data-worker")
        assert data_worker is not None and data_worker.poll() is None, (
            "data worker exited while importing the mixed-workflow fixture"
        )
        value = cluster.request("GET", upload_path)
        assert value["status"] != "FAILED", value
        return value if value["status"] == "READY" else None

    ready = eventually(imported, timeout=SPATIAL_IMPORT_READY_TIMEOUT)
    ref = ready["asset"]
    asset = cluster.request("GET", cluster.p + f"/assets/{ref['asset_id']}/versions/1")
    original = asset["manifest"]["representations"][0]
    original_content = cluster.client.get(
        cluster.p + f"/assets/{ref['asset_id']}/versions/1/representations/original/content"
    ).content
    selection = {
        "asset": ref,
        "representation_id": "original",
        "representation_digest": original["digest"],
        "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
        "semantic_revision": None,
    }
    local = target(next(item for item in OPERATORS if item["operator_id"] == "qgis-buffer"))
    remote = remote_target()
    queued = cluster.request(
        "POST",
        cluster.p + "/workflow-packages",
        package(local, remote),
        key=key + "-package",
    )
    cluster.launch_platform_worker_lanes()

    def frozen():
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    package_view = eventually(frozen, timeout=SPATIAL_PACKAGE_READY_TIMEOUT)
    workflow = {"kind": "WORKFLOW", "package_ref": package_view["package_ref"]}
    frozen_workflow = {
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
    if agent_actor is None:
        task = cluster.request(
            "POST",
            cluster.p + "/tasks",
            {
                "execution_mode": "DIRECT",
                "spec": {"goal": "Mixed workflow reliability", "requirements": []},
                "budget": {"execution_count": 2, "max_in_flight": 2},
            },
        )
    else:
        from platform_agent_runtime.profiles import profile
        from platform_core.application.control import ControlService
        from platform_core.application.model_catalog import build_catalog

        from tests.agent.model.conftest import model_budget

        control = ControlService(
            database,
            agent_runtime_profile=profile(),
            agent_model_catalog=build_catalog(
                [agent_model_profile], agent_model_profile["profile_id"]
            ),
        )
        task = control.create_task(
            agent_actor,
            cluster.project,
            key + "-agent-task",
            {
                "execution_mode": "AGENT",
                "spec": {
                    "goal": "Run the frozen mixed local and remote workflow",
                    "requirements": [],
                    "agent_workflow": frozen_workflow,
                },
                "budget": {"execution_count": 2, "max_in_flight": 2},
                "model_budget": model_budget(max_model_calls=4),
            },
        )
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
        **frozen_workflow,
    }
    command = None
    if submit:
        command = cluster.request("POST", cluster.p + "/executions", request, key=key)
        replay = cluster.request("POST", cluster.p + "/executions", request, key=key)
        assert replay["action_id"] == command["action_id"]
    return {
        "command": command,
        "task": task,
        "workflow": workflow,
        "request": request,
        "input_ref": ref,
        "input_digest": original["digest"],
        "input_content": original_content,
    }
