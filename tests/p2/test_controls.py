from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.application.control import ControlService
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables as t

from .conftest import TASK


def test_task_http_contracts_and_command_receipts(database, owner):
    _, token = owner
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "create"}
    with TestClient(create_app(database)) as client:
        assert client.get("/api/v1/ready").status_code == 200
        assert client.get("/api/v1/projects").status_code == 401
        project_response = client.post("/api/v1/projects", json={"name": "P2"}, headers=headers)
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()["project_id"]
        prefix = f"/api/v1/projects/{project}"
        response = client.post(prefix + "/tasks", json=TASK, headers=headers)
        assert response.status_code == 201, response.text
        task_id = response.json()["task_id"]
        assert client.post(prefix + "/tasks", json=TASK, headers=headers).json() == response.json()
        response = client.post(
            prefix + f"/tasks/{task_id}/pause",
            json={"expected_control_version": 1},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["status"] == "RESOLVED" and receipt["result"] is None
        assert (
            client.get(receipt["status_url"], headers=headers).json()["command_id"]
            == receipt["command_id"]
        )
        assert (
            client.get(prefix + f"/tasks/{task_id}", headers=headers).json()["control"] == "PAUSED"
        )
        # P3 now implements this route; an empty calculation still cannot create state.
        assert client.post(prefix + "/executions", json={}, headers=headers).status_code == 400
        invalid = dict(TASK, execution_mode="AGENT")
        response = client.post(
            prefix + "/tasks", json=invalid, headers=headers | {"Idempotency-Key": "agent"}
        )
        assert response.status_code == 422 and response.json()["code"] == "UNSUPPORTED_TARGET"
        response = client.post(
            prefix + "/tasks", json=TASK | {"actor_id": "forged"}, headers=headers
        )
        assert response.status_code == 400 and "forged" not in response.text


def test_idempotency_conflict_scope_and_live_authorization(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    result = app.control(actor, project, task, "pause", "pause", 1)
    assert app.control(actor, project, task, "pause", "pause", 1) == result
    with pytest.raises(RuleViolation) as error:
        app.control(actor, project, task, "pause", "pause", 2)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    other = app.create_project(actor, "another-project", "other")
    with pytest.raises(RuleViolation) as error:
        app.task(actor, other["project_id"], task)
    assert error.value.status == 404
    principal = database.create_principal("new owner")
    database.set_member(project, principal, "OWNER", actor=actor)
    database.set_member(project, actor.principal_id, "VIEWER", actor=actor)
    with pytest.raises(RuleViolation) as error:
        app.control(actor, project, task, "pause", "pause", 1)
    assert error.value.status == 403
    database.revoke_token(actor.token_id)
    with pytest.raises(RuleViolation) as error:
        app.task(actor, project, task)
    assert error.value.status == 401


def test_concurrent_control_version_and_immutable_revisions(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    barrier = Barrier(2)

    def competing(operation):
        barrier.wait()
        try:
            return app.control(actor, project, task, operation, operation, 1)["status"]
        except RuleViolation as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(competing, ["pause", "cancel"]))
    assert sorted(results) == ["RESOLVED", "REVISION_CONFLICT"]
    other = app.create_task(actor, project, "revisable", TASK)["task_id"]
    revised = app.revise(
        actor,
        project,
        other,
        "r2",
        {
            "expected_control_version": 1,
            "expected_spec_revision": 1,
            "spec": {"goal": "New requirement", "requirements": []},
        },
    )
    assert revised["spec_revision"] == revised["control_version"] == 2
    with database.transaction() as uow:
        assert uow.tasks.spec(other, 1)["goal"] == TASK["spec"]["goal"]
        assert uow.tasks.spec(other, 2)["goal"] == "New requirement"


def test_cancel_does_not_mean_completed_and_blocks_resume(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    app.control(actor, project, task, "cancel", "cancel", 1)
    assert app.task(actor, project, task)["lifecycle"] == "CANCELLED"
    with pytest.raises(RuleViolation):
        app.control(actor, project, task, "resume", "resume", 2)
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.commands)) == 1


def test_bounded_pagination_and_invalid_cursor(database, owner):
    actor, token = owner
    app = ControlService(database)
    for index in range(3):
        app.create_project(actor, str(index), str(index))
    with TestClient(create_app(database)) as client:
        headers = {"Authorization": "Bearer " + token}
        page = client.get("/api/v1/projects?limit=2", headers=headers).json()
        assert len(page["items"]) == 2 and page["next_cursor"]
        second = client.get(
            "/api/v1/projects", params={"limit": 2, "cursor": page["next_cursor"]}, headers=headers
        ).json()
        assert len(second["items"]) == 1 and second["next_cursor"] is None
        assert client.get("/api/v1/projects?limit=101", headers=headers).status_code == 400
        assert client.get("/api/v1/projects?cursor=bad", headers=headers).status_code == 400


def test_global_project_creation_replay_checks_current_membership(database, owner):
    actor, token = owner
    app = ControlService(database)
    project = app.create_project(actor, "global-key", "original")
    new_owner = database.create_principal("new-owner")
    database.set_member(project["project_id"], new_owner, "OWNER", actor=actor)
    database.set_member(project["project_id"], actor.principal_id, "REMOVE", actor=actor)
    with TestClient(create_app(database)) as client:
        response = client.post(
            "/api/v1/projects",
            json={"name": "original"},
            headers={
                "Authorization": "Bearer " + token,
                "Idempotency-Key": "global-key",
            },
        )
        assert response.status_code == 404
        assert project["project_id"] not in response.text
