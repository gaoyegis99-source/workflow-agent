import pytest
from platform_core.application.trial_coverage import coverage as project_coverage
from platform_core.application.trial_coverage import shape
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables

from tests.authoring.a3.test_candidate import admitted_trial


def case(*, profile="workflow-trial-case-v2", expected="EXECUTED"):
    return {
        "profile": profile,
        "required_steps": [
            {
                "node_id": "node-0",
                "call_path": ["scatter", "profile"],
                "scatter_indices": [0],
                "operator_id": "table-profile",
            }
        ],
        "planned_steps": [
            {
                "node_id": "node-0",
                "call_path": ["scatter", "profile"],
                "scatter_indices": [0],
                "operator_id": "table-profile",
                "expected": expected,
            }
        ]
        if profile == "workflow-trial-case-v2"
        else None,
        "deferred_templates": [],
    }


def node(node_id="node-0", *, indices=None, executed=True):
    return {
        "node_id": node_id,
        "request": {
            "call_path": ["scatter", "profile"],
            "scatter_indices": indices if indices is not None else [0],
        },
        "executed": executed,
        "observation": {"state": "SUCCEEDED" if executed else "SKIPPED"},
    }


def test_coverage_records_instances_shapes_and_skipped_branches():
    trial_case = case(expected="SKIPPED")
    coverage, failures = project_coverage(
        trial_case,
        [],
        {"reports": [[None, {"asset": {}, "representation_id": "report"}]]},
    )
    assert failures == []
    assert coverage["positive_compute"] is False
    assert coverage["instances"][0]["skipped"] == 1
    assert coverage["untested_branches"][0]["reason"] == "CONDITION_FALSE"
    assert coverage["final_output_shapes"]["reports"]["kind"] == "ARRAY"
    assert coverage["final_output_shapes"]["reports"]["item_shapes"][0]["kind"] == "ARRAY"


def test_duplicate_scatter_values_keep_distinct_observed_instances():
    trial_case = case()
    trial_case["planned_steps"].append(
        trial_case["planned_steps"][0] | {"node_id": "node-1", "scatter_indices": [1]}
    )
    coverage, failures = project_coverage(
        trial_case,
        [node(), node("node-1", indices=[1])],
        {},
    )
    assert failures == []
    assert coverage["instances"][0]["planned"] == 2
    assert coverage["instances"][0]["observed"] == 2
    assert coverage["instances"][0]["started"] == 2


def test_dynamic_and_unexpected_instances_are_distinguished():
    trial_case = case()
    trial_case["planned_steps"] = []
    trial_case["required_steps"] = []
    trial_case["deferred_templates"] = [
        {
            "template_id": "template",
            "call_path": ["scatter", "profile"],
            "index_depth": 1,
            "operator_id": "table-profile",
        }
    ]
    coverage, failures = project_coverage(
        trial_case,
        [node("dynamic", indices=[4])],
        {},
    )
    assert failures == []
    assert coverage["positive_compute"] is True
    _, failures = project_coverage(
        trial_case,
        [node("wrong-depth", indices=[4, 5])],
        {},
    )
    assert failures == ["wrong-depth"]


def test_legacy_a3_trial_case_does_not_become_invalid_after_upgrade():
    trial_case = case(profile="workflow-trial-case-v1")
    trial_case.pop("planned_steps")
    coverage, failures = project_coverage(trial_case, [node()], {})
    assert failures == []
    assert coverage["positive_compute"] is True


def test_all_skipped_pass_trial_cannot_alone_support_release(database, owner):
    actor, project, definition, service, candidate, trial = admitted_trial(database, owner)
    trial = service.trial(actor, project, definition, trial["trial_id"])
    execution_id = "all-skipped-execution"
    result = {
        "profile": "workflow-trial-result-v1",
        "trial_case_digest": trial["trial_case_digest"],
        "action_id": trial["action_id"],
        "execution_id": execution_id,
        "execution_state": "SUCCEEDED",
        "publication_state": "COMMITTED",
        "started_steps": 0,
        "assertions": [],
        "coverage": {"positive_compute": False},
        "reasons": [],
        "verdict": "PASS",
    }
    with database.transaction() as uow:
        uow.actions.update(trial["action_id"], execution_id=execution_id, state="RESOLVED")
        uow.connection.execute(
            tables.workflow_trials.update()
            .where(tables.workflow_trials.c.trial_id == trial["trial_id"])
            .values(
                case_verdict="PASS",
                case_result=result,
                case_result_digest=canonical_digest(result),
                finished_at=uow.now(),
            )
        )
    with pytest.raises(RuleViolation) as raised:
        service.create_release(
            actor,
            project,
            definition,
            "all-skipped-release",
            {
                "candidate_id": candidate["candidate_id"],
                "version_label": "1.0.0",
                "trial_ids": [trial["trial_id"]],
            },
        )
    assert raised.value.code == "PRECONDITION_UNRESOLVED"


@pytest.mark.parametrize("depth", [17, 18])
def test_excessively_nested_output_shape_is_rejected(depth):
    value = None
    for _ in range(depth):
        value = [value]
    with pytest.raises(Exception, match="deeply nested"):
        shape(value)
