from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from platform_contracts.authoring import build_authoring_descriptor, validate_operator_parameters
from platform_core.application.authoring import (
    AuthoringService,
    AuthoringValidationProcessor,
    semantic_environment,
)
from platform_core.application.authoring_release import AuthoringReleaseService
from platform_core.application.catalog import build_catalog
from platform_core.application.control import ControlService
from platform_core.application.trial_verdict import TrialVerdictProcessor
from platform_core.application.workflows import PackageProcessor
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables
from platform_infrastructure.repositories import AuthoringRepository

from tests.authoring.a2.helpers import profile_source


class Validator:
    image_id = "sha256:" + "a" * 64

    def validate(self, compiled):
        assert compiled["files"]


def frozen_definition(database, owner):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "a3-project", "A3")
    descriptor = build_authoring_descriptor(build_catalog())
    environment = semantic_environment(descriptor, Validator.image_id, None)
    service = AuthoringService(
        database,
        descriptor=descriptor,
        parameter_validator=validate_operator_parameters,
        environment=environment,
    )
    definition = service.create(
        actor,
        project["project_id"],
        "definition",
        {
            "expected_revision": 0,
            "stable_key": "a3-flow",
            "metadata": {"title": "A3"},
            "layout": {},
        },
    )
    revision = service.append(
        actor,
        project["project_id"],
        definition["definition_id"],
        "revision",
        {
            "expected_revision": 1,
            "source_digest": definition["head"]["source_digest"],
            "source": profile_source(),
            "metadata": {"title": "A3"},
            "layout": {},
        },
    )
    validation = service.request_validation(
        actor,
        project["project_id"],
        definition["definition_id"],
        "validation",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "semantic_contract_digest": environment["semantic_contract_digest"],
        },
    )
    assert AuthoringValidationProcessor(
        database,
        Validator(),
        descriptor,
        validate_operator_parameters,
        environment,
        operator_catalog=build_catalog(),
        owner="validation-worker",
    ).process_one()
    return (
        actor,
        project["project_id"],
        definition["definition_id"],
        revision,
        validation,
        descriptor,
        environment,
    )


def test_candidate_freezes_package_atomically_and_becomes_ready(database, owner):
    actor, project, definition, revision, validation, descriptor, environment = frozen_definition(
        database, owner
    )
    service = AuthoringReleaseService(
        database,
        descriptor=descriptor,
        environment=environment,
        operator_catalog=build_catalog(),
    )
    request = {
        "revision": revision["revision"],
        "source_digest": revision["source_digest"],
        "validation_id": validation["validation_id"],
    }
    receipt = service.create_candidate(actor, project, definition, "candidate", request)
    assert service.create_candidate(actor, project, definition, "candidate", request) == receipt
    assert receipt["status"] == "QUEUED"
    assert PackageProcessor(
        database,
        Validator(),
        operator_catalog=build_catalog(),
        owner="package-worker",
    ).process_one()
    view = service.candidate(actor, project, definition, receipt["candidate_id"])
    assert view["status"] == "READY"
    assert view["current_valid"]
    assert view["package_ref"]["manifest_digest"].startswith("sha256:")


def test_candidate_compiler_accepts_a5_graph_features(database, owner):
    source = profile_source()
    source["documents"]["sub"] = source["documents"]["main"]
    source["documents"]["sub"]["inputs"]["enabled"] = "boolean"
    source["documents"]["main"] = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {"table_input": "File", "enabled": "boolean"},
        "outputs": {"report": {"type": ["null", "File"], "outputSource": "gated/report"}},
        "steps": {
            "gated": {
                "run": "sub",
                "in": {"table_input": "table_input", "enabled": "enabled"},
                "out": ["report"],
                "when": "$(inputs.enabled)",
            }
        },
    }
    assert PackageProcessor.freeze(source, operator_catalog=build_catalog())["invocations"]


