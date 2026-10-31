from datetime import UTC, datetime

import pytest
from platform_contracts.research import (
    AnalysisRegistrationView,
    AnalysisRevisionView,
    ClaimRevisionView,
    ResearchView,
)
from platform_core.application.control import ControlService
from platform_core.application.errors import RuleViolation
from platform_core.application.research import Research
from platform_core.application.research_context import freeze_research_context

DIGEST = "sha256:" + "a" * 64


def evidence(project):
    return {
        "kind": "EVIDENCE",
        "ref": {
            "project_id": project,
            "document_id": "document",
            "version_id": "1",
            "unit_id": "unit",
            "content_digest": DIGEST,
            "start_char": 0,
            "end_char": 10,
            "exact_text_digest": DIGEST,
            "normalization": "utf8-nfc-lf-v1",
        },
    }


def asset(project):
    return {
        "kind": "ASSET",
        "ref": {
            "project_id": project,
            "asset_id": "asset",
            "version_id": "1",
            "version_manifest_digest": DIGEST,
        },
    }


def setup_task(database, owner):  # noqa: F811
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "project", "Research")["project_id"]
    task = control.create_task(
        actor,
        project,
        "task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Research protocol", "requirements": []},
            "budget": {"execution_count": 3, "max_in_flight": 1},
        },
    )
    return actor, project, task


def analysis_request(project, **changes):
    value = {
        "expected_spec_revision": 1,
        "supersedes_revision_id": None,
        "revision_reason": None,
        "question": "How do the two monitoring points compare?",
        "research_objects": ["well-a", "well-b"],
        "spatial_scope": "project area",
        "temporal_scope": None,
        "input_roles": [
            {"role": "primary", "source": asset(project), "description": "Primary series"}
        ],
        "methods": [],
        "primary_metrics": ["mean head"],
        "comparison_rules": [],
        "data_splits": [],
        "stopping_rules": [],
        "allowed_deviations": [],
        "checker_versions": [],
        "observed_sources": [evidence(project)],
        "groundwater_comparison": None,
    }
    return value | changes


def comparison_series(role, **changes):
    value = {
        "input_role": role,
        "monitoring_point_id": role + "-well",
        "quantity": "water_level_elevation",
        "unit": "metre",
        "vertical_datum": "national-1985",
        "temporal_scope": {
            "start": "2025-01-01T00:00:00+00:00",
            "end": "2026-01-01T00:00:00+00:00",
        },
        "aggregation": "monthly-mean",
    }
    return value | changes


