import pytest
from platform_core.application.open_acceptance import compile_coverage
from platform_core.application.output_identity import exact_deliverable_digest
from platform_core.domain.control import RuleViolation

from .test_contracts import OUTPUT, exact_spec


def candidate(mode="EXACT"):
    return {
        "digest": "candidate",
        "spec_revision": 1,
        "identity_mode": mode,
        "payload": {
            "identity_mode": mode,
            "artifacts": [
                {
                    "ref": OUTPUT["asset_ref"],
                    "manifest": {
                        "representations": [
                            {
                                "representation_id": "result",
                                "digest": OUTPUT["representation_digest"],
                            }
                        ]
                    },
                }
            ],
            "executions": [],
            "workflow_outputs": [],
            "role_bindings": [{"role_id": "trend_result", "output_ref": OUTPUT}],
        },
    }


def registration(identity="registration", revision_id="analysis-revision"):
    return {
        "registration_id": identity,
        "analysis_id": "analysis",
        "revision_id": revision_id,
        "revision": 1,
        "task_spec_revision": 1,
        "observation_watermark": [],
        "watermark_digest": "sha256:" + "c" * 64,
        "policy_decision_id": "policy",
    }


def test_schema_v2_freezes_role_binding_and_explicit_registration():
    plan, _ = compile_coverage(
        exact_spec(), candidate(), [registration()], [], [], [], [], "registration"
    )
    assert plan["schema_version"] == 2
    assert plan["analysis_registration_id"] == "registration"
    assert plan["role_bindings"] == {"trend_result": OUTPUT}
    role = next(item for item in plan["items"] if item.get("role_id") == "trend_result")
    assert role["output_ref"] == OUTPUT
    assert plan["limitations"] == []


@pytest.mark.parametrize("selected", [None, "wrong-registration"])
def test_verified_plan_rejects_missing_or_wrong_explicit_registration(selected):
    with pytest.raises(RuleViolation) as error:
        compile_coverage(exact_spec(), candidate(), [registration()], [], [], [], [], selected)
    assert error.value.code == "PRECONDITION_UNRESOLVED"


def test_superseded_or_old_spec_registration_is_rejected():
    with pytest.raises(RuleViolation):
        compile_coverage(
            exact_spec(),
            candidate(),
            [registration(), registration("new", "new-revision")],
            [],
            [],
            [],
            [{"revision_id": "new-revision", "supersedes_revision_id": "analysis-revision"}],
            "registration",
        )
    with pytest.raises(RuleViolation):
        compile_coverage(
            exact_spec(),
            candidate(),
            [registration() | {"task_spec_revision": 2}],
            [],
            [],
            [],
            [],
            "registration",
        )


def test_legacy_exploratory_plan_is_not_executable():
    spec = exact_spec("EXPLORATORY") | {
        "required_outputs": [{"output_id": "result", "necessary": True}]
    }
    value = candidate("LEGACY")
    value["payload"]["role_bindings"] = []
    with pytest.raises(RuleViolation) as error:
        compile_coverage(spec, value, [], [], [], [], [], None)
    assert error.value.code == "PRECONDITION_UNRESOLVED"


def test_exchanging_the_same_entities_between_roles_changes_deliverable_identity():
    first = OUTPUT
    second = OUTPUT | {"action_id": "action-2", "execution_id": "execution-2"}
    original = [
        {"role_id": "primary", "output_ref": first},
        {"role_id": "comparison", "output_ref": second},
    ]
    exchanged = [
        {"role_id": "primary", "output_ref": second},
        {"role_id": "comparison", "output_ref": first},
    ]
    assert exact_deliverable_digest(original) != exact_deliverable_digest(exchanged)