def test_candidate_failure_rolls_back_its_workflow_package(database, owner, monkeypatch):
    actor, project, definition, revision, validation, descriptor, environment = frozen_definition(
        database, owner
    )
    service = AuthoringReleaseService(
        database,
        descriptor=descriptor,
        environment=environment,
        operator_catalog=build_catalog(),
    )

    def fail_candidate(*_args, **_kwargs):
        raise RuntimeError("injected candidate insert failure")

    monkeypatch.setattr(AuthoringRepository, "add_candidate", fail_candidate)
    with pytest.raises(RuntimeError, match="injected candidate"):
        service.create_candidate(
            actor,
            project,
            definition,
            "candidate-rollback",
            {
                "revision": revision["revision"],
                "source_digest": revision["source_digest"],
                "validation_id": validation["validation_id"],
            },
        )
    with database.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.workflow_packages)) == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.workflow_candidates))
            == 0
        )


class Data:
    def __init__(self, selection):
        self.selection = selection

    def json(self, method, path, **kwargs):
        assert method == "POST" and path.endswith("/resolve")
        assert kwargs["json"] == self.selection
        return {
            "selection": self.selection,
            "table": {
                "row_count": 2,
                "fields": [{"field_id": "f0", "name": "value", "type": "double"}],
            },
            "manifest": {},
        }


def ready_candidate(database, owner):
    values = frozen_definition(database, owner)
    actor, project, definition, revision, validation, descriptor, environment = values
    service = AuthoringReleaseService(
        database,
        descriptor=descriptor,
        environment=environment,
        operator_catalog=build_catalog(),
    )
    receipt = service.create_candidate(
        actor,
        project,
        definition,
        "candidate-trial",
        {
            "revision": revision["revision"],
            "source_digest": revision["source_digest"],
            "validation_id": validation["validation_id"],
        },
    )
    assert PackageProcessor(
        database,
        Validator(),
        operator_catalog=build_catalog(),
        owner="package-trial",
    ).process_one()
    return values, service, service.candidate(actor, project, definition, receipt["candidate_id"])


def admitted_trial(database, owner, *, expectation_digest=None):
    values, service, candidate = ready_candidate(database, owner)
    actor, project, definition, *_ = values
    control = ControlService(database)
    task = control.create_task(
        actor,
        project,
        "verdict-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "trial", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    asset = {
        "project_id": project,
        "asset_id": "source-asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "b" * 64,
    }
    selection = {
        "kind": "TABLE",
        "asset": asset,
        "representation_id": "normalized",
        "representation_digest": "sha256:" + "c" * 64,
    }
    leaf = profile_source()["implementations"]["profile_impl"]
    target = {"kind": "WORKFLOW", "package_ref": candidate["package_ref"]}
    control.grant(
        actor,
        project,
        "verdict-grant",
        {
            "task_id": task["task_id"],
            "targets": [target, leaf],
            "resources": [asset],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    service.data = Data(selection)
    expectation = {
        "output": "report",
        "path": [],
        "schema_id": "table-profile-report-v1",
    }
    if expectation_digest:
        expectation["representation_digest"] = expectation_digest
    result = service.create_trial(
        actor,
        project,
        definition,
        "verdict-trial",
        {
            "candidate_id": candidate["candidate_id"],
            "task_id": task["task_id"],
            "expected_spec_revision": 1,
            "inputs": {"table_input": selection},
            "max_node_executions": 1,
            "trial_expectations": [expectation],
        },
    )
    return actor, project, definition, service, candidate, result


def test_trial_admission_is_atomic_and_replay_precedes_data(database, owner):
    values, service, candidate = ready_candidate(database, owner)
    actor, project, definition, *_ = values
    control = ControlService(database)
    task = control.create_task(
        actor,
        project,
        "trial-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "trial", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    asset = {
        "project_id": project,
        "asset_id": "asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "b" * 64,
    }
    selection = {
        "kind": "TABLE",
        "asset": asset,
        "representation_id": "normalized",
        "representation_digest": "sha256:" + "c" * 64,
    }
    leaf = profile_source()["implementations"]["profile_impl"]
    target = {"kind": "WORKFLOW", "package_ref": candidate["package_ref"]}
    control.grant(
        actor,
        project,
        "trial-grant",
        {
            "task_id": task["task_id"],
            "targets": [target, leaf],
            "resources": [asset],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    service.data = Data(selection)
    request = {
        "candidate_id": candidate["candidate_id"],
        "task_id": task["task_id"],
        "expected_spec_revision": 1,
        "inputs": {"table_input": selection},
        "max_node_executions": 1,
        "trial_expectations": [
            {
                "output": "report",
                "path": [],
                "schema_id": "table-profile-report-v1",
            }
        ],
    }
    result = service.create_trial(actor, project, definition, "trial", request)
    assert result["trial_case_digest"].startswith("sha256:")
    service.data = None
    assert service.create_trial(actor, project, definition, "trial", request) == result
    with pytest.raises(RuleViolation) as conflict:
        service.create_trial(
            actor,
            project,
            definition,
            "trial",
            request | {"max_node_executions": 2},
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.actions)) == 1
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.workflow_trials)) == 1
        )


