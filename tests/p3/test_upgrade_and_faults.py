import socket
from urllib.parse import urlsplit

import httpx
import pytest
import sqlalchemy as sa
from platform_execution.adapters import tables as execution_tables
from platform_infrastructure import tables

from tests.p2.legacy_upgrade import fingerprint, migrate_to, seed
from tests.p2.processes import eventually
from tests.p3.test_execution import proxy


def test_upgrade_replays_original_task_revision_and_grant_keys(database):
    from fastapi.testclient import TestClient
    from platform_api.app import create_app
    from platform_core.application.control import ControlService

    migrate_to(database, "base", downgrade=True)
    migrate_to(database, "0002_observation_snapshot")
    fixture = seed(database)
    actor = database.authenticate(fixture["token"])
    app = ControlService(database)
    create = {
        "execution_mode": "DIRECT",
        "spec": {"goal": "Legacy HTTP request", "requirements": []},
        "budget": {"execution_count": 3, "max_in_flight": 2},
    }
    original = app.create_task(actor, fixture["project"], "old-create", create)
    revision = {
        "expected_control_version": 1,
        "expected_spec_revision": 1,
        "spec": {"goal": "Revised before upgrade", "requirements": []},
    }
    revised = app.revise(actor, fixture["project"], original["task_id"], "old-revise", revision)
    with database.engine.connect() as c:
        grant = (
            c.execute(
                sa.select(
                    tables.grants.c.task_id,
                    tables.grants.c.targets,
                    tables.grants.c.resources,
                    tables.grants.c.expires_at,
                )
            )
            .mappings()
            .one()
        )
        grant = dict(grant) | {"expires_at": grant["expires_at"].isoformat()}
    before = fingerprint(database)
    database.migrate()
    assert fingerprint(database) == before
    prefix = "/api/v1/projects/" + fixture["project"]
    headers = {"Authorization": "Bearer " + fixture["token"]}
    with TestClient(create_app(database)) as client:
        for suffix, key, request, identity, expected in [
            ("/tasks", "old-create", create, "task_id", original["task_id"]),
            (
                "/tasks/" + original["task_id"] + "/spec-revisions",
                "old-revise",
                revision,
                "spec_revision",
                revised["spec_revision"],
            ),
            ("/grants", "legacy-grant", grant, "task_id", fixture["task"]),
        ]:
            response = client.post(
                prefix + suffix, json=request, headers=headers | {"Idempotency-Key": key}
            )
            assert response.status_code == 201, response.text
            assert response.json()[identity] == expected
        conflict = client.post(
            prefix + "/grants",
            json=grant | {"derived_outputs": True},
            headers=headers | {"Idempotency-Key": "legacy-grant"},
        )
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert fingerprint(database) == before


def test_populated_p2_0002_upgrade_preserves_snapshot_and_ledger(database):
    migrate_to(database, "base", downgrade=True)
    migrate_to(database, "0002_observation_snapshot")
    fixture = seed(database)
    snapshot = {key: value for key, value in fixture["observation"].items() if key != "event_id"}
    with database.engine.begin() as c:
        c.execute(
            tables.actions.update()
            .where(tables.actions.c.action_id == snapshot["action_id"])
            .values(last_external_observation=snapshot)
        )
    before = fingerprint(database)
    database.migrate()
    database.migrate()
    database.check_schema()
    assert before == fingerprint(database)
    with database.engine.connect() as c:
        assert (
            c.scalar(
                sa.select(tables.actions.c.last_external_observation).where(
                    tables.actions.c.action_id == snapshot["action_id"]
                )
            )
            == snapshot
        )
        row = c.execute(sa.select(tables.grants)).mappings().one()
        assert not row["derived_outputs"] and row["acceptance_profiles"] == []


def test_public_execution_response_loss_restores_original_command(cluster, owner):
    cluster.bootstrap(owner[1])
    url, dropped = proxy(cluster, "platform", "/executions")
    body = {
        "task_id": cluster.task,
        "expected_spec_revision": 1,
        "target": cluster.targets[0],
        "inputs": {"observations": cluster.selection},
        "parameters": cluster.parameters,
    }
    with (
        httpx.Client(
            base_url=url,
            trust_env=False,
            headers={"Authorization": "Bearer " + owner[1], "Idempotency-Key": "lost-api"},
        ) as client,
        pytest.raises(httpx.TransportError),
    ):
        client.post(cluster.p + "/executions", json=body)
    assert dropped.exists()
    cluster.restart("platform")
    receipt = cluster.submit(key="lost-api")
    with cluster.database.engine.connect() as c:
        rows = c.execute(sa.select(tables.actions)).mappings().all()
        assert len(rows) == 1 and rows[0]["command_id"] == receipt["command_id"]
    assert cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["reserved"] == 1


def test_http_upload_disconnect_never_links_partial_bytes(cluster, owner):
    import hashlib

    cluster.bootstrap(owner[1])
    raw = b"a,b\nx,y\n"
    created = cluster.request(
        "POST",
        cluster.p + "/uploads",
        {
            "filename": "partial.csv",
            "format": "csv",
            "size": len(raw),
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        },
    )
    path = cluster.p + "/uploads/" + created["upload_id"]
    endpoint = urlsplit(cluster.env["V7_PLATFORM_URL"])
    with socket.create_connection((endpoint.hostname, endpoint.port)) as connection:
        headers = (
            f"PUT {path}/content HTTP/1.1\r\nHost: localhost\r\n"
            f"Authorization: Bearer {owner[1]}\r\nContent-Length: {len(raw)}\r\n"
            "Content-Type: application/octet-stream\r\nConnection: close\r\n\r\n"
        )
        connection.sendall(headers.encode() + raw[:3])
    eventually(lambda: cluster.request("GET", path)["status"] == "AWAITING_CONTENT")
    cluster.restart("data")
    assert cluster.client.put(path + "/content", content=raw).status_code == 200
    assert cluster.request("GET", path)["status"] == "UPLOADED"
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 1


def test_cancel_after_publish_response_loss_retains_result(cluster, owner):
    cluster.bootstrap(owner[1])
    command = cluster.submit()
    cluster.launch("platform-worker")
    url, dropped = proxy(cluster, "data", "/commit")
    cluster.env["V7_DATA_URL"] = url
    cluster.launch("execution-worker")
    eventually(dropped.exists, timeout=40)
    cluster.kill(cluster.running["execution-worker"])
    cluster.request(
        "POST", cluster.p + "/tasks/" + cluster.task + "/cancel", {"expected_control_version": 1}
    )

    def cancellation_reached_execution():
        with cluster.service_databases[1].engine.connect() as connection:
            return connection.execute(
                sa.select(execution_tables.executions.c.cancel_requested)
            ).scalar_one()

    eventually(cancellation_reached_execution, timeout=20)
    cluster.launch("execution-worker")
    execution = cluster.completed_execution(command)
    assert execution["state"] == "SUCCEEDED" and execution["executed"]
    assert execution["runtime"]["publication"] == "COMMITTED"
    assert execution["runtime"]["cancellation"] == {"state": "TOO_LATE_COMMITTED"}
    assert len(cluster.request("GET", cluster.p + "/assets")["items"]) == 2
    eventually(
        lambda: (
            cluster.request("GET", cluster.p + "/tasks/" + cluster.task)["cleanup_status"]
            == "CLEAR"
        ),
        timeout=40,
    )
    assert cluster.request("GET", cluster.p + "/tasks/" + cluster.task + "/budget")["used"] == 1