def test_protocol_registration_claim_and_provenance_are_immutable(database, owner):  # noqa: F811
    actor, project, task = setup_task(database, owner)
    service = Research(database)
    request = analysis_request(project)
    resolved = [{"verified": True}, {"verified": True, "read_receipt_id": "receipt"}]
    created = service.create_analysis(
        actor, project, task["task_id"], "analysis", request, resolved
    )
    AnalysisRevisionView.model_validate(created)
    assert (
        service.create_analysis(actor, project, task["task_id"], "analysis", request, []) == created
    )
    with pytest.raises(RuleViolation) as conflict:
        service.create_analysis(
            actor,
            project,
            task["task_id"],
            "analysis",
            request | {"question": "Different"},
            resolved,
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

    registered = service.register(
        actor,
        project,
        task["task_id"],
        created["revision_id"],
        "register",
        {"expected_spec_revision": 1},
    )
    AnalysisRegistrationView.model_validate(registered)
    assert registered["observation_watermark"][0]["read_receipt_id"] == "receipt"

    claim_request = {
        "expected_spec_revision": 1,
        "analysis_registration_id": registered["registration_id"],
        "claim_id": None,
        "expected_claim_revision": None,
        "revision_reason": None,
        "proposition_type": "DESCRIPTIVE",
        "statement": "The cited source declares the reference datum.",
        "scope": {"objects": ["well-a"], "spatial_scope": None, "temporal_scope": None},
        "quantity": None,
        "limitations": ["The source is declared and not independently corroborated"],
        "sources": [{"relation": "CITED", "role": "basis", "source": evidence(project)}],
        "support_status": "UNVERIFIED",
    }
    claim = service.create_claim(
        actor,
        project,
        task["task_id"],
        "claim",
        claim_request,
        [{"verified": True, "read_receipt_id": "claim-read"}],
    )
    claim_view = ClaimRevisionView.model_validate(claim)
    research = ResearchView.model_validate(service.view(actor, project, task["task_id"]))
    assert {item.provenance_kind for item in research.provenance} == {
        "DECLARED_PROVENANCE",
        "OBSERVED_PROVENANCE",
    }
    assert claim_view.support_status == "UNVERIFIED"

    successor_request = analysis_request(
        project,
        supersedes_revision_id=created["revision_id"],
        revision_reason="New comparison scope",
        question="Revised question",
        observed_sources=[],
    )
    successor = service.create_analysis(
        actor,
        project,
        task["task_id"],
        "analysis-2",
        successor_request,
        [{"verified": True}],
    )
    assert successor["revision"] == 2
    updated = ResearchView.model_validate(service.view(actor, project, task["task_id"]))
    assert updated.analysis_revisions[0].status == "SUPERSEDED"
    with pytest.raises(RuleViolation) as stale:
        service.create_claim(
            actor,
            project,
            task["task_id"],
            "claim-after-supersede",
            claim_request,
            [{"verified": True, "read_receipt_id": "later"}],
        )
    assert stale.value.code == "REVISION_CONFLICT"


def test_cross_project_observed_source_is_rejected(database, owner):  # noqa: F811
    actor, project, task = setup_task(database, owner)
    request = analysis_request(project, observed_sources=[evidence("other")])
    with pytest.raises(RuleViolation) as denied:
        Research(database).create_analysis(
            actor,
            project,
            task["task_id"],
            "cross-project",
            request,
            [{"verified": True}, {"verified": True, "read_receipt_id": "receipt"}],
        )
    assert denied.value.code == "SCOPE_DENIED"


def test_execution_source_from_another_task_is_rejected():
    class Actions:
        @staticmethod
        def by_execution(project, execution_id):
            assert project == "project" and execution_id == "execution"
            return {
                "task_id": "other-task",
                "action_id": "action",
                "state": "SUCCEEDED",
                "last_external_observation": None,
            }

    class UnitOfWork:
        actions = Actions()

    with pytest.raises(RuleViolation) as denied:
        Research._source_fact(
            UnitOfWork(),
            "project",
            "current-task",
            {"kind": "EXECUTION", "execution_id": "execution"},
            None,
            datetime.now(UTC),
        )
    assert denied.value.code == "SCOPE_DENIED"


def test_action_policy_is_opt_in_local_and_frozen_to_current_registration(
    database,  # noqa: F811
    owner,  # noqa: F811
):
    actor, project, task_view = setup_task(database, owner)
    service = Research(database)
    roles = [
        {"role": "a", "source": asset(project), "description": "Well A"},
        {"role": "b", "source": asset(project), "description": "Well B"},
    ]
    missing = service.create_analysis(
        actor,
        project,
        task_view["task_id"],
        "missing-policy",
        analysis_request(
            project,
            input_roles=roles,
            observed_sources=[],
            groundwater_comparison={
                "kind": "GROUNDWATER_HEAD_COMPARISON",
                "series": [
                    comparison_series("a"),
                    comparison_series("b", vertical_datum=None),
                ],
            },
        ),
        [{"verified": True}, {"verified": True}],
    )
    registration = service.register(
        actor,
        project,
        task_view["task_id"],
        missing["revision_id"],
        "register-missing",
        {"expected_spec_revision": 1},
    )
    with database.transaction() as uow:
        task = uow.tasks.get(project, task_view["task_id"])
        linked = freeze_research_context(
            uow,
            project,
            task,
            {"analysis_registration_id": registration["registration_id"]},
        )
        assert linked["policy_result"] == "MISSING"
        with pytest.raises(RuleViolation) as blocked:
            freeze_research_context(
                uow,
                project,
                task,
                {
                    "analysis_registration_id": registration["registration_id"],
                    "policy_requirement": "groundwater-head-comparison-v1",
                },
            )
        assert blocked.value.code == "PRECONDITION_UNRESOLVED"

    applicable = service.create_analysis(
        actor,
        project,
        task_view["task_id"],
        "applicable-policy",
        analysis_request(
            project,
            input_roles=roles,
            observed_sources=[],
            groundwater_comparison={
                "kind": "GROUNDWATER_HEAD_COMPARISON",
                "series": [comparison_series("a"), comparison_series("b", unit="foot")],
            },
        ),
        [{"verified": True}, {"verified": True}],
    )
    applicable_registration = service.register(
        actor,
        project,
        task_view["task_id"],
        applicable["revision_id"],
        "register-applicable",
        {"expected_spec_revision": 1},
    )
    with database.transaction() as uow:
        task = uow.tasks.get(project, task_view["task_id"])
        frozen = freeze_research_context(
            uow,
            project,
            task,
            {
                "analysis_registration_id": applicable_registration["registration_id"],
                "policy_requirement": "groundwater-head-comparison-v1",
            },
        )
        assert frozen["policy_result"] == "APPLICABLE"
        assert (
            frozen["policy_digest"] == applicable_registration["policy_decision"]["policy_digest"]
        )
