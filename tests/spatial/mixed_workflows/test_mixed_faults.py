import json
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from scripts.maintenance.project_environment import docker_executable
from tests.platform.database.processes import eventually
from tests.platform.services.harness import kill_owned_test_container
from tests.platform.services.test_execution import proxy as service_proxy
from tests.spatial.mixed_workflows.scenario import prepare_mixed
from tests.spatial.mixed_workflows.test_mixed_workflow import binding


def input_is_unchanged(cluster, scenario):
    ref = scenario["input_ref"]
    asset = cluster.request("GET", cluster.p + f"/assets/{ref['asset_id']}/versions/1")
    original = asset["manifest"]["representations"][0]
    content = cluster.client.get(
        cluster.p + f"/assets/{ref['asset_id']}/versions/1/representations/original/content"
    ).content
    return original["digest"] == scenario["input_digest"] and content == scenario["input_content"]


def remote_child(cluster, parent):
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        return (
            connection.execute(
                sa.text(
                    "select * from executions where parent_execution_id=:parent "
                    "and payload->'target'->'execution_binding'->>'kind'='REMOTE'"
                ),
                {"parent": parent},
            )
            .mappings()
            .one()
        )


def accepted_remote(cluster):
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        return (
            connection.execute(
                sa.text(
                    "select r.*, e.parent_execution_id from remote_calls r "
                    "join executions e on e.action_id=r.action_id "
                    "where r.remote_execution_id is not null"
                )
            )
            .mappings()
            .first()
        )


def cancel_parent(cluster, parent):
    return cluster.request(
        "POST",
        cluster.p + "/executions/" + parent + "/cancel",
        {"expected_spec_revision": 1},
        key="cancel-" + parent,
    )


@pytest.mark.parametrize(
    ("faults", "expected_used", "expected_failure"),
    [
        ({"reject_submit": True}, 1, "REMOTE_REJECTED"),
        ({"terminal_mode": "bad_digest"}, 2, "REMOTE_PROTOCOL_VIOLATION"),
        ({"terminal_mode": "missing_port"}, 2, "REMOTE_PROTOCOL_VIOLATION"),
    ],
)
def test_remote_failure_never_publishes_a_partial_group(
    fault_remote_cluster, owner, database, faults, expected_used, expected_failure
):
    cluster, peer, configure, _ = fault_remote_cluster
    configure(**faults)
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="mixed-failure")
    cluster.launch("execution-worker")
    result = cluster.completed_execution(scenario["command"], timeout=200)
    assert result["state"] == "FAILED", result
    assert result["runtime"]["publication"] == "ABORTED"
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1
    assert input_is_unchanged(cluster, scenario)
    budget = eventually(
        lambda: (
            value
            if (
                value := cluster.request(
                    "GET", cluster.p + f"/tasks/{scenario['task']['task_id']}/budget"
                )
            )["in_flight"]
            == 0
            and value["reserved"] == 0
            else None
        ),
        timeout=20,
    )
    child = remote_child(cluster, result["execution_id"])
    steps = cluster.request("GET", cluster.p + f"/executions/{result['execution_id']}/steps")[
        "items"
    ]
    assert (budget["used"], budget["reserved"]) == (expected_used, 0), json.dumps(
        {"child": dict(child), "steps": steps}, default=str
    )
    if expected_failure:
        assert child["runtime"]["remote"]["failure_code"] == expected_failure
    cluster.restart("execution-worker")
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1
    assert input_is_unchanged(cluster, scenario)


