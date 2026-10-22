import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import sqlalchemy as sa
from platform_contracts.service import ServiceFault
from platform_infrastructure import tables

from tests.p2.processes import eventually


def test_transient_http_errors_use_bounded_backoff(tmp_path):
    from platform_execution.application.runner import Runner

    class Repository:
        def __init__(self):
            self.row = {
                "execution_id": "e-retry",
                "action_id": "a-retry",
                "generation": 1,
                "runtime": {"phase": "ACCEPTED"},
            }
            self.delays = []

        def claim(self, owner, lease_seconds):
            return self.row.copy()

        def change(self, row, **changes):
            self.row.update(changes)
            return self.row.copy()

        def release(self, row, delay):
            self.delays.append(delay)

    repository = Repository()
    runner = Runner(repository, tmp_path, None, None, None)

    def unavailable(row, root):
        raise ServiceFault("DEPENDENCY_UNAVAILABLE", "temporary", 503)

    runner.step = unavailable
    for _ in range(3):
        assert runner.tick()
    assert repository.delays == [1, 2, 4]
    assert repository.row["runtime"]["uncertain"]


def test_long_execution_step_renews_lease(service_databases, tmp_path):
    from platform_execution.adapters.repository import Repository
    from platform_execution.application.runner import Runner

    _, database = service_databases
    repository = Repository(database)
    repository.create("long-running-action", {"project_id": "project", "task_id": "task"})
    runner = Runner(repository, tmp_path, None, None, None)
    runner.lease_seconds, runner.heartbeat_seconds = 0.3, 0.08
    entered, resume = threading.Event(), threading.Event()

    def slow_step(row, root):
        entered.set()
        assert resume.wait(5)

    runner.step = slow_step
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(runner.tick)
        assert entered.wait(5)
        time.sleep(0.75)
        assert repository.claim("competing-worker", 0.3) is None
        resume.set()
        assert future.result(timeout=5)
    assert repository.get(action="long-running-action")["generation"] == 1


def test_committed_submission_replays_without_data_service(cluster, owner):
    cluster.bootstrap(owner[1])
    command = cluster.submit(key="data-independent-replay")
    body = {
        "task_id": cluster.task,
        "expected_spec_revision": 1,
        "target": cluster.targets[0],
        "inputs": {"observations": cluster.selection},
        "parameters": cluster.parameters,
    }
    cluster.kill(cluster.running["data"])
    url = cluster.p + "/executions"
    headers = {"Idempotency-Key": "data-independent-replay"}
    replay = cluster.client.post(url, headers=headers, json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["command_id"] == command["command_id"]
    changed = cluster.client.post(url, headers=headers, json=body | {"task_id": "other"})
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    replacement = cluster.database.create_principal("replacement owner")
    cluster.database.set_member(cluster.project, replacement, "OWNER", actor=owner[0])
    cluster.database.set_member(cluster.project, owner[0].principal_id, "VIEWER", actor=owner[0])
    denied = cluster.client.post(url, headers=headers, json=body)
    assert denied.status_code == 403
    cluster.database.revoke_token(owner[0].token_id)
    revoked = cluster.client.post(url, headers=headers, json=body)
    assert revoked.status_code == 401


def test_interrupted_start_and_abort_outage_never_recompute_or_charge(cluster, owner):
    from platform_execution.adapters.docker import Docker
    from platform_execution.adapters.http import Client
    from platform_execution.adapters.tables import executions
    from platform_execution.worker import Worker

    cluster.bootstrap(owner[1])
    command = cluster.submit()
    cluster.launch("platform-worker")
    receipt = eventually(lambda: cluster.request("GET", command["status_url"])["result"])
    data = Client(cluster.env["V7_DATA_URL"], cluster.env["V7_EXECUTION_DATA_TOKEN"])
    platform = Client(cluster.env["V7_PLATFORM_URL"], cluster.env["V7_EXECUTION_PLATFORM_TOKEN"])
    docker = Docker(cluster.env["V7_OPERATOR_IMAGE"], cluster.env["V7_CHECKER_IMAGE"])
    database = cluster.service_databases[1]
    worker = Worker(database, cluster.env["V7_EXECUTION_WORKDIR"], docker, data, platform)
    identity = receipt["execution_id"]

    def advance(predicate):
        worker.tick()
        row = worker.repo.get(execution=identity)
        return row if predicate(row) else None

    try:
        row = eventually(lambda: advance(lambda r: r["runtime"]["phase"] == "CREATED"))
        assert docker.inspect(row["container_id"])["State"]["StartedAt"].startswith("0001-")
        # Crash after persisting start intent, before sending Docker start.
        with database.engine.begin() as c:
            c.execute(
                executions.update()
                .where(executions.c.execution_id == identity)
                .values(runtime=row["runtime"] | {"phase": "START_INTENT"})
            )
        cluster.kill(cluster.running["data"])
        unknown = eventually(lambda: advance(lambda r: r["runtime"].get("uncertain")), timeout=30)
        assert unknown["runtime"]["phase"] == "STOPPING_UNSTARTED"
        assert docker.inspect("v7-p3-" + identity) is None
        assert not unknown["executed"]
        budget = cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")
        assert (budget["used"], budget["reserved"], budget["in_flight"]) == (0, 1, 1)
        cluster.launch("data")
        final = eventually(lambda: advance(lambda r: r["state"] == "FAILED"), timeout=45)
        assert final["runtime"]["publication"] == "ABORTED"
        assert final["runtime"]["error"] == "START_INTERRUPTED"
        assert not final["executed"]
        assert docker.inspect("v7-p3-" + identity) is None
        eventually(
            lambda: (
                cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")[
                    "in_flight"
                ]
                == 0
            ),
            timeout=45,
        )
        assert cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["used"] == 0
    finally:
        data.close()
        platform.close()