def test_trial_without_grant_rolls_back_action_budget_and_trial(database, owner):
    values, service, candidate = ready_candidate(database, owner)
    actor, project, definition, *_ = values
    task = ControlService(database).create_task(
        actor,
        project,
        "ungranted-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "trial", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    asset = {
        "project_id": project,
        "asset_id": "asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "b" * 64,
    }
    selection = {
        "kind": "TABLE",
        "asset": asset,
        "representation_id": "normalized",
        "representation_digest": "sha256:" + "c" * 64,
    }
    service.data = Data(selection)
    with pytest.raises(RuleViolation) as error:
        service.create_trial(
            actor,
            project,
            definition,
            "ungranted-trial",
            {
                "candidate_id": candidate["candidate_id"],
                "task_id": task["task_id"],
                "expected_spec_revision": 1,
                "inputs": {"table_input": selection},
                "max_node_executions": 1,
                "trial_expectations": [],
            },
        )
    assert error.value.code == "SCOPE_DENIED"
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.actions)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.workflow_trials)) == 0
        )


def test_trial_insert_failure_rolls_back_admission(database, owner, monkeypatch):
    values, service, candidate = ready_candidate(database, owner)
    actor, project, definition, *_ = values
    control = ControlService(database)
    task = control.create_task(
        actor,
        project,
        "rollback-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "trial rollback", "requirements": []},
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    asset = {
        "project_id": project,
        "asset_id": "rollback-asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "b" * 64,
    }
    selection = {
        "kind": "TABLE",
        "asset": asset,
        "representation_id": "normalized",
        "representation_digest": "sha256:" + "c" * 64,
    }
    control.grant(
        actor,
        project,
        "rollback-grant",
        {
            "task_id": task["task_id"],
            "targets": [
                {"kind": "WORKFLOW", "package_ref": candidate["package_ref"]},
                profile_source()["implementations"]["profile_impl"],
            ],
            "resources": [asset],
            "derived_outputs": True,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    service.data = Data(selection)
    with database.engine.connect() as connection:
        baseline = {
            relation.name: connection.scalar(sa.select(sa.func.count()).select_from(relation))
            for relation in (
                tables.actions,
                tables.commands,
                tables.workflow_envelopes,
                tables.outbox,
            )
        }

    def fail_trial(*_args, **_kwargs):
        raise RuntimeError("injected trial insert failure")

    monkeypatch.setattr(AuthoringRepository, "add_trial", fail_trial)
    with pytest.raises(RuntimeError, match="injected trial"):
        service.create_trial(
            actor,
            project,
            definition,
            "trial-rollback",
            {
                "candidate_id": candidate["candidate_id"],
                "task_id": task["task_id"],
                "expected_spec_revision": 1,
                "inputs": {"table_input": selection},
                "max_node_executions": 1,
                "trial_expectations": [],
            },
        )
    with database.engine.connect() as connection:
        for relation in (tables.actions, tables.commands, tables.workflow_envelopes, tables.outbox):
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(relation))
                == baseline[relation.name]
            )
        budget = (
            connection.execute(
                sa.select(tables.budgets).where(tables.budgets.c.task_id == task["task_id"])
            )
            .mappings()
            .one()
        )
        assert budget["reserved"] == 0
        assert budget["in_flight"] == 0


class Facts:
    def __init__(self, action_id, execution_id, ref, representation_digest):
        self.action_id = action_id
        self.execution_id = execution_id
        self.ref = ref
        self.representation_digest = representation_digest

    def json(self, method, path, **kwargs):
        assert method == "GET"
        if "/executions/" in path:
            return {
                "state": "SUCCEEDED",
                "action_id": self.action_id,
                "execution_id": self.execution_id,
            }
        if "/publications/" in path:
            return {
                "state": "COMMITTED",
                "outputs": {"report": {"asset": self.ref, "representation_id": "report"}},
            }
        if "/assets/" in path:
            return {
                "ref": self.ref,
                "manifest": {
                    "representations": [
                        {
                            "representation_id": "report",
                            "schema_id": "table-profile-report-v1",
                            "digest": self.representation_digest,
                        }
                    ]
                },
            }
        raise AssertionError(path)


def settle_trial_facts(database, project, definition, trial_id):
    with database.transaction() as uow:
        trial = uow.authoring.trial(project, definition, trial_id)
        action = uow.actions.get(trial["action_id"])
        execution_id = "execution-a3"
        uow.actions.update(action["action_id"], execution_id=execution_id, state="RESOLVED")
        node = trial["trial_case"]["required_steps"][0]
        uow.workflows.add_node(
            {
                "node_id": node["node_id"],
                "action_id": action["action_id"],
                "request": {
                    "call_path": node["call_path"],
                    "scatter_indices": node["scatter_indices"],
                },
                "digest": "node-digest",
                "slot_acquired": False,
                "executed": True,
                "stopped": True,
                "revision": 1,
                "observation": {
                    "state": "SUCCEEDED",
                    "executed": True,
                    "stopped": True,
                    "execution_id": "node-execution",
                    "attempt_id": "node-attempt",
                },
            }
        )
    return action["action_id"], execution_id


def test_trial_verdict_and_release_are_immutable_and_do_not_recompute(database, owner):
    representation_digest = "sha256:" + "d" * 64
    actor, project, definition, service, candidate, trial = admitted_trial(
        database, owner, expectation_digest=representation_digest
    )
    action_id, execution_id = settle_trial_facts(database, project, definition, trial["trial_id"])
    ref = {
        "project_id": project,
        "asset_id": "result-asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "e" * 64,
    }
    facts = Facts(action_id, execution_id, ref, representation_digest)
    assert TrialVerdictProcessor(database, facts, facts, owner="verdict-worker").process_one()
    view = service.trial(actor, project, definition, trial["trial_id"])
    assert view["case_verdict"] == "PASS"
    assert view["case_result"]["started_steps"] == 1
    release_request = {
        "candidate_id": candidate["candidate_id"],
        "version_label": "1.0.0",
        "trial_ids": [trial["trial_id"]],
    }
    release = service.create_release(actor, project, definition, "release", release_request)
    assert release["qualification"] == "SAMPLE_VERIFIED"
    assert release["source_digest"] == candidate["source_digest"]
    assert service.create_release(actor, project, definition, "release", release_request) == release
    prerelease = service.create_release(
        actor,
        project,
        definition,
        "prerelease",
        release_request | {"version_label": "1.0.0-rc.1"},
    )
    assert service.releases(actor, project, definition, "", 10) == [prerelease, release]
    assert service.releases(actor, project, definition, "1.0.0-rc.1", 10) == [release]
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.actions)) == 1
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.workflow_releases)) == 2
        )


