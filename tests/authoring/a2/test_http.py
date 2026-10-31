from dataclasses import replace

from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_api.composition import PlatformComposition
from platform_core.application.control import ControlService


def test_preview_and_validation_routes_remain_after_a3(database, owner):
    actor, token = owner
    project = ControlService(database).create_project(actor, "a2-http", "A2")
    composition = replace(PlatformComposition.for_test(), cwl_image="sha256:" + "a" * 64)
    with TestClient(create_app(database, composition=composition)) as client:
        prefix = f"/api/v1/projects/{project['project_id']}"
        headers = {"Authorization": "Bearer " + token}
        descriptor = client.get(prefix + "/authoring/operator-descriptors", headers=headers).json()
        malformed = client.post(
            prefix + "/authoring/previews",
            headers=headers,
            json={
                "source": {
                    "profile": "v7-cwl-p4-v1",
                    "name": "empty",
                    "entrypoint": "main",
                    "documents": {
                        "main": {
                            "cwlVersion": "v1.2",
                            "class": "Workflow",
                            "inputs": {},
                            "outputs": {},
                            "steps": {},
                        }
                    },
                    "implementations": {},
                },
                "sample_bindings": {},
                "semantic_contract_digest": descriptor["semantic_contract_digest"],
            },
        )
        assert malformed.status_code == 200
        assert malformed.json()["status"] == "INCOMPATIBLE"
        paths = client.get("/api/v1/openapi.json").json()["paths"]
        assert "/api/v1/projects/{project_id}/authoring/previews" in paths
        assert (
            "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/validations"
            in paths
        )
        assert "/api/v1/projects/{project_id}/authoring/validations/{validation_id}" in paths
        assert (
            "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/candidates" in paths
        )