def test_real_container_queued_budget_publication_and_replay(cluster, owner):
    cluster.bootstrap(owner[1])
    command = cluster.submit(key="original")
    assert cluster.submit(key="original")["command_id"] == command["command_id"]
    cluster.launch("platform-worker")
    receipt = eventually(
        lambda: r if (r := cluster.request("GET", command["status_url"]))["result"] else None
    )
    budget = cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")
    assert budget["used"] == 0 and budget["reserved"] == 1 and budget["in_flight"] == 1
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command)
    assert execution["state"] == "SUCCEEDED", execution
    assert execution["executed"] and execution["runtime"]["publication"] == "COMMITTED"
    info = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "v7-p3-" + execution["execution_id"]], text=True
        )
    )[0]
    assert info["HostConfig"]["ReadonlyRootfs"] and info["HostConfig"]["NetworkMode"] == "none"
    assert info["Config"]["User"].split(":")[0] != "0"
    assert info["HostConfig"]["Memory"] == 1024**3
    assert info["HostConfig"]["NanoCpus"] == 10**9
    assert not any("TOKEN" in value or "DATABASE" in value for value in info["Config"]["Env"])
    cluster.restart("execution")
    cluster.restart("platform")
    assert cluster.submit(key="original")["command_id"] == command["command_id"]
    assert cluster.completed_execution(command)["execution_id"] == receipt["result"]["execution_id"]
    with cluster.database.engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(tables.actions)) == 1
        assert c.scalar(sa.select(sa.func.count()).select_from(tables.reservations)) == 1
    assets = cluster.request("GET", cluster.p + "/assets")["items"]
    assert len(assets) == 2


def proxy(cluster, service, match):
    import sys
    from pathlib import Path

    marker = cluster.directory / "proxy-ready.json"
    dropped = cluster.directory / "proxy-dropped.json"
    cluster.start(
        [
            sys.executable,
            Path(__file__).with_name("fault_proxy.py"),
            "--upstream",
            cluster.env["V7_" + service.upper() + "_URL"],
            "--match",
            match,
            "--ready",
            marker,
            "--dropped",
            dropped,
        ]
    )
    eventually(marker.exists)
    url = "http://127.0.0.1:" + str(json.loads(marker.read_text())["port"])
    return url, dropped


def test_lost_submission_and_publication_recover_original_identities(cluster, owner):
    from platform_data.adapters import tables as data_tables
    from platform_execution.adapters import tables as execution_tables

    cluster.bootstrap(owner[1])
    command = cluster.submit()
    url, dropped = proxy(cluster, "execution", "/internal/actions/")
    cluster.env["V7_EXECUTION_URL"] = url
    cluster.launch("platform-worker")
    eventually(dropped.exists)
    cluster.restart("platform-worker")
    eventually(lambda: cluster.request("GET", command["status_url"])["result"], timeout=45)
    # A second independent proxy drops a committed publication response.
    dropped.unlink()
    marker = cluster.directory / "proxy-ready.json"
    marker.unlink()
    url, publication_dropped = proxy(cluster, "data", "/commit")
    cluster.env["V7_DATA_URL"] = url
    cluster.launch("execution-worker")
    eventually(publication_dropped.exists, timeout=30)
    cluster.restart("execution-worker")
    execution = cluster.completed_execution(command)
    assert execution["state"] == "SUCCEEDED", execution
    for db, table in [
        (cluster.service_databases[1], execution_tables.executions),
        (cluster.service_databases[0], data_tables.publications),
    ]:
        with db.engine.connect() as c:
            assert c.scalar(sa.select(sa.func.count()).select_from(table)) == 1
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["in_flight"]
            == 0
        )
    )
    budget = cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")
    assert (budget["used"], budget["reserved"]) == (1, 0)


