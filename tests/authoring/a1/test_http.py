import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_api.composition import PlatformComposition
from platform_core.application.control import ControlService
from platform_infrastructure import tables

from scripts.lib.runtime_layout import WORK_ROOT


def test_authoring_http_and_openapi(database, owner):
    actor, token = owner
    project = ControlService(database).create_project(actor, "project", "Authoring")
    prefix = f"/api/v1/projects/{project['project_id']}"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(database)) as client:
        assert client.get("/api/v1/projects", headers=headers).json()["items"][0]["role"] == "OWNER"
        descriptor = client.get(prefix + "/authoring/operator-descriptors", headers=headers)
        assert descriptor.status_code == 200
        assert len(descriptor.json()["operators"]) == 11

        created = client.post(
            prefix + "/workflow-definitions",
            headers=headers | {"Idempotency-Key": "create-draft"},
            json={
                "expected_revision": 0,
                "stable_key": "http-draft",
                "metadata": {"title": "HTTP draft"},
                "layout": {},
            },
        )
        assert created.status_code == 201, created.text
        value = created.json()
        revised = client.post(
            prefix + f"/workflow-definitions/{value['definition_id']}/revisions",
            headers=headers | {"Idempotency-Key": "revise-draft"},
            json={
                "expected_revision": 1,
                "source_digest": value["head"]["source_digest"],
                "metadata": {"title": "HTTP draft 2"},
                "layout": {"viewport": {"zoom": 1}},
            },
        )
        assert revised.status_code == 201, revised.text
        assert revised.json()["revision"] == 2
        assert client.get(prefix + "/workflow-definitions", headers=headers).json()["items"]
        assert (
            len(
                client.get(
                    prefix + f"/workflow-definitions/{value['definition_id']}/revisions",
                    headers=headers,
                ).json()["items"]
            )
            == 2
        )

        rejected = client.post(
            prefix + f"/workflow-definitions/{value['definition_id']}/revisions",
            headers=headers | {"Idempotency-Key": "source-change"},
            json={
                "expected_revision": 2,
                "source_digest": revised.json()["source_digest"],
                "source": {"steps": {"not-allowed": {}}},
                "metadata": {"title": "Forbidden"},
                "layout": {},
            },
        )
        assert rejected.status_code == 400

        schema = client.get("/api/v1/openapi.json").json()
        definition_path = (
            prefix.replace(project["project_id"], "{project_id}") + "/workflow-definitions"
        )
        assert definition_path in schema["paths"]
        assert not any("candidates" in path for path in schema["paths"] if "authoring" in path)


def test_unified_platform_is_same_origin_and_legacy_routes_are_removed(database):
    composition = PlatformComposition.for_test(studio_root=WORK_ROOT / "capability-studio")
    with TestClient(create_app(database, composition=composition)) as client:
        platform = client.get("/")
        assert platform.status_code == 200
        assert "Groundwater V7" in platform.text
        script = re.search(r'src="([^"]+\.js)"', platform.text)
        assert script is not None
        assert client.get(script.group(1) + ".map").status_code == 404
        assert client.get("/projects/project-id/overview").status_code == 200
        assert client.get("/studio/").status_code == 404
        assert client.get("/ui/").status_code == 404


class _DataStub:
    def __init__(self, items=None):
        self.items = items or []
        self.calls = []

    def json(self, method, path, *, params=None, **kwargs):
        assert method == "GET"
        assert "/assets" in path
        assert kwargs["headers"]["X-Actor-ID"]
        params = params or {}
        self.calls.append({"path": path, "params": params})
        after = params.get("after")
        if after:
            return {"items": self.items[100:], "next_cursor": None}
        items = self.items[:100]
        return {
            "items": items,
            "next_cursor": "assets:page-2" if len(self.items) > 100 else None,
        }