def test_wrong_assertion_fails_without_changing_successful_execution(database, owner):
    expected_digest = "sha256:" + "d" * 64
    actor, project, definition, service, _, trial = admitted_trial(
        database, owner, expectation_digest=expected_digest
    )
    action_id, execution_id = settle_trial_facts(database, project, definition, trial["trial_id"])
    ref = {
        "project_id": project,
        "asset_id": "result-asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "e" * 64,
    }
    facts = Facts(action_id, execution_id, ref, "sha256:" + "f" * 64)
    TrialVerdictProcessor(database, facts, facts, owner="verdict-fail").process_one()
    service.execution = facts
    view = service.trial(actor, project, definition, trial["trial_id"])
    assert view["execution_outcome"] == "SUCCEEDED"
    assert view["case_verdict"] == "FAIL"
    assert view["case_result"]["reasons"] == ["OUTPUT_ASSERTION_FAILED"]


class TemporarilyMissingAsset(Facts):
    def __init__(self, *args):
        super().__init__(*args)
        self.missing = True

    def json(self, method, path, **kwargs):
        if "/assets/" in path and self.missing:
            raise RuleViolation("RESOURCE_NOT_FOUND", "Asset projection is rebuilding", 404)
        return super().json(method, path, **kwargs)


def test_temporary_asset_404_retries_original_trial_and_generation_fences_old_worker(
    database, owner
):
    representation_digest = "sha256:" + "d" * 64
    actor, project, definition, service, _, trial = admitted_trial(
        database, owner, expectation_digest=representation_digest
    )
    action_id, execution_id = settle_trial_facts(database, project, definition, trial["trial_id"])
    ref = {
        "project_id": project,
        "asset_id": "result-asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "e" * 64,
    }
    facts = TemporarilyMissingAsset(action_id, execution_id, ref, representation_digest)
    first = TrialVerdictProcessor(database, facts, facts, owner="worker-one")
    assert first.process_one()
    view = service.trial(actor, project, definition, trial["trial_id"])
    assert view["case_verdict"] == "ERROR"
    assert view["error_category"] == "DEPENDENCY_UNAVAILABLE"
    assert view["case_result"] is None

    with database.transaction() as uow:
        uow.connection.execute(
            tables.workflow_trials.update()
            .where(tables.workflow_trials.c.trial_id == trial["trial_id"])
            .values(next_retry_at=uow.now() - timedelta(seconds=1))
        )
        stale_claim = uow.authoring.claim_trial("worker-two", 30)
        assert stale_claim["generation"] == 2
        assert not uow.authoring.finish_trial(
            trial["trial_id"],
            "worker-one",
            1,
            case_verdict="FAIL",
            case_result=None,
            case_result_digest=None,
            error_category="STALE_WORKER",
            next_retry_at=None,
            finished_at=uow.now(),
        )
        # Return the current claim to the retry queue without changing facts.
        assert uow.authoring.finish_trial(
            trial["trial_id"],
            "worker-two",
            stale_claim["generation"],
            case_verdict="ERROR",
            case_result=None,
            case_result_digest=None,
            error_category="DEPENDENCY_UNAVAILABLE",
            next_retry_at=uow.now() - timedelta(seconds=1),
            finished_at=None,
        )

    facts.missing = False
    assert TrialVerdictProcessor(database, facts, facts, owner="worker-three").process_one()
    recovered = service.trial(actor, project, definition, trial["trial_id"])
    assert recovered["case_verdict"] == "PASS"
    assert recovered["generation"] == 3
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.actions)) == 1
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.workflow_envelopes))
            == 1
        )
