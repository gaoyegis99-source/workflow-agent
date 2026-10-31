import json
import subprocess
from pathlib import Path

from platform_contracts.engine_runtime import ON_PROFILE_ID, diagnostic, profile
from platform_execution.adapters.repository import Repository

from scripts.maintenance.project_environment import docker_executable
from tests.execution.workflows.test_workflow_e2e import (
    assert_engines_stopped_cleanly,
    chained_package,
    submit_workflow,
)
from tests.platform.database.processes import eventually
from tests.platform.services.harness import kill_owned_test_container

# The execution worker bounds each rootless Docker operation at 30 seconds and
# reconciles a timed-out external effect instead of duplicating it.  Reaching
# ENGINE_RUNNING must therefore allow several bounded create/inspect retries
# when the daemon is under the sustained load of the full P2 suite.
ENGINE_RUNNING_TIMEOUT = 180


def _running(cluster, command, *, recoverable=False):
    receipt = cluster.request("GET", command["status_url"])
    if not receipt["result"]:
        return None
    execution = cluster.request(
        "GET", cluster.p + "/executions/" + receipt["result"]["execution_id"]
    )
    if execution["runtime"].get("phase") != "ENGINE_RUNNING":
        return None
    if recoverable:
        root = Path(cluster.env["V7_EXECUTION_WORKDIR"]) / execution["execution_id"]
        if not (root / "jobstore" / "shared" / "rootJobStoreID").is_file():
            return None
    return execution


def test_chaining_on_publishes_once_with_frozen_profile(p4_chaining_on_cluster, owner):
    cluster = p4_chaining_on_cluster
    command = submit_workflow(cluster, owner[1])
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command, timeout=120)
    assert execution["state"] == "SUCCEEDED", execution
    assert execution["runtime"]["engine_profile"] == diagnostic(profile(ON_PROFILE_ID))
    assert_engines_stopped_cleanly(execution)
    name = f"v7-p4-{execution['execution_id']}-e1"
    info = json.loads(subprocess.check_output([docker_executable(), "inspect", name], text=True))[0]
    labels = info["Config"]["Labels"]
    assert labels["v7.engine.profile"] == ON_PROFILE_ID
    assert labels["v7.engine.profile-digest"] == profile(ON_PROFILE_ID)["profile_digest"]
    children = Repository(cluster.service_databases[1]).children(execution["execution_id"])
    assert len(children) == 2
    assert len({item["action_id"] for item in children}) == 2
    budget = cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)


def test_chaining_on_survives_api_and_worker_restart_without_profile_change(
    p4_chaining_on_cluster, owner
):
    cluster = p4_chaining_on_cluster
    command = submit_workflow(cluster, owner[1])
    worker = cluster.launch("execution-worker")
    before = eventually(lambda: _running(cluster, command), timeout=ENGINE_RUNNING_TIMEOUT)
    cluster.kill(worker)
    # The normal service defaults to off. The accepted execution must retain on.
    cluster.restart("execution")
    cluster.launch("execution-worker")
    result = cluster.completed_execution(command, timeout=120)
    assert result["state"] == "SUCCEEDED", result
    assert result["execution_id"] == before["execution_id"]
    assert result["runtime"]["engine_profile"] == diagnostic(profile(ON_PROFILE_ID))
    assert_engines_stopped_cleanly(result)


def test_chaining_on_leader_restart_uses_original_jobstore_and_nodes(p4_chaining_on_cluster, owner):
    cluster = p4_chaining_on_cluster
    command = submit_workflow(cluster, owner[1])
    cluster.launch("execution-worker")
    before = eventually(
        lambda: _running(cluster, command, recoverable=True), timeout=ENGINE_RUNNING_TIMEOUT
    )
    name = f"v7-p4-{before['execution_id']}-e{before['runtime']['engine_epoch']}"
    kill_owned_test_container(name, cluster.test_run_id)
    result = cluster.completed_execution(command, timeout=120)
    assert result["state"] == "SUCCEEDED", result
    assert result["runtime"]["engine_epoch"] == 2
    assert result["runtime"]["engine_profile"] == diagnostic(profile(ON_PROFILE_ID))
    steps = cluster.request("GET", cluster.p + "/executions/" + result["execution_id"] + "/steps")[
        "items"
    ]
    assert len(steps) == 2 and len({item["node_id"] for item in steps}) == 2
    assert_engines_stopped_cleanly(result)


def test_chaining_on_does_not_duplicate_sequential_node_or_budget(p4_chaining_on_cluster, owner):
    cluster = p4_chaining_on_cluster
    command = submit_workflow(cluster, owner[1], definition=chained_package)
    cluster.launch("execution-worker")
    result = cluster.completed_execution(command, timeout=150)
    assert result["state"] == "FAILED", result
    assert result["runtime"]["engine_profile"] == diagnostic(profile(ON_PROFILE_ID))
    children = Repository(cluster.service_databases[1]).children(result["execution_id"])
    assert len(children) == 2 and len({item["action_id"] for item in children}) == 2
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
    assert (budget["used"], budget["reserved"]) == (2, 0)
    assert_engines_stopped_cleanly(result)


def test_chaining_on_cancel_stops_new_compute_and_aborts_publication(p4_chaining_on_cluster, owner):
    cluster = p4_chaining_on_cluster
    command = submit_workflow(cluster, owner[1])
    cluster.launch("execution-worker")
    running = eventually(lambda: _running(cluster, command), timeout=ENGINE_RUNNING_TIMEOUT)
    cluster.request(
        "POST",
        cluster.p + "/executions/" + running["execution_id"] + "/cancel",
        {"expected_spec_revision": 1},
    )
    result = cluster.completed_execution(command, timeout=90)
    assert result["state"] == "CANCELLED", result
    assert result["runtime"]["publication"] == "ABORTED"
    assert result["runtime"]["engine_profile"] == diagnostic(profile(ON_PROFILE_ID))
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
    assert budget["reserved"] == 0
