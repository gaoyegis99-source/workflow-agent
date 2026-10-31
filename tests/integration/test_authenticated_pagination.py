"""Authenticated Platform gateway probes for the three public catalogues.

The Data and Knowledge service contracts are exercised separately in P5.1.
This test closes the evidence gap in the public OpenAPI probe by running the
real Platform routes with an authenticated database principal and deterministic
service doubles.  It verifies that pagination parameters and cursors are
forwarded unchanged, rather than replacing the service tests or production
data with a browser interception.
"""

from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.application.control import ControlService

from tests.platform.database.conftest import database, owner, postgres  # noqa: F401, F811

DIGEST = "sha256:" + "0" * 64


def _asset(project: str, asset_id: str, version_id: str) -> dict:
    return {
        "ref": {
            "project_id": project,
            "asset_id": asset_id,
            "version_id": version_id,
            "version_manifest_digest": DIGEST,
        },
        "manifest": {"kind": "OBSERVATIONS", "representations": []},
    }


def _upload(project: str, upload_id: str) -> dict:
    return {
        "filename": f"{upload_id}.txt",
        "format": "text",
        "size": 1,
        "digest": DIGEST,
        "upload_id": upload_id,
        "project_id": project,
        "status": "READY",
        "actor_id": "owner",
        "conversion": None,
        "asset": None,
        "error": None,
    }


def _document(project: str, document_id: str, version_id: str) -> dict:
    return {
        "project_id": project,
        "document_id": document_id,
        "version_id": version_id,
        "title": "Evidence guide",
        "source": {
            "project_id": project,
            "asset_id": "asset-001",
            "version_id": "version-001",
            "version_manifest_digest": DIGEST,
        },
        "source_digest": DIGEST,
        "content_digest": DIGEST,
        "format": "text",
        "normalization": "utf8-nfc-lf-v1",
        "chunking": "paragraph-window-v1",
        "availability": "ACTIVE",
        "epistemic_status": "DECLARED",
        "review_state": "UNREVIEWED",
        "lifecycle_revision": 1,
        "unit_count": 1,
        "withdrawal_reason": None,
    }


class RecordingRemote:
    def __init__(self, project: str):
        self.project = project
        self.calls: list[dict] = []

    def json(self, method, path, *, headers=None, params=None, **kwargs):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": headers or {},
                "params": params or {},
            }
        )
        assert headers and headers["X-Actor-ID"]
        if path.endswith("/uploads"):
            return {"items": [_upload(self.project, "upload-101")], "next_cursor": "upload-cursor"}
        if path.endswith("/assets"):
            return {
                "items": [_asset(self.project, "asset-101", "version-101")],
                "next_cursor": "asset-cursor",
            }
        if path.endswith("/documents"):
            return {
                "items": [_document(self.project, "document-101", "version-101")],
                "next_cursor": "document-cursor",
            }
        raise AssertionError(path)


def test_authenticated_public_catalogues_forward_cursor_and_limit(database, owner):  # noqa: F811
    actor, token = owner
    project = ControlService(database).create_project(actor, "gateway-pagination", "Gateway")
    project_id = project["project_id"]
    app = create_app(database)
    data = RecordingRemote(project_id)
    knowledge = RecordingRemote(project_id)
    app.state.data = data
    app.state.knowledge = knowledge

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        for path, remote, expected_path in (
            ("uploads", data, f"/internal/projects/{project_id}/uploads"),
            ("assets", data, f"/internal/projects/{project_id}/assets"),
            (
                "knowledge/documents",
                knowledge,
                f"/internal/projects/{project_id}/knowledge/documents",
            ),
        ):
            response = client.get(
                f"/api/v1/projects/{project_id}/{path}?limit=100&after=cursor-100",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            assert response.json()["next_cursor"].endswith("cursor")
            assert remote.calls[-1] == {
                "method": "GET",
                "path": expected_path,
                "headers": {"X-Actor-ID": actor.principal_id},
                "params": {"limit": 100, "after": "cursor-100"},
            }
