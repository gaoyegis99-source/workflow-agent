from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_contracts.authoring import WorkflowReleaseExecutionCreate
from platform_core.application.authoring_release import TrialVerdictProcessor
from platform_core.application.control import ControlService
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables

from tests.authoring.a2.helpers import profile_source
from tests.authoring.a3.test_candidate import (
    Data,
    Facts,
    Validator,
    admitted_trial,
    settle_trial_facts,
)


def published_release(database, owner):
    digest = "sha256:" + "d" * 64
    actor, project, definition, service, candidate, trial = admitted_trial(
        database, owner, expectation_digest=digest
    )
    action_id, execution_id = settle_trial_facts(database, project, definition, trial["trial_id"])
    ref = {
        "project_id": project,
        "asset_id": "result-asset",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "e" * 64,
    }
    facts = Facts(action_id, execution_id, ref, digest)
    assert TrialVerdictProcessor(database, facts, facts, owner="a6-verdict").process_one()
    release = service.create_release(
        actor,
        project,
        definition,
        "a6-release",
        {
            "candidate_id": candidate["candidate_id"],
            "version_label": "1.0.0",
            "trial_ids": [trial["trial_id"]],
        },
    )
    return actor, project, definition, service, release


def execution_request(database, actor, project, release, *, grant=True):
    control = ControlService(database)
    task = control.create_task(
        actor,
        project,
        "a6-run-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Run a released workflow", "requirements": []},
            "budget": {"execution_count": 2, "max_in_flight": 1},
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
    if grant:
        control.grant(
            actor,
            project,
            "a6-run-grant",
            {
                "task_id": task["task_id"],
                "targets": [
                    {"kind": "WORKFLOW", "package_ref": release["package_ref"]},
                    profile_source()["implementations"]["profile_impl"],
                ],
                "resources": [asset],
                "derived_outputs": True,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    return (
        task,
        selection,
        {
            "task_id": task["task_id"],
            "expected_spec_revision": 1,
            "inputs": {"table_input": selection},
            "max_node_executions": 1,
        },
    )


def test_release_execution_contract_does_not_accept_client_target():
    value = WorkflowReleaseExecutionCreate.model_validate(
        {
            "task_id": "task",
            "expected_spec_revision": 1,
            "inputs": {},
            "max_node_executions": 1,
        }
    )
    assert value.max_node_executions == 1
    with pytest.raises(ValueError):
        WorkflowReleaseExecutionCreate.model_validate(
            {
                "task_id": "task",
                "expected_spec_revision": 1,
                "inputs": {},
                "max_node_executions": 1,
                "target": {
                    "kind": "WORKFLOW",
                    "package_ref": {
                        "package_id": "forged",
                        "version_id": "1",
                        "manifest_digest": "sha256:" + "f" * 64,
                    },
                },
            }
        )


def test_project_library_is_exact_paginated_and_project_isolated(database, owner):
    actor, project, definition, service, release = published_release(database, owner)
    second = service.create_release(
        actor,
        project,
        definition,
        "a6-release-2",
        {
            "candidate_id": release["candidate_id"],
            "version_label": "1.0.1",
            "trial_ids": release["trial_ids"],
        },
    )
    first_page = service.project_releases(actor, project, "", 1)
    assert len(first_page) == 1
    second_page = service.project_releases(actor, project, first_page[0]["release_id"], 2)
    assert [*first_page, *second_page] == [release, second]
    assert release["stable_key"] == "a3-flow"
    assert release["metadata"]["title"] == "A3"

    other = ControlService(database).create_project(actor, "a6-other", "Other")
    assert service.project_releases(actor, other["project_id"], "", 10) == []
    with pytest.raises(RuleViolation) as missing:
        service.release(actor, other["project_id"], definition, "1.0.0")
    assert missing.value.code == "RESOURCE_NOT_FOUND"


def test_release_execution_freezes_identity_and_replays_before_dependencies(database, owner):
    actor, project, definition, service, release = published_release(database, owner)
    task, selection, request = execution_request(database, actor, project, release)
    service.data = Data(selection)
    command = service.execute_release(
        actor, project, definition, release["version_label"], "a6-run", request
    )
    assert command["action_id"]
    with database.transaction() as uow:
        action = uow.actions.get(command["action_id"])
        frozen = action["request"]["_workflow_release"]
        assert frozen == {
            "release_id": release["release_id"],
            "definition_id": definition,
            "stable_key": "a3-flow",
            "version_label": "1.0.0",
            "candidate_id": release["candidate_id"],
            "revision": release["revision"],
            "source_digest": release["source_digest"],
            "package_ref": release["package_ref"],
            "context_digest": release["context_digest"],
            "lifecycle": "ACTIVE",
            "lifecycle_revision": 1,
            "qualification": "SAMPLE_VERIFIED",
        }
        assert action["request"]["target"] == {
            "kind": "WORKFLOW",
            "package_ref": release["package_ref"],
        }
        assert uow.budgets.get(task["task_id"]).reserved == 1

    service.data = None
    service.environment = None
    assert (
        service.execute_release(
            actor, project, definition, release["version_label"], "a6-run", request
        )
        == command
    )
    with pytest.raises(RuleViolation) as conflict:
        service.execute_release(
            actor,
            project,
            definition,
            release["version_label"],
            "a6-run",
            request | {"max_node_executions": 2},
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(tables.actions)
                .where(tables.actions.c.task_id == task["task_id"])
            )
            == 1
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.workflow_trials)) == 1
        )


def test_release_execution_without_grant_leaves_no_action_or_reservation(database, owner):
    actor, project, definition, service, release = published_release(database, owner)
    task, selection, request = execution_request(database, actor, project, release, grant=False)
    service.data = Data(selection)
    with pytest.raises(RuleViolation) as denied:
        service.execute_release(
            actor, project, definition, release["version_label"], "a6-ungranted", request
        )
    assert denied.value.code == "SCOPE_DENIED"
    with database.transaction() as uow:
        assert not uow.actions.list(task["task_id"])
        budget = uow.budgets.get(task["task_id"])
        assert budget.reserved == 0
        assert budget.in_flight == 0


def test_release_library_and_execution_http_routes(database, owner, monkeypatch):
    actor, project, definition, service, release = published_release(database, owner)
    _, token = owner
    task, selection, body = execution_request(database, actor, project, release)
    monkeypatch.setenv("V7_CWL_IMAGE", Validator.image_id)
    with TestClient(create_app(database)) as client:
        client.app.state.data = Data(selection)
        headers = {
            "Authorization": "Bearer " + token,
            "Idempotency-Key": "a6-http-run",
        }
        library = client.get(
            f"/api/v1/projects/{project}/workflow-releases",
            headers={"Authorization": "Bearer " + token},
        )
        assert library.status_code == 200, library.text
        assert library.json()["items"][0]["release_id"] == release["release_id"]
        response = client.post(
            f"/api/v1/projects/{project}/workflow-definitions/{definition}/releases/"
            f"{release['version_label']}/executions",
            headers=headers,
            json=body,
        )
        assert response.status_code == 202, response.text
        assert response.json()["action_id"]
        paths = client.get("/api/v1/openapi.json").json()["paths"]
        assert "/api/v1/projects/{project_id}/workflow-releases" in paths
        assert (
            "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/releases/"
            "{version_label}/executions" in paths
        )
