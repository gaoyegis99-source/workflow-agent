import pytest
from platform_contracts.science import AnalysisParameters, UploadCreate
from platform_core.domain.control import ExternalObservation, RuleViolation
from pydantic import ValidationError


def test_queued_identity_does_not_imply_execution():
    ExternalObservation("e:1", "a", 1, "QUEUED", "exec", False).validate()
    ExternalObservation("e:2", "a", 2, "CANCELLED", "exec", False).validate()
    with pytest.raises(RuleViolation):
        ExternalObservation("e:1", "a", 1, "QUEUED", "exec", True).validate()


@pytest.mark.parametrize(
    "change",
    [
        {"groups": []},
        {"groups": ["A", "A"]},
        {"start": "2024-01-01"},
        {"frequency": "minute"},
        {"min_samples": True},
        {"end": "2023-01-01T00:00:00Z"},
    ],
)
def test_bad_analysis_contract(change):
    valid = dict(
        groups=["A"], start="2024-01-01T00:00:00Z", end="2025-01-01T00:00:00Z", frequency="day"
    )
    with pytest.raises(ValidationError):
        AnalysisParameters.model_validate(valid | change)


def test_upload_limits_and_unknown_fields():
    with pytest.raises(ValidationError):
        UploadCreate(filename="x", format="csv", size=21 * 1024 * 1024, digest="sha256:" + "0" * 64)


def test_public_p3_query_responses_have_explicit_schemas():
    from platform_api.app import create_app

    paths = create_app().openapi()["paths"]
    for route in (
        "/api/v1/projects/{project_id}/uploads/{upload_id}",
        "/api/v1/projects/{project_id}/assets/{asset_id}/versions/{version_id}",
        "/api/v1/projects/{project_id}/executions/{execution_id}",
        "/api/v1/projects/{project_id}/tasks/{task_id}/acceptance/{acceptance_id}",
    ):
        schema = paths[route]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" in schema, route
