from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.application.control import ControlService

from tests.authoring.a4.helpers import catalog_and_materials


def test_public_http_only_exposes_project_material_workflow(database, owner, monkeypatch):
    actor, token = owner
    project = ControlService(database).create_project(actor, "a4-http", "A4 HTTP")
    catalog, materials, release = catalog_and_materials()
    monkeypatch.setattr("platform_core.application.catalog.configured_catalog", lambda: catalog)
    prefix = f"/api/v1/projects/{project['project_id']}"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(database, release_lock=release)) as client:
        created = client.post(
            prefix + "/operator-proposals",
            headers=headers | {"Idempotency-Key": "create-proposal"},
            json={"stable_key": "table-profile-registration", "materials": materials},
        )
        assert created.status_code == 201, created.text
        proposal = created.json()
        submitted = client.post(
            prefix + f"/operator-proposals/{proposal['proposal_id']}/submit",
            headers=headers | {"Idempotency-Key": "submit-proposal"},
            json={
                "revision": 1,
                "materials_digest": proposal["head"]["materials_digest"],
            },
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["status"] == "SUBMITTED"
        assert client.get(prefix + "/operator-proposals", headers=headers).json()["items"]
        schema = client.get("/api/v1/openapi.json").json()
        paths = schema["paths"]
        assert "/api/v1/projects/{project_id}/operator-proposals" in paths
        assert not any("approve" in path or "deploy" in path for path in paths)


def test_viewer_cannot_create_and_owner_has_no_review_route(database, owner, monkeypatch):
    actor, token = owner
    project = ControlService(database).create_project(actor, "a4-permissions", "A4 Permissions")
    viewer_id = database.create_principal("viewer")
    database.set_member(project["project_id"], viewer_id, "VIEWER", actor=actor)
    viewer_token = database.issue_token(viewer_id)["token"]
    catalog, materials, release = catalog_and_materials()
    monkeypatch.setattr("platform_core.application.catalog.configured_catalog", lambda: catalog)
    prefix = f"/api/v1/projects/{project['project_id']}"
    with TestClient(create_app(database, release_lock=release)) as client:
        denied = client.post(
            prefix + "/operator-proposals",
            headers={
                "Authorization": "Bearer " + viewer_token,
                "Idempotency-Key": "viewer-create",
            },
            json={"stable_key": "denied", "materials": materials},
        )
        assert denied.status_code == 403
        assert (
            client.post(
                prefix + "/operator-proposals/unknown/approve",
                headers={"Authorization": "Bearer " + token, "Idempotency-Key": "no-web-review"},
                json={},
            ).status_code
            == 404
        )
