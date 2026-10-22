from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.domain.control import RuleViolation

from .conftest import TARGET
from .test_actions import action_request


def test_scope_is_exact_and_never_project_wide(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    asset = {
        "project_id": project,
        "asset_id": "input",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "a" * 64,
    }
    request = action_request(task) | {"resources": [asset]}
    with pytest.raises(RuleViolation) as error:
        app.submit_action(actor, project, "no-resource-scope", request)
    assert error.value.status == 403
    body = {
        "task_id": task,
        "targets": [TARGET],
        "resources": [asset],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    grant = app.grant(actor, project, "exact", body)
    app.submit_action(actor, project, "exact", request)
    with pytest.raises(RuleViolation):
        app.submit_action(
            actor, project, "other-version", request | {"resources": [asset | {"version_id": "2"}]}
        )
    with pytest.raises(RuleViolation):
        app.grant(
            actor,
            project,
            "cross-project",
            body | {"resources": [asset | {"project_id": "another-project"}]},
        )
    revoked = app.revoke_grant(actor, project, grant["grant_id"], "revoke")
    assert revoked["revoked_by"] == actor.principal_id and revoked["revoked_at"]
    assert app.revoke_grant(actor, project, grant["grant_id"], "revoke") == revoked
    with pytest.raises(RuleViolation):
        app.submit_action(actor, project, "revoked", request)


def test_all_grant_and_revision_routes_recheck_role_and_return_strict_views(database, setup_task):
    app, actor, token, project, task, _ = setup_task
    prefix = f"/api/v1/projects/{project}"
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "http-grant"}
    body = {
        "task_id": task,
        "targets": [TARGET],
        "resources": [],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    with TestClient(create_app(database)) as client:
        created = client.post(prefix + "/grants", json=body, headers=headers)
        assert created.status_code == 201
        grant = created.json()
        assert client.get(prefix, headers=headers).status_code == 200
        assert len(client.get(prefix + "/tasks", headers=headers).json()["items"]) == 1
        assert len(client.get(prefix + "/grants", headers=headers).json()["items"]) == 2
        response = client.post(prefix + f"/grants/{grant['grant_id']}/revoke", headers=headers)
        assert response.status_code == 200 and response.json()["revoked_by"] == actor.principal_id
        task_path = prefix + "/tasks/" + task
        response = client.post(
            task_path + "/spec-revisions",
            headers=headers,
            json={
                "expected_spec_revision": 1,
                "expected_control_version": 1,
                "spec": {"goal": "New immutable requirements"},
            },
        )
        assert response.status_code == 201 and response.json()["spec_revision"] == 2
        assert (
            client.post(
                task_path + "/pause", headers=headers, json={"expected_control_version": 2}
            ).status_code
            == 200
        )
        assert (
            client.post(
                task_path + "/resume", headers=headers, json={"expected_control_version": 3}
            ).status_code
            == 200
        )
        assert client.get(task_path + "/budget", headers=headers).status_code == 200
        second = database.create_principal("second-owner")
        database.set_member(project, second, "OWNER", actor=actor)
        database.set_member(project, actor.principal_id, "EDITOR", actor=actor)
        # Replay still requires the current OWNER permission.
        assert client.post(prefix + "/grants", json=body, headers=headers).status_code == 403
        assert (
            client.post(
                task_path + "/cancel", headers=headers, json={"expected_control_version": 4}
            ).status_code
            == 200
        )
    assert app.task(actor, project, task)["lifecycle"] == "CANCELLED"
