from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_contracts.authoring import WorkflowReleaseLifecycleCreate
from platform_core.application.science import Science
from platform_core.application.workflows import Workflows
from platform_core.domain.control import RuleViolation
from platform_core.domain.workflows import execution_id
from platform_infrastructure import database as database_module
from platform_infrastructure import tables

from tests.authoring.a6.test_release_execution import (
    Data,
    execution_request,
    published_release,
)
from tests.p4.test_node_budget import dispatch_root


def transition(service, actor, project, definition, release, key, state, revision, reason=None):
    return service.transition_release(
        actor,
        project,
        definition,
        release["version_label"],
        key,
        {
            "expected_lifecycle_revision": revision,
            "lifecycle": state,
            "reason": reason or state.lower() + " for A7 verification",
        },
    )


def admitted_release_action(database, owner):
    actor, project, definition, service, release = published_release(database, owner)
    task, selection, request = execution_request(database, actor, project, release)
    service.data = Data(selection)
    command_receipt = service.execute_release(
        actor, project, definition, release["version_label"], "a7-run", request
    )
    return actor, project, definition, service, release, task, request, command_receipt


def test_lifecycle_contract_rejects_meaningless_reason():
    with pytest.raises(ValueError):
        WorkflowReleaseLifecycleCreate.model_validate(
            {
                "expected_lifecycle_revision": 1,
                "lifecycle": "DISABLED",
                "reason": "   ",
            }
        )


def test_disable_reenable_and_revoke_are_auditable(database, owner):
    actor, project, definition, service, release = published_release(database, owner)
    assert release["lifecycle"] == "ACTIVE"
    disabled = transition(service, actor, project, definition, release, "disable", "DISABLED", 1)
    assert disabled["previous_lifecycle"] == "ACTIVE"
    assert disabled["lifecycle_revision"] == 2
    view = service.release(actor, project, definition, "1.0.0")
    assert not view["current_executable"]
    assert "RELEASE_DISABLED" in view["unavailable_reasons"]

    with pytest.raises(RuleViolation) as stale:
        transition(service, actor, project, definition, release, "stale", "ACTIVE", 1)
    assert stale.value.code == "VERSION_CONFLICT"

    enabled = transition(service, actor, project, definition, release, "enable", "ACTIVE", 2)
    assert enabled["lifecycle_revision"] == 3
    revoked = transition(service, actor, project, definition, release, "revoke", "REVOKED", 3)
    assert revoked["lifecycle_revision"] == 4
    with pytest.raises(RuleViolation) as terminal:
        transition(service, actor, project, definition, release, "revive", "ACTIVE", 4)
    assert terminal.value.code == "INVALID_STATE"

    history = service.release_lifecycle(actor, project, definition, "1.0.0", 0, 10)
    assert [item["lifecycle"] for item in history] == [
        "ACTIVE",
        "DISABLED",
        "ACTIVE",
        "REVOKED",
    ]


def test_only_owner_can_change_release_lifecycle(database, owner):
    actor, project, definition, service, release = published_release(database, owner)
    principal = database.create_principal("release-editor")
    token = database.issue_token(principal)
    database.set_member(project, principal, "EDITOR", actor=actor)
    editor = database.authenticate(token["token"])
    with pytest.raises(RuleViolation) as denied:
        transition(service, editor, project, definition, release, "editor-disable", "DISABLED", 1)
    assert denied.value.code == "SCOPE_DENIED"


def test_concurrent_lifecycle_revision_has_one_winner(database, owner):
    actor, project, definition, service, release = published_release(database, owner)

    def attempt(index, state):
        try:
            return transition(
                service,
                actor,
                project,
                definition,
                release,
                f"concurrent-{index}",
                state,
                1,
            )
        except RuleViolation as problem:
            return problem.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: attempt(*value), [(1, "DISABLED"), (2, "REVOKED")]))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert sum(value == "VERSION_CONFLICT" for value in results) == 1
    history = service.release_lifecycle(actor, project, definition, "1.0.0", 0, 10)
    assert len(history) == 2 and history[-1]["lifecycle_revision"] == 2


def test_disabled_blocks_first_admission_but_original_replay_survives(database, owner):
    actor, project, definition, service, release, task, request, receipt = admitted_release_action(
        database, owner
    )
    transition(service, actor, project, definition, release, "disable", "DISABLED", 1)
    assert (
        service.execute_release(actor, project, definition, "1.0.0", "a7-run", request) == receipt
    )
    with pytest.raises(RuleViolation) as denied:
        service.execute_release(actor, project, definition, "1.0.0", "a7-new-run", request)
    assert denied.value.code == "PRECONDITION_UNRESOLVED"
    with database.engine.connect() as connection:
        count = connection.scalar(
            sa.select(sa.func.count())
            .select_from(tables.actions)
            .where(tables.actions.c.task_id == task["task_id"])
        )
    assert count == 1


