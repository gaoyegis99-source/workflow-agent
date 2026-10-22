import json
import sys
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa
from platform_contracts.v1 import CommandReceipt
from platform_core.application.dispatch import Dispatcher
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables as t

from .processes import HERE, eventually
from .test_actions import action_request, available
from .test_recovery import row


def drain_events(database):
    dispatcher = Dispatcher(database)
    while claim := dispatcher.claim("event-drain"):
        assert claim["kind"] == "TASK_EVENT"
        dispatcher.prepare(claim)


def step(database, processes):
    available(database)
    worker = processes.worker(once=True)
    assert worker.wait(timeout=10) == 0


def submit_once(database, setup_task, processes):
    app, actor, _, project, task, _ = setup_task
    _, peer = processes.peer()
    drain_events(database)
    command = app.submit_action(actor, project, "http-fault", action_request(task))
    step(database, processes)
    assert row(database, command["action_id"])["state"] == "RUNNING"
    return command, peer


@pytest.mark.parametrize("fault", ["drop_query_response", "query_absent"])
def test_known_execution_query_failure_recovers_same_version(
    database, setup_task, processes, fault
):
    app, actor, token, project, task, _ = setup_task
    command, peer = submit_once(database, setup_task, processes)
    before = row(database, command["action_id"])
    budget = app.budget(actor, project, task)
    peer.request("POST", "/test/configure", {fault: 1})
    step(database, processes)
    unknown = row(database, command["action_id"])
    assert unknown["state"] == "RECONCILING"
    assert unknown["execution_id"] == before["execution_id"]
    assert unknown["last_external_observation"] == before["last_external_observation"]
    assert app.budget(actor, project, task) == budget
    _, client = processes.api()
    response = client.get(
        f"/api/v1/projects/{project}/commands/{command['command_id']}",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200
    assert (
        CommandReceipt.model_validate(response.json()).result.execution_id == before["execution_id"]
    )
    client.close()
    step(database, processes)
    recovered = row(database, command["action_id"])
    assert recovered["state"] == "RUNNING" and recovered["failure_count"] == 0
    assert app.budget(actor, project, task) == budget
    stats = peer.request("GET", "/test/stats")
    assert stats["executions"] == 1
    assert sum(call["method"] == "PUT" for call in stats["calls"]) == 1
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 1


def expires_soon(database, grant):
    with database.engine.begin() as connection:
        expiry = connection.scalar(sa.select(sa.func.clock_timestamp())) + timedelta(seconds=0.25)
        connection.execute(
            t.grants.update()
            .where(t.grants.c.grant_id == grant["grant_id"])
            .values(expires_at=expiry)
        )
    return expiry


def wait_expired(database, expiry):
    def expired():
        with database.engine.connect() as connection:
            return connection.scalar(sa.select(sa.func.clock_timestamp())) >= expiry

    eventually(expired)


def test_grant_naturally_expires_while_queued_without_sending(database, setup_task, processes):
    app, actor, _, project, task, grant = setup_task
    _, peer = processes.peer()
    drain_events(database)
    command = app.submit_action(actor, project, "queued-expiry", action_request(task))
    expiry = expires_soon(database, grant)
    wait_expired(database, expiry)
    step(database, processes)
    assert row(database, command["action_id"])["state"] == "REJECTED"
    assert app.command(actor, project, command["command_id"])["error_code"] == "SCOPE_DENIED"
    assert app.budget(actor, project, task)["reserved"] == 0
    assert peer.request("GET", "/test/stats")["calls"] == []


def test_expiry_after_send_window_retains_unknown_until_confirmed_stop(
    database, setup_task, processes
):
    app, actor, _, project, task, grant = setup_task
    _, peer = processes.peer()
    drain_events(database)
    command = app.submit_action(actor, project, "sending-expiry", action_request(task))
    peer.request("POST", "/test/configure", {"submit_delay": 1, "drop_submit_response": 1})
    worker = processes.worker(once=True)
    eventually(lambda: peer.request("GET", "/test/stats")["calls"])
    expiry = expires_soon(database, grant)
    wait_expired(database, expiry)
    assert worker.wait(timeout=10) == 0
    assert row(database, command["action_id"])["state"] == "RECONCILING"
    assert app.budget(actor, project, task)["reserved"] == 1
    peer.request("POST", "/test/configure", {"drop_cancel_response": 1})
    step(database, processes)
    assert app.budget(actor, project, task)["in_flight"] == 1
    assert app.budget(actor, project, task)["reserved"] == 1
    step(database, processes)
    assert row(database, command["action_id"])["state"] == "CANCELLED"
    budget = app.budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 0, 0)
    assert peer.request("GET", "/test/stats")["executions"] == 1


