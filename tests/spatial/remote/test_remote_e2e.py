import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from platform_execution.implementations import REMOTE_SPATIAL_SUMMARY_CONTRACT

from scripts.maintenance.project_environment import docker_executable
from tests.platform.database.processes import eventually
from tests.spatial.native.test_spatial_workflow_e2e import SPATIAL_IMPORT_READY_TIMEOUT


def test_real_http_remote_summary_publishes_once(remote_cluster, owner, database):
    (cluster, peer), (_, token) = remote_cluster, owner
    cluster.client.headers["Authorization"] = "Bearer " + token
    project = cluster.request("POST", "/api/v1/projects", {"name": "Remote spatial"})
    project_id = project["project_id"]
    p = "/api/v1/projects/" + project_id
    cluster.project, cluster.p = project_id, p
    binding = {
        "service_binding_id": "p43-peer",
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
    database.service_binding_create(project_id, binding)
    discovered = cluster.request("GET", p + "/service-bindings")["items"]
    assert len(discovered) == 1 and "credential_ref" not in discovered[0]

    raw = Path("tests/spatial/native/fixtures/points-utm.gpkg").read_bytes()
    upload = cluster.request(
        "POST",
        p + "/uploads",
        {
            "filename": "points.gpkg",
            "format": "geopackage",
            "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        },
    )
    upload_path = p + "/uploads/" + upload["upload_id"]
    cluster.put_content(upload_path + "/content", raw)
    cluster.request(
        "POST", upload_path + "/finalize", {"kind": "SPATIAL", "profile": "strict-spatial-v1"}
    )

    def imported():
        value = cluster.request("GET", upload_path)
        assert value["status"] != "FAILED", value
        return value if value["status"] == "READY" else None

    ready = eventually(imported, timeout=SPATIAL_IMPORT_READY_TIMEOUT)
    ref = ready["asset"]
    asset = cluster.request("GET", p + f"/assets/{ref['asset_id']}/versions/1")
    original = asset["manifest"]["representations"][0]
    task = cluster.request(
        "POST",
        p + "/tasks",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Remote spatial summary", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    target = {
        "kind": "OPERATOR",
        "operator_id": "spatial-vector-summary",
        "version_id": "1.0.0",
        "execution_binding": {
            "kind": "REMOTE",
            "service_binding_id": "p43-peer",
            "binding_revision": 1,
        },
    }
    cluster.request(
        "POST",
        p + "/grants",
        {
            "task_id": task["task_id"],
            "targets": [target],
            "resources": [ref],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    request = {
        "task_id": task["task_id"],
        "expected_spec_revision": 1,
        "target": target,
        "inputs": {
            "vector": {
                "asset": ref,
                "representation_id": "original",
                "representation_digest": original["digest"],
                "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
                "semantic_revision": None,
            }
        },
        "parameters": {"layer_id": "l0"},
    }
    command = cluster.request("POST", p + "/executions", request, key="remote-summary")
    replay = cluster.request("POST", p + "/executions", request, key="remote-summary")
    assert replay["action_id"] == command["action_id"]
    cluster.launch_platform_worker_lanes()
    cluster.launch("execution-worker")
    result = cluster.completed_execution(command, timeout=60)
    assert result["state"] == "SUCCEEDED", result
    assert "_remote_binding" not in result["request"]
    assert result["runtime"]["remote"]["remote_execution_id"].startswith("r")
    assert result["runtime"]["publication"] == "COMMITTED"
    budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", p + f"/tasks/{task['task_id']}/budget"))[
                "in_flight"
            ]
            == 0
            else None
        ),
        timeout=10,
    )
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 0, 0)
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("select count(*) from actions where task_id=:t"), {"t": task["task_id"]}
            )
            == 1
        )
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        remote = (
            connection.execute(
                sa.text("select * from remote_calls where action_id=:a"),
                {"a": command["action_id"]},
            )
            .mappings()
            .one()
        )
        assert remote["remote_state"] == "SUCCEEDED" and remote["executed"] is True
        assert (
            connection.scalar(
                sa.text("select count(*) from remote_observations where action_id=:a"),
                {"a": command["action_id"]},
            )
            >= 2
        )
    # Container internals are only observable for the in-process conformance peer.
    # A configured peer is deliberately a black box to the Execution service and
    # its host-level isolation is checked by the deployment acceptance script.
    if peer[2] != "local-host-service":
        return
    containers = subprocess.run(
        [
            docker_executable(),
            "ps",
            "-aq",
            "--filter",
            f"label=v7.action_id={command['action_id']}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert len(containers) == 1
    inspected = json.loads(
        subprocess.run(
            [docker_executable(), "inspect", containers[0]],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]
    host = inspected["HostConfig"]
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
    assert host["Memory"] == 1024**3 and host["NanoCpus"] == 10**9
    assert host["PidsLimit"] == 128 and host["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert inspected["State"]["Pid"] == 0
