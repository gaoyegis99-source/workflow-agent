import json
import signal
import sys
import threading
import time

import pytest
import sqlalchemy as sa
from platform_core.application.dispatch import Dispatcher
from platform_infrastructure import tables as t
from platform_infrastructure.worker import Worker

from .processes import HERE, eventually
from .test_actions import action_request, available, claim_action


def row(database, action_id):
    with database.engine.connect() as connection:
        return dict(
            connection.execute(sa.select(t.actions).where(t.actions.c.action_id == action_id))
            .mappings()
            .one()
        )


def expire(database, action_id):
    with database.engine.begin() as connection:
        connection.execute(
            t.outbox.update()
            .where(t.outbox.c.payload["action_id"].astext == action_id)
            .values(lease_until=sa.func.now(), available_at=sa.func.now())
        )


@pytest.mark.parametrize("gate", ["before_commit", "after_commit"])
def test_killed_process_atomic_commit_and_lost_reply(
    database, setup_task, processes, tmp_path, gate
):
    app, actor, token, project, task, _ = setup_task
    request = action_request(task)
    payload = tmp_path / "request.json"
    marker = tmp_path / "committing"
    payload.write_text(json.dumps({"project_id": project, "key": "crash", "request": request}))
    child = processes.start(
        [
            sys.executable,
            HERE / "driver.py",
            "submit",
            "--request-file",
            payload,
            "--marker",
            marker,
            "--gate",
            gate,
        ],
        {"P2_ACTOR_TOKEN": token},
    )
    eventually(marker.exists)
    processes.kill(child)
    with database.engine.connect() as connection:
        count = 0 if gate == "before_commit" else 1
        for table in [t.actions, t.reservations, t.commands]:
            assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == count
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.requests)) == 3 + count
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(t.outbox).where(t.outbox.c.kind == "ACTION")
            )
            == count
        )
    assert app.budget(actor, project, task)["reserved"] == count
    result = app.submit_action(actor, project, "crash", request)
    assert app.submit_action(actor, project, "crash", request) == result
    assert app.budget(actor, project, task)["reserved"] == 1


