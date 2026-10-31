from typing import Annotated

import pytest
from fastapi import Body
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_api.composition import PlatformComposition
from platform_contracts.v1 import ErrorEnvelope, TaskRef


@pytest.fixture
def client():
    with TestClient(
        create_app(composition=PlatformComposition.for_test()),
        raise_server_exceptions=False,
    ) as client:
        yield client


def test_health_and_request_ids(client):
    response = client.get("/api/v1/health", headers={"X-Request-ID": "untrusted-client-value"})
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "platform-api",
        "phase": "P8.2",
        "contract_version": "1",
    }
    request_id = response.headers["X-Request-ID"]
    assert request_id != "untrusted-client-value"
    assert request_id != client.get("/api/v1/health").headers["X-Request-ID"]


@pytest.mark.parametrize("path", ["/api/v1/tasks", "/api/v1/executions", "/api/v1/knowledge"])
def test_future_business_routes_are_not_available(client, path):
    response = client.post(path, json={"name": "must not create state"})
    assert response.status_code == 404
    error = ErrorEnvelope.model_validate(response.json())
    assert error.code == "RESOURCE_NOT_FOUND"
    assert error.request_id == response.headers["X-Request-ID"]
    assert error.retry_mode == "NONE"


def test_method_not_allowed_preserves_allow_header(client):
    response = client.post("/api/v1/health")
    assert response.status_code == 405
    assert "GET" in response.headers["Allow"]
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"


def test_openapi_only_advertises_implemented_routes(client):
    schema = client.get("/api/v1/openapi.json").json()
    assert schema["info"]["description"].startswith("P8 / A7:")
    project = "/api/v1/projects/{project_id}"
    task = project + "/tasks/{task_id}"
    assert set(schema["paths"]) == {
        "/api/v1/health",
        "/api/v1/ready",
        "/api/v1/me",
        "/api/v1/projects",
        project,
        project + "/workspace-summary",
        project + "/tasks",
        task,
        task + "/spec-revisions",
        task + "/pause",
        task + "/resume",
        task + "/cancel",
        task + "/budget",
        task + "/model-usage",
        task + "/agent",
        task + "/agent/events",
        task + "/questions/{question_id}/answers",
        project + "/grants",
        project + "/grants/{grant_id}/revoke",
        project + "/commands/{command_id}",
        project + "/uploads",
        project + "/uploads/{upload_id}",
        project + "/uploads/{upload_id}/content",
        project + "/uploads/{upload_id}/finalize",
        project + "/assets",
        project + "/assets/{asset_id}/versions/{version_id}",
        project + "/assets/{asset_id}/semantic-revisions",
        project
        + "/assets/{asset_id}/versions/{version_id}/representations/{representation_id}/content",
        project + "/operators",
        project + "/operators/{operator_id}/versions/{version_id}",
        project + "/executions",
        project + "/executions/{execution_id}",
        project + "/executions/{execution_id}/cancel",
        project + "/executions/{execution_id}/steps",
        project + "/workflow-packages",
        project + "/workflow-packages/{package_id}",
        project + "/authoring/operator-descriptors",
        project + "/authoring/previews",
        project + "/authoring/validations/{validation_id}",
        project + "/workflow-definitions",
        project + "/workflow-definitions/{definition_id}",
        project + "/workflow-definitions/{definition_id}/revisions",
        project + "/workflow-definitions/{definition_id}/revisions/{revision}",
        project + "/workflow-definitions/{definition_id}/validations",
        project + "/workflow-definitions/{definition_id}/candidates",
        project + "/workflow-definitions/{definition_id}/candidates/{candidate_id}",
        project + "/workflow-definitions/{definition_id}/trial-runs",
        project + "/workflow-definitions/{definition_id}/trial-runs/{trial_id}",
        project + "/workflow-definitions/{definition_id}/releases",
        project + "/workflow-definitions/{definition_id}/releases/{version_label}",
        project
        + "/workflow-definitions/{definition_id}/releases/{version_label}/lifecycle-revisions",
        project + "/workflow-definitions/{definition_id}/releases/{version_label}/executions",
        project + "/workflow-releases",
        project + "/authoring/imports",
        project + "/operator-proposals",
        project + "/operator-proposals/{proposal_id}",
        project + "/operator-proposals/{proposal_id}/revisions",
        project + "/operator-proposals/{proposal_id}/submit",
        project + "/service-bindings",
        project + "/service-bindings/{service_binding_id}/revisions/{revision}",
        task + "/activities",
        task + "/deliverables",
        task + "/acceptance",
        task + "/acceptance/{acceptance_id}",
        task + "/acceptance/{acceptance_id}/reviews",
        project + "/knowledge/imports",
        project + "/knowledge/imports/{import_id}",
        project + "/knowledge/documents",
        project + "/knowledge/documents/{document_id}/versions/{version_id}",
        project + "/knowledge/search",
        project + "/knowledge/semantic-search",
        project + "/knowledge/fusion-search",
        project + "/knowledge/documents/{document_id}/versions/{version_id}/units/{unit_id}",
        project + "/knowledge/documents/{document_id}/versions/{version_id}/withdraw",
        task + "/analysis-revisions",
        task + "/analysis-revisions/{revision_id}/register",
        task + "/claims",
        task + "/research",
    }
    assert set(schema["paths"]["/api/v1/health"]) == {"get"}
    assert schema["components"]["schemas"]["HealthResponse"]["additionalProperties"] is False