def test_revoke_before_node_permission_prevents_start(database, owner):
    actor, project, definition, service, release, task, _, receipt = admitted_release_action(
        database, owner
    )
    root = execution_id(receipt["action_id"])
    dispatch_root(database, receipt["action_id"], root)
    transition(service, actor, project, definition, release, "revoke", "REVOKED", 1)
    with database.transaction() as uow:
        action = uow.actions.get(receipt["action_id"])
        planned = next(
            item for item in action["request"]["execution_plan"]["nodes"] if item["condition"]
        )
    request = {
        "call_path": planned["call_path"],
        "scatter_indices": planned["scatter_indices"],
        "inputs": {},
        "epoch": 1,
    }
    node_id = planned["node_id"]
    result = Workflows(database).permit_node(receipt["action_id"], root, node_id, request)
    assert result == {
        "decision": "DENY",
        "reportable": False,
        "reason": "RELEASE_REVOKED",
    }
    budget = service.control.budget(actor, project, task["task_id"])
    assert budget["used"] == 0 and budget["in_flight"] == 0


def test_disabled_release_allows_already_accepted_action_to_continue(database, owner):
    actor, project, definition, service, release, _, _, receipt = admitted_release_action(
        database, owner
    )
    root = execution_id(receipt["action_id"])
    dispatch_root(database, receipt["action_id"], root)
    transition(service, actor, project, definition, release, "disable", "DISABLED", 1)
    with database.transaction() as uow:
        action = uow.actions.get(receipt["action_id"])
        planned = next(
            item for item in action["request"]["execution_plan"]["nodes"] if item["condition"]
        )
    result = Workflows(database).permit_node(
        receipt["action_id"],
        root,
        planned["node_id"],
        {
            "call_path": planned["call_path"],
            "scatter_indices": planned["scatter_indices"],
            "inputs": {},
            "epoch": 1,
        },
    )
    assert result["decision"] == "ALLOW"
    assert Science(database, None, None).permit_publication(receipt["action_id"])["decision"] == (
        "ALLOW"
    )


def test_node_permit_replay_survives_revocation_after_permit(database, owner):
    actor, project, definition, service, release, _, _, receipt = admitted_release_action(
        database, owner
    )
    root = execution_id(receipt["action_id"])
    dispatch_root(database, receipt["action_id"], root)
    with database.transaction() as uow:
        planned = next(
            item
            for item in uow.actions.get(receipt["action_id"])["request"]["execution_plan"]["nodes"]
            if item["condition"]
        )
    request = {
        "call_path": planned["call_path"],
        "scatter_indices": planned["scatter_indices"],
        "inputs": {},
        "epoch": 1,
    }
    workflows = Workflows(database)
    assert (
        workflows.permit_node(receipt["action_id"], root, planned["node_id"], request)["decision"]
        == "ALLOW"
    )
    transition(service, actor, project, definition, release, "revoke", "REVOKED", 1)
    replay = workflows.permit_node(receipt["action_id"], root, planned["node_id"], request)
    assert replay["decision"] == "ALLOW"


def test_publication_permit_and_revocation_have_deterministic_order(database, owner):
    actor, project, definition, service, release, _, _, receipt = admitted_release_action(
        database, owner
    )
    science = Science(database, None, None)
    first = science.permit_publication(receipt["action_id"])
    assert first["decision"] == "ALLOW"
    transition(service, actor, project, definition, release, "revoke", "REVOKED", 1)
    assert science.permit_publication(receipt["action_id"]) == first
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(tables.workflow_release_publication_permits)
            )
            == 1
        )


def test_revocation_before_publication_permit_is_final(database, owner):
    actor, project, definition, service, release, _, _, receipt = admitted_release_action(
        database, owner
    )
    transition(service, actor, project, definition, release, "revoke", "REVOKED", 1)
    first = Science(database, None, None).permit_publication(receipt["action_id"])
    assert first["decision"] == "DENY" and first["reason"] == "RELEASE_REVOKED"
    assert Science(database, None, None).permit_publication(receipt["action_id"]) == first


def test_lifecycle_http_and_openapi(database, owner):
    actor, project, definition, _, release = published_release(database, owner)
    _, token = owner
    path = (
        f"/api/v1/projects/{project}/workflow-definitions/{definition}/releases/"
        f"{release['version_label']}/lifecycle-revisions"
    )
    with TestClient(create_app(database)) as client:
        response = client.post(
            path,
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": "http-disable"},
            json={
                "expected_lifecycle_revision": 1,
                "lifecycle": "DISABLED",
                "reason": "Temporarily stop new executions",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["lifecycle"] == "DISABLED"
        history = client.get(path, headers={"Authorization": "Bearer " + token})
        assert history.status_code == 200
        assert [item["lifecycle"] for item in history.json()["items"]] == [
            "ACTIVE",
            "DISABLED",
        ]
        assert (
            "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/releases/"
            "{version_label}/lifecycle-revisions"
            in client.get("/api/v1/openapi.json").json()["paths"]
        )


def test_upgrade_from_0022_backfills_active_history(database, owner):
    actor, project, definition, service, release = published_release(database, owner)
    with database.engine.begin() as connection:
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(database_module.__file__).parent / "migrations"),
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0022_operator_proposals")
    database.migrate()
    database.check_schema()
    view = service.release(actor, project, definition, release["version_label"])
    assert view["lifecycle"] == "ACTIVE" and view["lifecycle_revision"] == 1
    history = service.release_lifecycle(actor, project, definition, "1.0.0", 0, 10)
    assert len(history) == 1 and history[0]["reason"] == "INITIAL_RELEASE"