@pytest.mark.parametrize("old_revision", [False, True])
def test_resume_without_grant_does_not_authorize_queued_or_old_actions(
    database, setup_task, processes, old_revision
):
    app, actor, token, project, task, grant = setup_task
    _, peer = processes.peer()
    command = app.submit_action(actor, project, "queued-resume", action_request(task))
    app.control(actor, project, task, "pause", "pause", 1)
    app.revoke_grant(actor, project, grant["grant_id"], "revoke")
    version = 2
    if old_revision:
        app.revise(
            actor,
            project,
            task,
            "revise",
            {
                "expected_control_version": 2,
                "expected_spec_revision": 1,
                "spec": {"goal": "new", "requirements": []},
            },
        )
        version = 3
    _, client = processes.api()
    response = client.post(
        f"/api/v1/projects/{project}/tasks/{task}/resume",
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": "resume"},
        json={"expected_control_version": version},
    )
    assert response.status_code == 200
    assert app.task(actor, project, task)["control"] == "ACTIVE"
    with pytest.raises(RuleViolation) as error:
        app.submit_action(actor, project, "forbidden", action_request(task))
    assert error.value.code == ("REVISION_CONFLICT" if old_revision else "SCOPE_DENIED")
    processes.worker()
    eventually(lambda: row(database, command["action_id"])["stopped"])
    result = app.command(actor, project, command["command_id"])
    assert result["error_code"] == ("REVISION_CONFLICT" if old_revision else "SCOPE_DENIED")
    assert peer.request("GET", "/test/stats")["calls"] == []
    assert app.budget(actor, project, task)["in_flight"] == 0
    client.close()


def test_durable_external_rejection_stops_retries_and_releases_once(
    database, setup_task, processes
):
    app, actor, _, project, task, _ = setup_task
    peer_process, peer = processes.peer()
    drain_events(database)
    command = app.submit_action(actor, project, "reject", action_request(task))
    peer.request("POST", "/test/configure", {"reject_submit": 1})
    step(database, processes)
    assert app.command(actor, project, command["command_id"])["status"] == "REJECTED"
    assert row(database, command["action_id"])["state"] == "REJECTED"
    processes.kill(peer_process)
    _, peer = processes.peer()
    for _ in range(2):
        step(database, processes)
    stats = peer.request("GET", "/test/stats")
    assert stats["executions"] == 0 and len(stats["calls"]) == 1
    assert stats["actions"][0]["state"] == "REJECTED"
    budget = app.budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (0, 0, 0)
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 1


@pytest.mark.parametrize(
    "fault", [{"http_error": 400}, {"http_error": 503}, {"invalid_execution_identity": 1}]
)
def test_transport_error_or_invalid_identity_holds_budget_and_recovers(
    database, setup_task, processes, fault
):
    app, actor, token, project, task, _ = setup_task
    _, peer = processes.peer()
    drain_events(database)
    command = app.submit_action(actor, project, "invalid", action_request(task))
    peer.request("POST", "/test/configure", fault)
    step(database, processes)
    action = row(database, command["action_id"])
    assert action["state"] == "RECONCILING" and action["execution_id"] is None
    assert action["last_external_observation"] is None
    assert app.budget(actor, project, task)["reserved"] == 1
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(t.inbox)
                .where(t.inbox.c.source == "EXECUTION")
            )
            == 0
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 0
    _, client = processes.api()
    response = client.get(
        f"/api/v1/projects/{project}/commands/{command['command_id']}",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200
    assert CommandReceipt.model_validate(response.json()).status == "RECONCILING"
    client.close()
    step(database, processes)
    assert row(database, command["action_id"])["state"] == "RUNNING"
    assert peer.request("GET", "/test/stats")["executions"] == 1
    assert app.budget(actor, project, task)["used"] == 1


def test_api_commits_then_proxy_drops_response_restart_replays_one_command(
    database, setup_task, processes, tmp_path
):
    app, actor, token, project, task, _ = setup_task
    api_process, client = processes.api()
    marker, receipt_file = tmp_path / "proxy.json", tmp_path / "receipt.json"
    processes.start(
        [
            sys.executable,
            HERE / "drop_proxy.py",
            "--upstream",
            str(client.base_url),
            "--ready-file",
            marker,
            "--receipt-file",
            receipt_file,
        ]
    )
    eventually(marker.exists)
    port = json.loads(marker.read_text())["port"]
    path = f"/api/v1/projects/{project}/tasks/{task}/pause"
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "lost-http"}
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as proxy:
        with pytest.raises(httpx.RemoteProtocolError):
            proxy.post(path, headers=headers, json={"expected_control_version": 1})
    saved = json.loads(receipt_file.read_text())
    assert saved["status"] == 200
    assert app.task(actor, project, task)["control_version"] == 2
    processes.kill(api_process)
    client.close()
    _, client = processes.api()
    replay = client.post(path, headers=headers, json={"expected_control_version": 1})
    assert replay.status_code == 200
    assert replay.json()["command_id"] == saved["receipt"]["command_id"]
    assert app.task(actor, project, task)["control_version"] == 2
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.commands)) == 1
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(t.requests)
                .where(t.requests.c.operation == "pause")
            )
            == 1
        )
    client.close()