def test_openapi_exposes_bounded_cursor_pagination_for_all_three_catalogues(client):
    schema = client.get("/api/v1/openapi.json").json()
    routes = (
        "/api/v1/projects/{project_id}/uploads",
        "/api/v1/projects/{project_id}/assets",
        "/api/v1/projects/{project_id}/knowledge/documents",
    )
    for route in routes:
        parameters = {item["name"]: item for item in schema["paths"][route]["get"]["parameters"]}
        assert parameters["after"]["required"] is False
        assert parameters["limit"]["schema"]["default"] == 100
        assert parameters["limit"]["schema"]["maximum"] == 100


def test_unconfigured_api_is_live_but_not_ready(client):
    assert client.get("/api/v1/ready").status_code == 503
    assert client.get("/api/v1/projects").status_code == 503
    assert (
        client.get("/api/v1/projects", headers={"Authorization": "Bearer unavailable"}).status_code
        == 503
    )


def test_internal_error_is_redacted_and_traceable():
    app = create_app(composition=PlatformComposition.for_test())

    @app.get("/test-only-failure")
    def failing_handler():
        raise RuntimeError("password=private; /private/host/database")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-only-failure")
        assert response.status_code == 500
        error = ErrorEnvelope.model_validate(response.json())
        assert error.code == "INTERNAL_ERROR"
        assert error.request_id == response.headers["X-Request-ID"]
        assert "private" not in response.text
        assert "RuntimeError" not in response.text
        assert error.retry_mode == "NONE"
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize("body", [b'{"task_id": "private",', b'{"private-secret": 42}'])
def test_validation_errors_use_safe_envelope(body):
    app = create_app(composition=PlatformComposition.for_test())

    @app.post("/test-only-contract")
    def validate_contract(payload: Annotated[TaskRef, Body()]):
        return payload

    with TestClient(app) as client:
        response = client.post(
            "/test-only-contract", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 400
    error = ErrorEnvelope.model_validate(response.json())
    assert error.code == "MALFORMED_REQUEST"
    assert error.request_id == response.headers["X-Request-ID"]
    assert "private" not in response.text


def test_app_factories_do_not_share_routes():
    first = create_app(composition=PlatformComposition.for_test())
    second = create_app(composition=PlatformComposition.for_test())

    @first.get("/test-only-local")
    def local_route():
        return {}

    with TestClient(second) as client:
        assert client.get("/test-only-local").status_code == 404
