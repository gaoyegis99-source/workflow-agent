from copy import deepcopy

import pytest
from platform_contracts.operator_proposals import OperatorProposalCreate
from pydantic import ValidationError

from tests.authoring.a4.helpers import catalog_and_materials


def test_exact_existing_material_is_valid_and_text_remains_inert():
    _, materials, _ = catalog_and_materials()
    materials["source"]["package_source"] = "https://invalid.example/$(touch never-fetched)"
    value = OperatorProposalCreate.model_validate(
        {"stable_key": "table-profile-registration", "materials": materials}
    )
    assert value.materials.source.package_source.endswith("never-fetched)")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol", "CUSTOM_SHELL"),
        ("resource_profile", "unbounded-root-v1"),
        ("binding_kind", "REMOTE"),
    ],
)
def test_unknown_execution_material_is_rejected(field, value):
    _, materials, _ = catalog_and_materials()
    materials["implementation"][field] = value
    with pytest.raises(ValidationError):
        OperatorProposalCreate.model_validate(
            {"stable_key": "bad-registration", "materials": materials}
        )


def test_forged_semantic_and_implementation_digests_are_rejected():
    _, materials, _ = catalog_and_materials()
    forged = deepcopy(materials)
    forged["operator"]["outputs"] = {"secret": "File"}
    with pytest.raises(ValidationError):
        OperatorProposalCreate.model_validate({"stable_key": "forged", "materials": forged})
    forged = deepcopy(materials)
    forged["implementation"]["image_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValidationError):
        OperatorProposalCreate.model_validate({"stable_key": "forged", "materials": forged})
