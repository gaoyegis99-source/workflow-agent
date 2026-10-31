import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.maintenance.project_environment import docker_executable
from tests.execution.workflows.test_workflow_registry import PARAMETERS
from tests.platform.database.processes import eventually
from tests.platform.services.harness import kill_owned_test_container

# Package validation runs on a dedicated durable worker lane.  A full P2 run
# can delay that subprocess beyond the former 30-second boundary while the
# shared PostgreSQL and rootless runtime finish earlier integration teardown.
# Keep the wait bounded and fail immediately below if any lane actually exits.
PACKAGE_READY_TIMEOUT = 90


def assert_engines_stopped_cleanly(execution):
    for epoch in range(1, execution["runtime"]["engine_epoch"] + 1):
        name = f"v7-p4-{execution['execution_id']}-e{epoch}"
        inspected = subprocess.run(
            [docker_executable(), "inspect", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        info = json.loads(inspected.stdout)[0]
        assert info["HostConfig"]["Init"] is True
        assert info["Config"]["User"] not in {"", "0", "root", "0:0"}
        assert info["HostConfig"]["ReadonlyRootfs"] is True
        assert info["HostConfig"]["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in info["HostConfig"]["SecurityOpt"]
        assert info["State"]["Running"] is False
        assert info["State"]["Pid"] == 0
        top = subprocess.run(
            [docker_executable(), "top", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert top.returncode != 0
        logs = subprocess.run(
            [docker_executable(), "logs", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert "Processes were not reaped in groups" not in logs.stdout + logs.stderr


def package(targets):
    documents = {
        "main": {
            "cwlVersion": "v1.2",
            "class": "Workflow",
            "inputs": {"observations": "File", "parameters": PARAMETERS},
            "outputs": {
                "complete_result": {
                    "type": "File",
                    "outputSource": "complete/result",
                },
                "complete_table": {
                    "type": "File",
                    "outputSource": "complete/table",
                },
                "complete_method": {
                    "type": "File",
                    "outputSource": "complete/method",
                },
                "trend_result": {"type": "File", "outputSource": "trend/result"},
                "trend_table": {"type": "File", "outputSource": "trend/table"},
                "trend_method": {"type": "File", "outputSource": "trend/method"},
            },
            "steps": {
                name: {
                    "run": name,
                    "in": {"observations": "observations", "parameters": "parameters"},
                    "out": ["result", "table", "method"],
                }
                for name in ("complete", "trend")
            },
        }
    }
    return {
        "name": "P4 two operator workflow",
        "entrypoint": "main",
        "profile": "v7-cwl-p4-v1",
        "documents": documents,
        "implementations": {"complete": targets[0], "trend": targets[1]},
    }


def chained_package(targets):
    value = package(targets)
    main = value["documents"]["main"]
    main["outputs"] = {
        "result": {"type": "File", "outputSource": "trend/result"},
    }
    main["steps"]["trend"]["in"]["observations"] = "complete/table"
    return value | {"name": "P4 verified derived File workflow"}


def submit_workflow(cluster, token, *, budget=2, definition=None):
    cluster.bootstrap(token, budget=budget)
    definition = definition(cluster.targets) if callable(definition) else definition
    queued = cluster.request(
        "POST", cluster.p + "/workflow-packages", definition or package(cluster.targets)
    )
    workers = cluster.launch_platform_worker_lanes()

    def validated():
        status = workers.poll()
        assert status is None, (
            f"Platform worker lane exited with status {status}; inspect {cluster.directory}"
        )
        value = cluster.request("GET", queued["status_url"])
        assert value["status"] != "REJECTED", value
        return value if value["status"] == "READY" else None

    frozen = eventually(validated, timeout=PACKAGE_READY_TIMEOUT)
    workflow_target = {"kind": "WORKFLOW", "package_ref": frozen["package_ref"]}
    cluster.request(
        "POST",
        cluster.p + "/grants",
        {
            "task_id": cluster.task,
            "targets": [workflow_target],
            "resources": [cluster.selection["asset"]],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    return cluster.request(
        "POST",
        cluster.p + "/executions",
        {
            "task_id": cluster.task,
            "expected_spec_revision": 1,
            "target": workflow_target,
            "inputs": {
                "observations": cluster.selection,
                "parameters": cluster.parameters,
            },
            "max_node_executions": 2,
        },
    )


def test_real_workflow_publishes_two_assets_and_passes_independent_check(p4_cluster, owner):
    from platform_execution.adapters.repository import Repository

    cluster = p4_cluster
    command = submit_workflow(cluster, owner[1], budget=3)
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=120)
    assert execution["state"] == "SUCCEEDED", execution
    assert_engines_stopped_cleanly(execution)
    steps = cluster.request(
        "GET", cluster.p + "/executions/" + execution["execution_id"] + "/steps"
    )["items"]
    assert len(steps) == 2 and all(item["state"] == "SUCCEEDED" for item in steps)
    persisted = Repository(cluster.service_databases[1]).children(execution["execution_id"])
    assert len(persisted) == 2
    assert all("trace" not in item["payload"]["node_request"] for item in persisted)
    budget_path = cluster.p + "/tasks/" + cluster.task + "/budget"
    budget = eventually(
        lambda: value if (value := cluster.request("GET", budget_path))["in_flight"] == 0 else None
    )
    assert (budget["used"], budget["reserved"]) == (2, 0)
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 3

    task_path = cluster.p + "/tasks/" + cluster.task
    bindings = []
    outputs = execution["runtime"]["publication_receipt"]["outputs"]
    for published_port in ("complete_result", "trend_result"):
        published = outputs[published_port]
        ref = published["asset"]
        asset = cluster.request(
            "GET", cluster.p + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
        )
        representation_id = published["representation_id"]
        representation = next(
            item
            for item in asset["manifest"]["representations"]
            if item["representation_id"] == representation_id
        )
        node = next(
            item for item in steps if item["node_id"] == asset["manifest"]["workflow"]["member_id"]
        )
        bindings.append(
            {
                "role_id": asset["manifest"]["source"]["target"]["operator_id"],
                "output_ref": {
                    "kind": "WORKFLOW_OUTPUT",
                    "parent_action_id": execution["action_id"],
                    "parent_execution_id": execution["execution_id"],
                    "node_action_id": node["node_id"],
                    "node_execution_id": node["execution_id"],
                    "call_path": node["call_path"],
                    "scatter_path": node["scatter_indices"],
                    "published_output_port": published_port,
                    "published_path": [],
                    "output_port": representation_id,
                    "asset_ref": ref,
                    "representation_id": representation_id,
                    "representation_digest": representation["digest"],
                    "schema_id": representation["schema_id"],
                    "schema_source": "MANIFEST",
                },
            }
        )
    bindings.sort(key=lambda item: item["role_id"])
    deliverable = cluster.request(
        "POST",
        task_path + "/deliverables",
        {"expected_spec_revision": 1, "role_bindings": bindings},
    )
    receipt = cluster.request(
        "POST",
        task_path + "/acceptance",
        {"expected_spec_revision": 1, "deliverable_id": deliverable["deliverable_id"]},
    )

    def accepted():
        value = cluster.request("GET", receipt["status_url"])
        return value if value["status"] != "QUEUED" else None

    report = eventually(accepted, timeout=60)
    assert report["status"] == "PASS", report
    assert cluster.request("GET", task_path)["lifecycle"] == "COMPLETED"


def test_execution_worker_restart_recovers_original_nodes(p4_cluster, owner):
    cluster = p4_cluster
    command = submit_workflow(cluster, owner[1])
    worker = cluster.launch("execution-worker")

    def running():
        receipt = cluster.request("GET", command["status_url"])
        if not receipt["result"]:
            return None
        value = cluster.request(
            "GET", cluster.p + "/executions/" + receipt["result"]["execution_id"]
        )
        return value if value["state"] == "RUNNING" else None

    before = eventually(running, timeout=40)
    cluster.kill(worker)
    cluster.launch("execution-worker")
    after = cluster.completed_execution(command, timeout=120)
    assert after["state"] == "SUCCEEDED"
    assert after["execution_id"] == before["execution_id"]
    steps = cluster.request("GET", cluster.p + "/executions/" + after["execution_id"] + "/steps")[
        "items"
    ]
    assert len(steps) == 2 and len({item["node_id"] for item in steps}) == 2


def test_execution_api_restart_keeps_bridge_on_original_nodes(p4_cluster, owner):
    cluster = p4_cluster
    command = submit_workflow(cluster, owner[1])
    cluster.launch("execution-worker")

    def engine_running():
        receipt = cluster.request("GET", command["status_url"])
        if not receipt["result"]:
            return None
        value = cluster.request(
            "GET", cluster.p + "/executions/" + receipt["result"]["execution_id"]
        )
        return value if value["runtime"].get("phase") == "ENGINE_RUNNING" else None

    before = eventually(engine_running, timeout=40)
    cluster.restart("execution")
    after = cluster.completed_execution(command, timeout=120)
    assert after["state"] == "SUCCEEDED", after
    assert after["execution_id"] == before["execution_id"]
    steps = cluster.request("GET", cluster.p + "/executions/" + after["execution_id"] + "/steps")[
        "items"
    ]
    assert len(steps) == 2 and len({item["node_id"] for item in steps}) == 2


def test_killed_toil_engine_restarts_original_jobstore(p4_cluster, owner):
    from pathlib import Path

    from platform_execution.adapters.repository import Repository

    cluster = p4_cluster
    command = submit_workflow(cluster, owner[1])
    cluster.launch("execution-worker")

    def recoverable_engine_running():
        receipt = cluster.request("GET", command["status_url"])
        if not receipt["result"]:
            return None
        value = cluster.request(
            "GET", cluster.p + "/executions/" + receipt["result"]["execution_id"]
        )
        if value["runtime"].get("phase") != "ENGINE_RUNNING":
            return None
        root = Path(cluster.env["V7_EXECUTION_WORKDIR"]) / value["execution_id"]
        durable_root = root / "jobstore" / "shared" / "rootJobStoreID"
        return value if durable_root.is_file() else None

    value = eventually(recoverable_engine_running, timeout=40)
    repository = Repository(cluster.service_databases[1])
    original_plan = repository.get(execution=value["execution_id"])["payload"]["execution_plan"]
    name = f"v7-p4-{value['execution_id']}-e{value['runtime']['engine_epoch']}"
    kill_owned_test_container(name, cluster.test_run_id)
    result = cluster.completed_execution(command, timeout=120)
    assert result["state"] == "SUCCEEDED", result
    assert result["runtime"]["engine_epoch"] == 2
    recovered_plan = repository.get(execution=value["execution_id"])["payload"]["execution_plan"]
    assert recovered_plan == original_plan
    assert recovered_plan["plan_digest"] == original_plan["plan_digest"]
    assert_engines_stopped_cleanly(result)


def test_verified_private_node_output_requires_derived_permission_and_stays_atomic(
    p4_cluster, owner
):
    cluster = p4_cluster
    command = submit_workflow(cluster, owner[1], definition=chained_package)
    cluster.launch("execution-worker")
    result = cluster.completed_execution(command, timeout=150)
    assert result["state"] == "FAILED", result
    assert_engines_stopped_cleanly(result)
    steps = cluster.request("GET", cluster.p + "/executions/" + result["execution_id"] + "/steps")[
        "items"
    ]
    assert {item["state"] for item in steps} == {"SUCCEEDED", "FAILED"}
    assert all(item["executed"] for item in steps)
    budget_path = cluster.p + "/tasks/" + cluster.task + "/budget"
    budget = eventually(
        lambda: value if (value := cluster.request("GET", budget_path))["in_flight"] == 0 else None
    )
    assert (budget["used"], budget["reserved"]) == (2, 0)
    # Only the uploaded source is visible; the successful first node remains private.
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_running_workflow_cancel_stops_new_nodes_and_aborts_group_publication(p4_cluster, owner):
    cluster = p4_cluster
    command = submit_workflow(cluster, owner[1])
    cluster.launch("execution-worker")

    def engine_running():
        receipt = cluster.request("GET", command["status_url"])
        if not receipt["result"]:
            return None
        value = cluster.request(
            "GET", cluster.p + "/executions/" + receipt["result"]["execution_id"]
        )
        return value if value["runtime"].get("phase") == "ENGINE_RUNNING" else None

    running = eventually(engine_running, timeout=40)
    cluster.request(
        "POST",
        cluster.p + "/executions/" + running["execution_id"] + "/cancel",
        {"expected_spec_revision": 1},
    )
    result = cluster.completed_execution(command, timeout=90)
    assert result["state"] == "CANCELLED", result
    assert result["runtime"]["publication"] == "ABORTED"
    assert_engines_stopped_cleanly(result)
    budget_path = cluster.p + "/tasks/" + cluster.task + "/budget"
    budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", budget_path))["in_flight"] == 0
            and value["reserved"] == 0
            else None
        ),
        timeout=45,
    )
    assert 0 <= budget["used"] <= 2
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_queued_workflow_cancel_never_starts_toil(p4_cluster, owner):
    cluster = p4_cluster
    command = submit_workflow(cluster, owner[1])

    def accepted():
        receipt = cluster.request("GET", command["status_url"])
        return receipt if receipt["result"] else None

    receipt = eventually(accepted, timeout=30)
    execution_id = receipt["result"]["execution_id"]
    cluster.request(
        "POST",
        cluster.p + "/executions/" + execution_id + "/cancel",
        {"expected_spec_revision": 1},
    )
    cluster.launch("execution-worker")
    result = cluster.completed_execution(command, timeout=60)
    # A cancellation confirmed before execution starts is the existing P3/P4
    # unexecuted rejection fact; no runtime cleanup is required.
    assert result["state"] == "REJECTED", result
    assert result["executed"] is False
    absent = subprocess.run(
        [docker_executable(), "inspect", f"v7-p4-{execution_id}-e1"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert absent.returncode != 0
    budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget"))[
                "reserved"
            ]
            == 0
            else None
        ),
        timeout=30,
    )
    assert (budget["used"], budget["in_flight"]) == (0, 0)


def test_cancel_while_bridge_downloads_output_reaps_processes(p4_cluster, owner):
    cluster = p4_cluster
    ready = cluster.directory / "blocking-proxy-ready.json"
    blocked = cluster.directory / "blocking-proxy-blocked.txt"
    release = cluster.directory / "blocking-proxy-release"
    proxy = cluster.start(
        [
            sys.executable,
            Path(__file__).with_name("blocking_proxy.py"),
            "--upstream",
            cluster.env["V7_EXECUTION_URL"],
            "--host",
            "0.0.0.0",
            "--ready",
            ready,
            "--blocked",
            blocked,
            "--release",
            release,
        ],
        cluster.env,
    )
    cluster.running["blocking-proxy"] = proxy
    eventually(ready.exists, timeout=20)
    port = json.loads(ready.read_text())["port"]
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = f"http://{cluster.container_callback_host}:{port}"

    command = submit_workflow(cluster, owner[1])
    cluster.launch("execution-worker")
    eventually(blocked.exists, timeout=90)
    receipt = cluster.request("GET", command["status_url"])
    execution_id = receipt["result"]["execution_id"]
    cluster.request(
        "POST",
        cluster.p + "/executions/" + execution_id + "/cancel",
        {"expected_spec_revision": 1},
    )
    result = cluster.completed_execution(command, timeout=90)
    release.write_text("release")
    assert result["state"] == "CANCELLED", result
    assert result["runtime"]["publication"] == "ABORTED"
    assert_engines_stopped_cleanly(result)
    budget = eventually(
        lambda: (
            value
            if (value := cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget"))[
                "in_flight"
            ]
            == 0
            and value["reserved"] == 0
            else None
        ),
        timeout=45,
    )
    assert budget["used"] == 2
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1