def test_expired_before_start_and_unknown_publication_cleanup(cluster, owner):
    cluster.bootstrap(owner[1])
    command = cluster.submit()
    cluster.launch("platform-worker")
    eventually(lambda: cluster.request("GET", command["status_url"])["result"], timeout=45)
    with cluster.database.engine.begin() as c:
        c.execute(
            tables.grants.update().values(expires_at=sa.func.now() - sa.text("interval '1 second'"))
        )
    cluster.kill(cluster.running["data"])
    cluster.launch("execution-worker")
    cancel = cluster.request(
        "POST", cluster.p + "/tasks/" + cluster.task + "/cancel", {"expected_control_version": 1}
    )
    assert cancel["status"] == "RESOLVED"

    def unknown():
        return (
            cluster.request("GET", cluster.p + "/tasks/" + cluster.task)["cleanup_status"]
            == "UNKNOWN"
        )

    eventually(unknown, timeout=20)
    budget = cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")
    assert budget["used"] == 0 and budget["in_flight"] == 1
    cluster.launch("data")
    execution = cluster.completed_execution(command)
    assert not execution["executed"]
    assert execution["runtime"]["publication"] == "ABORTED"
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + "/tasks/" + cluster.task)["cleanup_status"]
            == "CLEAR"
        ),
        timeout=20,
    )
    budget = cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (0, 0, 0)


def test_execution_lease_fences_old_worker(service_databases):
    from platform_execution.adapters.repository import Repository
    from platform_execution.adapters.tables import executions

    _, db = service_databases
    repo = Repository(db)
    repo.create("action", {"project_id": "project", "task_id": "task"})
    old = repo.claim("old")
    with db.engine.begin() as c:
        c.execute(
            executions.update().values(lease_until=sa.func.now() - sa.text("interval '1 second'"))
        )
    current = repo.claim("current")
    assert current["generation"] > old["generation"]
    assert repo.change(current, state="CANCELLED")
    assert not repo.change(old, state="RUNNING", executed=True)
    assert repo.get(action="action")["state"] == "CANCELLED"


def test_grant_natural_expiry_rejects_queued_without_charging(cluster, owner):
    cluster.bootstrap(owner[1])
    command = cluster.submit()
    cluster.launch("platform-worker")
    eventually(lambda: cluster.request("GET", command["status_url"])["result"], timeout=45)
    # Stop Platform delivery to exercise Execution's own last-moment permit check.
    cluster.kill(cluster.running["platform-worker"])
    with cluster.database.engine.begin() as c:
        c.execute(
            tables.grants.update().values(expires_at=sa.func.now() - sa.text("interval '1 second'"))
        )
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command)
    assert execution["state"] == "REJECTED" and not execution["executed"]
    cluster.launch("platform-worker")
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["in_flight"]
            == 0
        )
    )
    assert cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["used"] == 0


def test_container_input_failure_consumes_started_execution(cluster, owner):
    cluster.bootstrap(owner[1])
    # A valid transport contract with scientifically unaligned monthly boundaries.
    cluster.parameters["frequency"] = "month"
    command = cluster.submit(index=1)
    cluster.launch("platform-worker")
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command)
    assert execution["state"] == "FAILED" and execution["executed"]
    assert execution["runtime"]["publication"] == "ABORTED"
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["in_flight"]
            == 0
        )
    )
    assert cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["used"] == 1
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_cancel_before_acceptance_seals_without_a_false_execution_binding(service_databases):
    from platform_execution.adapters.repository import Repository

    repo = Repository(service_databases[1])
    observation = repo.observation(repo.cancel("never-accepted"))
    assert observation["state"] == "SEALED"
    assert observation["execution_id"] is None and not observation["executed"]
    late = repo.observation(
        repo.create("never-accepted", {"project_id": "project", "task_id": "task"})
    )
    assert late == observation
