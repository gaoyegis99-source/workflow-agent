import pytest
from platform_contracts.authoring import WorkflowReleaseCreate, WorkflowTrialCreate
from platform_core.application.authoring_release import semver_sort
from pydantic import ValidationError


def test_semver_is_canonical_and_orderable():
    assert semver_sort("1.0.0-alpha.1") < semver_sort("1.0.0")
    assert semver_sort("1.9.0") < semver_sort("1.10.0")
    for value in ("latest", "01.0.0", "1.0.0+build", " 1.0.0"):
        with pytest.raises(ValidationError):
            WorkflowReleaseCreate(
                candidate_id="candidate", version_label=value, trial_ids=["trial"]
            )


def test_trial_rejects_duplicate_assertion_location():
    with pytest.raises(ValidationError):
        WorkflowTrialCreate(
            candidate_id="candidate",
            task_id="task",
            expected_spec_revision=1,
            inputs={},
            max_node_executions=1,
            trial_expectations=[
                {"output": "result", "path": [0], "schema_id": "schema"},
                {"output": "result", "path": [0], "schema_id": "schema"},
            ],
        )
