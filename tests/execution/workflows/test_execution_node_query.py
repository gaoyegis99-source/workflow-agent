import hashlib
import json
import threading
import time

from fastapi.testclient import TestClient
from platform_execution.adapters.repository import Repository
from platform_execution.adapters.tables import executions
from platform_execution.api import create_app


def workflow_root(database):
    repository = Repository(database)
    bridge_token = "bridge-token-" + "x" * 32
    representation_digest = "sha256:" + "1" * 64
    payload = {
        "project_id": "project",
        "task_id": "task",
        "expected_spec_revision": 1,
        "kind": "WORKFLOW",
        "compiled": {
            "invocations": {
                "leaf": {
                    "target": {
                        "kind": "OPERATOR",
                        "operator_id": "timeseries-completeness",
                        "version_id": "1.0.0",
                        "execution_binding": {
                            "kind": "LOCAL",
                            "implementation_id": "p3-completeness-v1",
                        },
                    }
                }
            }
        },
        "inputs": {
            "observations": {
                "class": "File",
                "binding": {
                    "selection": {"asset": {"asset_id": "asset", "version_id": "1"}},
                    "representation": {"digest": representation_digest},
                    "semantics": {},
                },
            }
        },
    }
    root = repository.create("root_action", payload)
    runtime = root["runtime"] | {
        "phase": "ENGINE_RUNNING",
        "engine_epoch": 1,
        "bridge_token_digest": hashlib.sha256(bridge_token.encode()).hexdigest(),
    }
    with database.engine.begin() as connection:
        connection.execute(
            executions.update()
            .where(executions.c.action_id == root["action_id"])
            .values(state="RUNNING", executed=True, revision=2, runtime=runtime)
        )
    root = repository.get(action=root["action_id"])
    call_path, scatter_indices = ["leaf"], []
    node_id = (
        "node_"
        + hashlib.sha256(
            json.dumps(
                [root["execution_id"], call_path, scatter_indices], separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    request = {
        "call_path": call_path,
        "scatter_indices": scatter_indices,
        "inputs": {"observations": {"class": "File", "digest": representation_digest}},
        "engine_job_id": "toil_job",
        "epoch": 1,
        "trace": {
            "bridge_instance_id": "a" * 32,
            "started_monotonic_ns": 1,
        },
    }
    return repository, root, node_id, request, bridge_token


def test_node_revision_query_waits_without_hiding_atomic_terminal_files(service_databases):
    database = service_databases[1]
    repository, root, node_id, body, bridge_token = workflow_root(database)
    headers = {"Authorization": "Bearer " + bridge_token}
    base = f"/internal/workflows/{root['execution_id']}/nodes/{node_id}"
    with TestClient(create_app(database, "platform-" + "x" * 32)) as client:
        accepted = client.post(base, json=body, headers=headers)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["revision"] == 1

        current = client.get(
            base,
            params={"after_revision": 0, "wait_seconds": 1},
            headers=headers,
        )
        assert current.status_code == 200
        assert current.json()["changed"] is True

        def finish():
            time.sleep(0.15)
            row = repository.get(action=node_id)
            with database.engine.begin() as connection:
                connection.execute(
                    executions.update()
                    .where(executions.c.action_id == node_id)
                    .values(
                        state="SUCCEEDED",
                        executed=True,
                        revision=2,
                        runtime=row["runtime"]
                        | {
                            "phase": "FINAL",
                            "files": [
                                {
                                    "representation_id": "result",
                                    "digest": "sha256:" + "2" * 64,
                                }
                            ],
                        },
                    )
                )

        update = threading.Thread(target=finish)
        update.start()
        started = time.monotonic()
        changed = client.get(
            base,
            params={
                "after_revision": 1,
                "wait_seconds": 2,
                "bridge_instance_id": "a" * 32,
            },
            headers=headers,
        )
        elapsed = time.monotonic() - started
        update.join()
        assert changed.status_code == 200, changed.text
        assert 0.1 <= elapsed < 1.5
        assert changed.json() == {
            "state": "SUCCEEDED",
            "execution_id": repository.identity(node_id),
            "attempt_id": "a" + repository.identity(node_id)[1:],
            "revision": 2,
            "files": [{"representation_id": "result", "digest": "sha256:" + "2" * 64}],
            "changed": True,
            "retry_after_ms": 0,
        }


def test_node_revision_query_times_out_without_reposting(service_databases):
    database = service_databases[1]
    _, root, node_id, body, bridge_token = workflow_root(database)
    headers = {"Authorization": "Bearer " + bridge_token}
    base = f"/internal/workflows/{root['execution_id']}/nodes/{node_id}"
    with TestClient(create_app(database, "platform-" + "x" * 32)) as client:
        accepted = client.post(base, json=body, headers=headers).json()
        started = time.monotonic()
        response = client.get(
            base,
            params={"after_revision": accepted["revision"], "wait_seconds": 0.1},
            headers=headers,
        )
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert response.json()["changed"] is False
        assert elapsed >= 0.08


def test_optional_engine_binding_is_diagnostic_and_replay_safe(service_databases):
    database = service_databases[1]
    repository, root, node_id, body, bridge_token = workflow_root(database)
    headers = {"Authorization": "Bearer " + bridge_token}
    base = f"/internal/workflows/{root['execution_id']}/nodes/{node_id}"
    body.pop("engine_job_id")
    body["engine_binding"] = {"kind": "toil", "id": None, "epoch": 1}
    with TestClient(create_app(database, "platform-" + "x" * 32)) as client:
        accepted = client.post(base, json=body, headers=headers)
        assert accepted.status_code == 202, accepted.text

        changed = body | {"engine_binding": {"kind": "toil", "id": "new-job", "epoch": 1}}
        replay = client.post(base, json=changed, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["execution_id"] == accepted.json()["execution_id"]

    children = repository.children(root["execution_id"])
    assert len(children) == 1
    assert children[0]["payload"]["node_request"]["engine_job_id"] is None
    bindings = [
        event["attributes"]
        for event in repository.traces(root["execution_id"])
        if event["event_type"] == "ENGINE_BINDING_OBSERVED"
    ]
    assert bindings == [
        {"kind": "toil", "binding_id": None},
        {"kind": "toil", "binding_id": "new-job"},
    ]


def test_legacy_engine_job_id_is_normalized(service_databases):
    database = service_databases[1]
    repository, root, node_id, body, bridge_token = workflow_root(database)
    headers = {"Authorization": "Bearer " + bridge_token}
    base = f"/internal/workflows/{root['execution_id']}/nodes/{node_id}"
    with TestClient(create_app(database, "platform-" + "x" * 32)) as client:
        response = client.post(base, json=body, headers=headers)
        assert response.status_code == 202, response.text
    request = repository.get(action=node_id)["payload"]["node_request"]
    assert request["engine_job_id"] == "toil_job"
    assert request["engine_binding"] == {"kind": "toil", "id": "toil_job", "epoch": 1}


def test_invalid_engine_binding_is_rejected_without_creating_node(service_databases):
    database = service_databases[1]
    repository, root, node_id, body, bridge_token = workflow_root(database)
    headers = {"Authorization": "Bearer " + bridge_token}
    base = f"/internal/workflows/{root['execution_id']}/nodes/{node_id}"
    body.pop("engine_job_id")
    body["engine_binding"] = {"kind": "toil", "id": "job", "epoch": 2}
    with TestClient(create_app(database, "platform-" + "x" * 32)) as client:
        response = client.post(base, json=body, headers=headers)
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_BINDING"
    assert repository.children(root["execution_id"]) == []