def test_lost_external_reply_survives_api_worker_and_peer_restarts(database, setup_task, processes):
    app, actor, token, project, task, _ = setup_task
    peer_process, peer = processes.peer()
    api_process, client = processes.api()
    command = app.submit_action(actor, project, "lost", action_request(task))
    action_id = command["action_id"]
    peer.request("POST", "/test/configure", {"drop_submit_response": 1})
    worker = processes.worker()
    eventually(lambda: row(database, action_id)["state"] == "RECONCILING")
    assert app.budget(actor, project, task)["reserved"] == 1
    assert peer.request("GET", "/test/stats")["executions"] == 1
    processes.kill(worker)
    processes.kill(api_process)
    client.close()
    processes.kill(peer_process)
    _, peer = processes.peer()  # Same external durable store, new service process.
    _, client = processes.api()
    processes.worker()
    eventually(lambda: row(database, action_id)["execution_id"] is not None)
    assert app.submit_action(actor, project, "lost", action_request(task)) == command
    response = client.get(
        f"/api/v1/projects/{project}/commands/{command['command_id']}",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200 and response.json()["status"] == "RESOLVED"
    peer.request("POST", "/test/complete/" + action_id)
    eventually(lambda: row(database, action_id)["stopped"])
    stats = peer.request("GET", "/test/stats")
    assert stats["executions"] == 1
    assert sum(call["method"] == "PUT" for call in stats["calls"]) == 1
    assert app.budget(actor, project, task)["used"] == 1
    assert app.budget(actor, project, task)["in_flight"] == 0
    assert app.task(actor, project, task)["lifecycle"] == "OPEN"
    client.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_frozen_old_worker_late_submit_and_cancel_seal(database, setup_task, processes, cancel):
    app, actor, _, project, task, _ = setup_task
    _, peer = processes.peer()
    command = app.submit_action(actor, project, "race", action_request(task))
    action_id = command["action_id"]
    peer.request("POST", "/test/configure", {"submit_delay": 3})
    old = processes.worker()
    eventually(
        lambda: any(c["method"] == "PUT" for c in peer.request("GET", "/test/stats")["calls"])
    )
    old.send_signal(signal.SIGSTOP)  # Stops both computation thread and lease heartbeat.
    # Acquiring the task lock now proves the HTTP call holds no platform transaction.
    with database.transaction() as uow:
        uow.tasks.get(project, task, lock=True)
    if cancel:
        app.control(actor, project, task, "cancel", "cancel", 1)
        assert app.task(actor, project, task)["cleanup_status"] == "PENDING"
    expire(database, action_id)  # Advance lease expiry without a 30-second test sleep.
    processes.worker()
    eventually(lambda: row(database, action_id)["state"] == ("SEALED" if cancel else "RUNNING"))
    authoritative = row(database, action_id)
    old.send_signal(signal.SIGCONT)
    # Wait beyond the external service's original delayed request, then verify fencing.
    time.sleep(3.2)
    assert row(database, action_id)["execution_id"] == authoritative["execution_id"]
    assert row(database, action_id)["state"] == authoritative["state"]
    stats = peer.request("GET", "/test/stats")
    assert stats["executions"] == (0 if cancel else 1)
    budget = app.budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (
        (0, 0, 0) if cancel else (1, 0, 1)
    )
    if cancel:
        assert app.task(actor, project, task)["cleanup_status"] == "CLEAR"


def test_cancellation_unknown_retains_slot_until_terminal_evidence(database, setup_task, processes):
    app, actor, _, project, task, _ = setup_task
    _, peer = processes.peer()
    command = app.submit_action(actor, project, "cancel", action_request(task))
    action_id = command["action_id"]
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    prepared = dispatcher.prepare(claim)
    dispatcher.observe(claim, peer.submit(action_id, prepared["request"]))
    app.control(actor, project, task, "cancel", "cancel", 1)
    peer.request("POST", "/test/configure", {"drop_cancel_response": 1})
    worker = Worker(database, peer)
    available(database)
    eventually(lambda: worker.process_one() and row(database, action_id)["state"] == "RECONCILING")
    assert app.task(actor, project, task)["cleanup_status"] == "UNKNOWN"
    assert app.budget(actor, project, task)["in_flight"] == 1
    peer.request("POST", "/test/complete/" + action_id)  # Result wins the external cancel race.
    available(database)
    eventually(lambda: worker.process_one() and row(database, action_id)["stopped"])
    assert app.budget(actor, project, task)["used"] == 1
    assert app.budget(actor, project, task)["in_flight"] == 0
    assert app.task(actor, project, task)["lifecycle"] == "CANCELLED"
    assert app.task(actor, project, task)["cleanup_status"] == "CLEAR"


def test_heartbeat_extends_lease_while_external_call_blocks(database, setup_task, processes):
    app, actor, _, project, task, _ = setup_task
    _, peer = processes.peer()
    command = app.submit_action(actor, project, "heartbeat", action_request(task))
    # Drain task-event delivery, leaving the action unclaimed.
    with database.engine.begin() as connection:
        connection.execute(
            t.outbox.update().where(t.outbox.c.kind == "TASK_EVENT").values(done=True)
        )
    peer.request("POST", "/test/configure", {"submit_delay": 2})
    worker = Worker(database, peer, lease_seconds=0.8, heartbeat_seconds=0.2)
    thread = threading.Thread(target=worker.process_one)
    thread.start()
    try:
        eventually(lambda: peer.request("GET", "/test/stats")["calls"])
        time.sleep(1)
        assert Dispatcher(database).claim("competitor") is None
    finally:
        thread.join(timeout=7)
    assert not thread.is_alive()
    assert row(database, command["action_id"])["state"] == "RUNNING"
    assert app.budget(actor, project, task)["used"] == 1


def test_database_outage_recovers_without_new_execution(database, setup_task, processes, postgres):
    app, actor, token, project, task, _ = setup_task
    _, peer = processes.peer()
    _, client = processes.api()
    command = app.submit_action(actor, project, "outage", action_request(task))
    action_id = command["action_id"]
    worker = processes.worker()
    eventually(lambda: row(database, action_id)["state"] == "RUNNING")
    postgres.docker("stop", "--time", "1", postgres.name)
    try:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 503
        response = client.get("/api/v1/projects", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 503
        assert postgres.password not in response.text and "SELECT" not in response.text
        peer.request("POST", "/test/complete/" + action_id)
    finally:
        postgres.docker("start", postgres.name)
    eventually(lambda: client.get("/api/v1/ready").status_code == 200)
    eventually(lambda: row(database, action_id)["stopped"])
    assert worker.poll() is None
    assert peer.request("GET", "/test/stats")["executions"] == 1
    assert app.budget(actor, project, task)["used"] == 1
    assert app.budget(actor, project, task)["in_flight"] == 0
    client.close()