def test_workspace_summary_is_viewer_safe_complete_and_dependency_aware(database, owner):
    actor, owner_token = owner
    service = ControlService(database)
    project = service.create_project(actor, "workspace-project", "Workspace")
    project_id = project["project_id"]
    service.create_task(
        actor,
        project_id,
        "workspace-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Summarize the workspace", "requirements": []},
            "budget": {"execution_count": 3, "max_in_flight": 1},
        },
    )
    viewer_id = database.create_principal("workspace-viewer")
    viewer_token = database.issue_token(viewer_id)["token"]
    with database.engine.begin() as connection:
        connection.execute(
            tables.members.insert().values(
                project_id=project_id, principal_id=viewer_id, role="VIEWER"
            )
        )

    app = create_app(database)
    app.state.data = _DataStub([{"asset_id": "a"}, {"asset_id": "b"}])
    path = f"/api/v1/projects/{project_id}/workspace-summary"
    with TestClient(app) as client:
        response = client.get(path, headers={"Authorization": "Bearer " + viewer_token})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["role"] == "VIEWER"
        assert value["phase"] == "P8.2"
        assert value["resources"] == {
            "assets": 2,
            "workflow_definitions": 0,
            "workflow_releases": 0,
            "operator_versions": 11,
            "operator_materials": 0,
        }
        assert value["tasks"] == {"OPEN": 1, "PAUSED": 0, "CANCELLED": 0, "COMPLETED": 0}
        assert set(value["executions"]) == {
            "RECORDED",
            "DISPATCHING",
            "RECONCILING",
            "QUEUED",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "SEALED",
            "REJECTED",
        }
        assert not any(value["executions"].values())
        assert client.get(path).status_code == 401

    unavailable = create_app(database)
    unavailable.state.data = None
    with TestClient(unavailable) as client:
        response = client.get(path, headers={"Authorization": "Bearer " + owner_token})
        assert response.status_code == 503
        assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"


def test_workspace_summary_follows_data_cursor_after_100_assets(database, owner):
    actor, token = owner
    service = ControlService(database)
    project_id = service.create_project(actor, "workspace-page", "Workspace page")["project_id"]
    app = create_app(database)
    data = _DataStub([{"asset_id": f"asset-{index:03d}"} for index in range(101)])
    app.state.data = data
    path = f"/api/v1/projects/{project_id}/workspace-summary"

    with TestClient(app) as client:
        response = client.get(path, headers={"Authorization": "Bearer " + token})

    assert response.status_code == 200, response.text
    assert response.json()["resources"]["assets"] == 101
    assert data.calls == [
        {"path": f"/internal/projects/{project_id}/assets", "params": {"limit": 100}},
        {
            "path": f"/internal/projects/{project_id}/assets",
            "params": {"limit": 100, "after": "assets:page-2"},
        },
    ]


def test_execution_summaries_filter_paginate_and_redact_internal_request(database, owner):
    actor, token = owner
    service = ControlService(database)
    project_id = service.create_project(actor, "execution-project", "Executions")["project_id"]
    task = service.create_task(
        actor,
        project_id,
        "execution-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Run a redacted action", "requirements": []},
            "budget": {"execution_count": 3, "max_in_flight": 2},
        },
    )
    target = {
        "kind": "OPERATOR",
        "operator_id": "p2-fixture",
        "version_id": "1.0.0",
        "execution_binding": {"kind": "REMOTE", "credential": "must-not-leak"},
    }
    service.grant(
        actor,
        project_id,
        "execution-grant",
        {
            "task_id": task["task_id"],
            "targets": [target],
            "resources": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    for index in range(2):
        service.submit_action(
            actor,
            project_id,
            f"execution-{index}",
            {
                "task_id": task["task_id"],
                "expected_spec_revision": 1,
                "target": target,
                "resources": [],
                "inputs": {"secret": f"credential-{index}"},
            },
        )

    path = f"/api/v1/projects/{project_id}/executions"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(database)) as client:
        first = client.get(path, headers=headers, params={"limit": 1})
        assert first.status_code == 200, first.text
        assert len(first.json()["items"]) == 1
        assert first.json()["next_cursor"]
        second = client.get(
            path, headers=headers, params={"limit": 1, "after": first.json()["next_cursor"]}
        )
        assert len(second.json()["items"]) == 1
        assert second.json()["next_cursor"] is None
        filtered = client.get(path, headers=headers, params={"state": "RECORDED"})
        assert len(filtered.json()["items"]) == 2
        payload = filtered.text
        assert "execution_binding" not in payload
        assert "must-not-leak" not in payload
        assert "credential-" not in payload
        assert all(item["target_id"] == "p2-fixture@1.0.0" for item in filtered.json()["items"])
        assert client.get(path, headers=headers, params={"state": "NOT_A_STATE"}).status_code == 400
