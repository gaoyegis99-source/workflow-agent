import json
from copy import deepcopy

import pytest
from platform_contracts.authoring import (
    WorkflowDefinitionCreate,
    WorkflowRevisionCreate,
    build_authoring_descriptor,
)
from platform_contracts.science import (
    TableAggregateParameters,
    TableProfileParameters,
    TableValidateParameters,
)
from platform_core.application.catalog import build_catalog
from platform_core.domain.authoring import canonical_digest
from pydantic import ValidationError


def test_descriptor_is_deterministic_and_covers_only_compute_operators():
    first = build_authoring_descriptor(build_catalog())
    second = build_authoring_descriptor(build_catalog())
    assert first == second
    assert len(first["operators"]) == 11
    identities = {item["operator_id"] for item in first["operators"]}
    assert "open-science-check" not in identities
    assert "capability.list_operators" not in identities
    assert all(item["descriptor_digest"].startswith("sha256:") for item in first["operators"])


def test_descriptor_marks_pydantic_cross_field_validation():
    descriptor = build_authoring_descriptor(build_catalog())
    by_id = {item["operator_id"]: item for item in descriptor["operators"]}
    assert by_id["table-validate"]["server_validation_required"]
    assert by_id["table-aggregate"]["server_validation_required"]
    assert by_id["table-profile"]["server_validation_required"]

    with pytest.raises(ValidationError):
        TableValidateParameters.model_validate({"rules": [{"rule_id": "r", "kind": "ROW_COUNT"}]})
    with pytest.raises(ValidationError):
        TableAggregateParameters.model_validate(
            {
                "null_key_policy": "GROUP",
                "measures": [{"measure_id": "rows", "operation": "COUNT_ROWS", "field_id": "f0"}],
            }
        )
    with pytest.raises(ValidationError):
        TableProfileParameters.model_validate({"fields": ["f0", "f0"]})


def test_presentation_change_does_not_change_semantic_identity():
    item = build_authoring_descriptor(build_catalog())["operators"][0]
    changed = deepcopy(item)
    changed["ui_hints"]["fields"]["groups"] = {"widget": "chips"}
    changed["presentation_digest"] = canonical_digest(changed["ui_hints"])
    assert changed["semantic_contract_digest"] == item["semantic_contract_digest"]
    assert changed["presentation_digest"] != item["presentation_digest"]


def test_draft_contract_round_trip_and_source_mutation_rejection():
    create = WorkflowDefinitionCreate.model_validate(
        {
            "expected_revision": 0,
            "stable_key": "water-level-review",
            "metadata": {"title": "Water level review", "labels": ["draft"]},
            "layout": {"viewport": {"x": 1, "y": 2}},
        }
    )
    encoded = create.model_dump(mode="json", by_alias=True)
    assert WorkflowDefinitionCreate.model_validate_json(json.dumps(encoded)) == create

    with pytest.raises(ValidationError):
        WorkflowRevisionCreate.model_validate(
            {
                "expected_revision": 1,
                "source_digest": "sha256:" + "0" * 64,
                "metadata": {"title": "Changed"},
                "layout": {},
                "source": {"steps": {"forbidden": {}}},
            }
        )
