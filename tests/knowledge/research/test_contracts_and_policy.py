import pytest
from platform_contracts.research import AnalysisRevisionCreate, ClaimRevisionCreate
from platform_core.domain.research import (
    POLICY_DIGEST,
    evaluate_groundwater_comparison,
)
from pydantic import ValidationError

DIGEST = "sha256:" + "a" * 64
ASSET = {
    "kind": "ASSET",
    "ref": {
        "project_id": "project",
        "asset_id": "asset",
        "version_id": "1",
        "version_manifest_digest": DIGEST,
    },
}


def series(role, **changes):
    value = {
        "input_role": role,
        "monitoring_point_id": role + "-well",
        "quantity": "water_level_elevation",
        "unit": "metre",
        "vertical_datum": "1985-national-height",
        "temporal_scope": {
            "start": "2025-01-01T00:00:00+00:00",
            "end": "2026-01-01T00:00:00+00:00",
        },
        "aggregation": "monthly-mean",
    }
    return value | changes


def test_research_contracts_are_closed_and_revision_shapes_are_explicit():
    analysis = AnalysisRevisionCreate(
        expected_spec_revision=1,
        question="Compare groundwater heads",
        research_objects=["well-a", "well-b"],
        input_roles=[
            {"role": "a", "source": ASSET, "description": "First monitoring series"},
            {"role": "b", "source": ASSET, "description": "Second monitoring series"},
        ],
        groundwater_comparison={
            "kind": "GROUNDWATER_HEAD_COMPARISON",
            "series": [series("a"), series("b")],
        },
    )
    assert analysis.supersedes_revision_id is None
    with pytest.raises(ValidationError):
        AnalysisRevisionCreate(
            expected_spec_revision=1,
            question="Invalid successor",
            research_objects=["well"],
            supersedes_revision_id="old",
        )
    with pytest.raises(ValidationError):
        AnalysisRevisionCreate(
            expected_spec_revision=1,
            question="Unknown role",
            research_objects=["well"],
            groundwater_comparison={
                "kind": "GROUNDWATER_HEAD_COMPARISON",
                "series": [series("a"), series("b")],
            },
        )
    with pytest.raises(ValidationError):
        ClaimRevisionCreate(
            expected_spec_revision=1,
            analysis_registration_id="registration",
            claim_id="claim",
            proposition_type="DESCRIPTIVE",
            statement="A statement",
            scope={"objects": ["well"]},
        )
    with pytest.raises(ValidationError):
        AnalysisRevisionCreate(
            expected_spec_revision=1,
            question="Duplicate observations",
            research_objects=["well"],
            observed_sources=[ASSET, ASSET],
        )
    duplicate_use = {"relation": "CITED", "role": "basis", "source": ASSET}
    with pytest.raises(ValidationError):
        ClaimRevisionCreate(
            expected_spec_revision=1,
            analysis_registration_id="registration",
            proposition_type="DESCRIPTIVE",
            statement="Duplicate provenance",
            scope={"objects": ["well"]},
            sources=[duplicate_use, duplicate_use],
        )


def test_groundwater_policy_is_local_deterministic_and_does_not_block_unrelated_work():
    not_applicable = evaluate_groundwater_comparison(None)
    assert not_applicable["result"] == "NOT_APPLICABLE"
    assert not_applicable["policy_digest"] == POLICY_DIGEST

    comparison = {"series": [series("a"), series("b", unit="foot")]}
    first = evaluate_groundwater_comparison(comparison)
    assert first == evaluate_groundwater_comparison(comparison)
    assert first["result"] == "APPLICABLE"

    missing = evaluate_groundwater_comparison(
        {"series": [series("a"), series("b", quantity="depth_to_water")]}
    )
    assert missing["result"] == "MISSING"
    assert "ELEVATION_CONVERSION_MISSING" in {item["code"] for item in missing["reasons"]}

    conflict = evaluate_groundwater_comparison(
        {
            "series": [
                series("a"),
                series("b", unit="degree", vertical_datum="local-datum"),
            ]
        }
    )
    assert conflict["result"] == "CONFLICT"
    assert {"UNIT_NOT_CONVERTIBLE", "VERTICAL_DATUM_CONFLICT"} <= {
        item["code"] for item in conflict["reasons"]
    }