@pytest.mark.parametrize(
    "faults",
    [
        {"drop_submit_response_once": True},
        {"query_404_once": True},
        {"output_404_once": True},
        {"drop_output_once": True},
    ],
)
def test_transient_remote_faults_reuse_identity_and_settle_once(
    fault_remote_cluster, owner, database, faults
):
    cluster, peer, configure, original_peer = fault_remote_cluster
    configure(**faults)
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="mixed-transient")
    cluster.launch("execution-worker")
    result = cluster.completed_execution(scenario["command"], timeout=220)
    assert result["state"] == "SUCCEEDED", result
    assert result["runtime"]["publication"] == "COMMITTED"
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 3
    child = remote_child(cluster, result["execution_id"])
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        call = (
            connection.execute(
                sa.text("select * from remote_calls where action_id=:action"),
                {"action": child["action_id"]},
            )
            .mappings()
            .one()
        )
        assert call["remote_execution_id"] == child["runtime"]["remote"]["remote_execution_id"]
        assert (
            connection.scalar(
                sa.text("select count(*) from remote_calls where action_id=:action"),
                {"action": child["action_id"]},
            )
            == 1
        )
    budget = cluster.request("GET", cluster.p + f"/tasks/{scenario['task']['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)
    if original_peer[2] == "local-host-service":
        containers = subprocess.run(
            [
                docker_executable(),
                "ps",
                "-aq",
                "--filter",
                f"label=v7.action_id={child['action_id']}",
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
        assert inspected["State"]["Pid"] == 0


def test_data_commit_response_loss_recovers_one_group_and_one_receipt(
    fault_remote_cluster, owner, database
):
    cluster, peer, _, _ = fault_remote_cluster
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="publication-response-loss")
    url, dropped = service_proxy(cluster, "data", "/commit")
    cluster.env["V7_DATA_URL"] = url
    cluster.launch("execution-worker")
    eventually(dropped.exists, timeout=150)
    cluster.restart("execution-worker")
    result = cluster.completed_execution(scenario["command"], timeout=180)
    assert result["state"] == "SUCCEEDED", result
    assert result["runtime"]["publication"] == "COMMITTED"
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 3
    data_db, _ = cluster.service_databases
    with data_db.engine.connect() as connection:
        publication = (
            connection.execute(
                sa.text("select * from publications where execution_id=:execution"),
                {"execution": result["execution_id"]},
            )
            .mappings()
            .one()
        )
    assert publication["state"] == "COMMITTED"
    assert len(publication["receipt"]["assets"]) == 2


def test_data_rejects_commit_before_visibility_without_partial_assets(
    fault_remote_cluster, owner, database
):
    cluster, peer, _, _ = fault_remote_cluster
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="publication-rejected")
    ready = cluster.directory / "data-reject-ready.json"
    cluster.start(
        [
            sys.executable,
            str(Path(__file__).with_name("data_fault_proxy.py")),
            "--upstream",
            cluster.env["V7_DATA_URL"],
            "--ready",
            str(ready),
        ]
    )
    eventually(ready.exists, timeout=20)
    port = json.loads(ready.read_text())["port"]
    cluster.env["V7_DATA_URL"] = f"http://127.0.0.1:{port}"
    cluster.launch("execution-worker")
    result = cluster.completed_execution(scenario["command"], timeout=180)
    assert result["state"] == "FAILED", result
    assert result["runtime"]["publication"] == "ABORTED"
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1
    assert input_is_unchanged(cluster, scenario)
    data_db, _ = cluster.service_databases
    with data_db.engine.connect() as connection:
        publication = (
            connection.execute(
                sa.text("select * from publications where execution_id=:execution"),
                {"execution": result["execution_id"]},
            )
            .mappings()
            .one()
        )
    assert publication["state"] == "ABORTED"


def test_binding_disabled_before_remote_send_rejects_without_remote_identity(
    fault_remote_cluster, owner, database
):
    cluster, peer, _, _ = fault_remote_cluster
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="binding-disabled")
    database.service_binding_enable(cluster.project, "p432-peer", False)
    cluster.launch("execution-worker")
    result = cluster.completed_execution(scenario["command"], timeout=200)
    assert result["state"] == "FAILED"
    child = remote_child(cluster, result["execution_id"])
    assert child["state"] == "REJECTED" and child["executed"] is False
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        call = (
            connection.execute(
                sa.text("select * from remote_calls where action_id=:action"),
                {"action": child["action_id"]},
            )
            .mappings()
            .one()
        )
        assert call["remote_execution_id"] is None and call["phase"] == "STAGING_INPUT"
    budget = eventually(
        lambda: (
            value
            if (
                value := cluster.request(
                    "GET", cluster.p + f"/tasks/{scenario['task']['task_id']}/budget"
                )
            )["reserved"]
            == 0
            else None
        ),
        timeout=20,
    )
    assert (budget["used"], budget["in_flight"]) == (1, 0)
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_binding_disabled_after_upload_but_before_submit_never_calls_remote(
    fault_remote_cluster, owner, database
):
    cluster, peer, configure, _ = fault_remote_cluster
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="binding-second-permit")
    configure(hold_upload=True)
    cluster.launch("execution-worker")
    eventually((configure.directory / "upload-blocked").exists, timeout=120)
    database.service_binding_enable(cluster.project, "p432-peer", False)
    (configure.directory / "upload-release").write_text("release")
    result = cluster.completed_execution(scenario["command"], timeout=160)
    assert result["state"] == "FAILED", result
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        current = connection.execute(sa.text("select * from remote_calls")).mappings().one()
    assert current["remote_execution_id"] is None
    assert remote_child(cluster, result["execution_id"])["executed"] is False
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_started_remote_node_keeps_frozen_revision_after_disable_and_revision_change(
    fault_remote_cluster, owner, database
):
    cluster, peer, configure, _ = fault_remote_cluster
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="binding-frozen")
    database.service_binding_revise(cluster.project, "p432-peer", binding(peer))
    configure(query_404_always=True)
    cluster.launch("execution-worker")
    _, execution_db = cluster.service_databases

    def accepted_remote():
        with execution_db.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.text(
                        "select r.*, e.parent_execution_id from remote_calls r "
                        "join executions e on e.action_id=r.action_id "
                        "where r.remote_execution_id is not null"
                    )
                )
                .mappings()
                .first()
            )
        return row

    accepted = eventually(accepted_remote, timeout=120)
    assert accepted["binding"]["revision"] == 1
    database.service_binding_enable(cluster.project, "p432-peer", False)
    configure()
    result = cluster.completed_execution(scenario["command"], timeout=200)
    assert result["state"] == "SUCCEEDED", result
    child = remote_child(cluster, result["execution_id"])
    assert child["payload"]["_remote_binding"]["revision"] == 1
    assert child["runtime"]["remote"]["remote_execution_id"] == accepted["remote_execution_id"]
    budget = cluster.request("GET", cluster.p + f"/tasks/{scenario['task']['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)


def test_cancel_404_keeps_parent_pending_and_reuses_remote_identity(
    fault_remote_cluster, owner, database
):
    cluster, peer, configure, _ = fault_remote_cluster
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="cancel-404")
    configure(query_404_always=True)
    cluster.launch("execution-worker")
    accepted = eventually(lambda: accepted_remote(cluster), timeout=120)
    configure(cancel_404_always=True)
    cancel_parent(cluster, accepted["parent_execution_id"])

    def pending():
        value = cluster.request("GET", cluster.p + "/executions/" + accepted["parent_execution_id"])
        return (
            value
            if value["runtime"].get("cancellation", {}).get("state") == "PENDING_REMOTE"
            else None
        )

    eventually(pending, timeout=45)
    budget = cluster.request("GET", cluster.p + f"/tasks/{scenario['task']['task_id']}/budget")
    # If the remote action has not started, its reservation remains.  A fast
    # real peer may already have started it, in which case the same unit is
    # irreversibly consumed.  Neither observation permits the unit to vanish.
    assert budget["used"] + budget["reserved"] == 2
    assert accepted_remote(cluster)["remote_execution_id"] == accepted["remote_execution_id"]
    configure()
    result = cluster.completed_execution(scenario["command"], timeout=160)
    assert result["state"] == "CANCELLED", result
    assert result["runtime"]["cancellation"]["state"] == "ABORTED_BEFORE_COMMIT"
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_cancel_during_remote_output_visibility_never_publishes_private_files(
    fault_remote_cluster, owner, database
):
    cluster, peer, configure, _ = fault_remote_cluster
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="cancel-output")
    configure(output_404_always=True)
    cluster.launch("execution-worker")
    _, execution_db = cluster.service_databases

    def succeeded_remote():
        with execution_db.engine.connect() as connection:
            return (
                connection.execute(
                    sa.text(
                        "select r.*, e.parent_execution_id from remote_calls r "
                        "join executions e on e.action_id=r.action_id "
                        "where r.remote_state='SUCCEEDED'"
                    )
                )
                .mappings()
                .first()
            )

    remote = eventually(succeeded_remote, timeout=150)
    cancel_parent(cluster, remote["parent_execution_id"])
    result = cluster.completed_execution(scenario["command"], timeout=160)
    assert result["state"] == "CANCELLED", result
    assert result["runtime"]["publication"] == "ABORTED"
    child = remote_child(cluster, result["execution_id"])
    incoming = Path(cluster.env["V7_EXECUTION_WORKDIR"]) / child["execution_id"] / "remote-incoming"
    output = Path(cluster.env["V7_EXECUTION_WORKDIR"]) / child["execution_id"] / "output"
    assert list(output.glob("*.json")) == []
    assert not incoming.exists() or not any(incoming.rglob("*.download"))
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_combined_worker_leader_and_peer_restart_keeps_all_identities(
    fault_remote_cluster, owner, database
):
    cluster, peer, configure, original_peer = fault_remote_cluster
    controller = original_peer[5]
    if controller is None:
        pytest.skip("configured peer lifecycle is controlled by the standard acceptance script")
    scenario = prepare_mixed(cluster, peer, database, owner[1], key="combined-recovery")
    configure(drop_submit_response_once=True, query_404_always=True)
    worker = cluster.launch("execution-worker")
    accepted = eventually(lambda: accepted_remote(cluster), timeout=150)

    def recoverable_parent():
        value = cluster.request("GET", cluster.p + "/executions/" + accepted["parent_execution_id"])
        root = Path(cluster.env["V7_EXECUTION_WORKDIR"]) / value["execution_id"]
        return (
            value
            if value["runtime"].get("phase") == "ENGINE_RUNNING"
            and (root / "jobstore/shared/rootJobStoreID").is_file()
            else None
        )

    parent = eventually(recoverable_parent, timeout=60)
    cluster.kill(worker)
    engine = f"v7-p4-{parent['execution_id']}-e{parent['runtime']['engine_epoch']}"
    kill_owned_test_container(engine, cluster.test_run_id)
    controller["restart"]()
    configure()
    cluster.launch("execution-worker")
    result = cluster.completed_execution(scenario["command"], timeout=220)
    assert result["state"] == "SUCCEEDED", result
    child = remote_child(cluster, result["execution_id"])
    assert child["action_id"] == accepted["action_id"]
    assert child["runtime"]["remote"]["remote_execution_id"] == accepted["remote_execution_id"]
    _, execution_db = cluster.service_databases
    with execution_db.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("select count(*) from remote_calls where action_id=:action"),
                {"action": child["action_id"]},
            )
            == 1
        )
    budget = cluster.request("GET", cluster.p + f"/tasks/{scenario['task']['task_id']}/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (2, 0, 0)
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 3
