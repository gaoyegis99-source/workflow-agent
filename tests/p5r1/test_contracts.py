import pytest
from platform_contracts.control import TaskSpec
from platform_contracts.science import AcceptanceCreate, DeliverableCreate
from pydantic import ValidationError

DIGEST = "sha256:" + "a" * 64
ASSET = {
    "project_id": "project",
    "asset_id": "asset",
    "version_id": "1",
    "version_manifest_digest": DIGEST,
}
EXPECTED = {
    "result_kind": "OPERATOR_RESULT",
    "operator_id": "statistics-linear-trend",
    "operator_version": "1.0.0",
    "output_port": "result",
    "schema_id": "p3-result-v1",
}
OUTPUT = {
    "kind": "OPERATOR_OUTPUT",
    "action_id": "action",
    "execution_id": "execution",
    "output_port": "result",
    "asset_ref": ASSET,
    "representation_id": "result",
    "representation_digest": DIGEST,
    "schema_id": "p3-result-v1",
    "schema_source": "MANIFEST",
}


def exact_spec(delivery="VERIFIED"):
    return {
        "profile": "open-science-v1",
        "delivery_class": delivery,
        "requirements": [],
        "required_outputs": [{"role_id": "trend_result", "necessary": True, "expected": EXPECTED}],
        "allow_manual_review": False,
        "max_check_executions": 1,
    }


def test_exact_output_contract_and_selected_registration_are_strict():
    task = TaskSpec.model_validate(
        {"goal": "Exact", "requirements": [], "acceptance_spec": exact_spec()}
    )
    assert task.acceptance_spec.required_outputs[0].role_id == "trend_result"
    deliverable = DeliverableCreate.model_validate(
        {
            "expected_spec_revision": 1,
            "role_bindings": [{"role_id": "trend_result", "output_ref": OUTPUT}],
        }
    )
    assert deliverable.role_bindings[0].output_ref.kind == "OPERATOR_OUTPUT"
    request = AcceptanceCreate.model_validate(
        {
            "expected_spec_revision": 1,
            "deliverable_id": "deliverable",
            "analysis_registration_id": "registration",
        }
    )
    assert request.analysis_registration_id == "registration"


def test_legacy_and_exact_output_requirements_or_selections_cannot_mix():
    mixed = exact_spec("EXPLORATORY")
    mixed["required_outputs"].append({"output_id": "legacy", "necessary": True})
    with pytest.raises(ValidationError):
        TaskSpec.model_validate({"goal": "Mixed", "requirements": [], "acceptance_spec": mixed})
    with pytest.raises(ValidationError):
        DeliverableCreate.model_validate(
            {
                "expected_spec_revision": 1,
                "executions": ["execution"],
                "role_bindings": [{"role_id": "trend_result", "output_ref": OUTPUT}],
            }
        )


def test_workflow_output_identity_freezes_node_and_result_position():
    value = DeliverableCreate.model_validate(
        {
            "expected_spec_revision": 1,
            "role_bindings": [
                {
                    "role_id": "trend_result",
                    "output_ref": {
                        "kind": "WORKFLOW_OUTPUT",
                        "parent_action_id": "parent-action",
                        "parent_execution_id": "parent-execution",
                        "node_action_id": "node-action",
                        "node_execution_id": "node-execution",
                        "call_path": ["trend"],
                        "scatter_path": [2],
                        "published_output_port": "results",
                        "published_path": [2, "result"],
                        "output_port": "result",
                        "asset_ref": ASSET,
                        "representation_id": "result",
                        "representation_digest": DIGEST,
                        "schema_id": "p3-result-v1",
                        "schema_source": "COMPATIBILITY_CATALOG",
                    },
                }
            ],
        }
    )
    output = value.role_bindings[0].output_ref
    assert output.scatter_path == [2]
    assert output.published_path == [2, "result"]
