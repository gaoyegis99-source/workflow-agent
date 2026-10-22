from copy import deepcopy

import pytest
from platform_core.application.control import ControlService
from platform_core.application.science import Science
from platform_core.domain.control import RuleViolation
from platform_infrastructure import tables

from .test_contracts import EXPECTED

DIGEST = "sha256:" + "1" * 64
REP_DIGEST = "sha256:" + "2" * 64


class External:
    def __init__(self, values):
        self.values = values

    def json(self, method, path, **_kwargs):
        return deepcopy(self.values[path])


def setup(database, owner, *, workflow=False):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "project", "R1")["project_id"]
    spec = {
        "profile": "open-science-v1",
        "delivery_class": "VERIFIED",
        "requirements": [],
        "required_outputs": [{"role_id": "trend_result", "necessary": True, "expected": EXPECTED}],
        "allow_manual_review": False,
        "max_check_executions": 1,
    }
    task = control.create_task(
        actor,
        project,
        "task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Exact output", "requirements": [], "acceptance_spec": spec},
            "budget": {"execution_count": 2, "max_in_flight": 1},
        },
    )
    task_id = task["task_id"]
    execution_id = "parent-execution" if workflow else "execution"
    action_id = "parent-action" if workflow else "action"
    command_id = "parent-command" if workflow else "command"
    with database.engine.begin() as connection:
        connection.execute(
            tables.commands.insert().values(
                command_id=command_id,
                project_id=project,
                task_id=task_id,
                spec_revision=1,
                status="RESOLVED",
                action_id=action_id,
                execution_id=execution_id,
                error_code=None,
                error_message=None,
            )
        )
        connection.execute(
            tables.actions.insert().values(
                action_id=action_id,
                command_id=command_id,
                task_id=task_id,
                project_id=project,
                spec_revision=1,
                grant_epoch=1,
                principal_id=actor.principal_id,
                token_id=actor.token_id,
                frame_id="frame-" + action_id,
                decision_source="CLIENT",
                request={"target": {"kind": "WORKFLOW" if workflow else "OPERATOR"}},
                send_started=True,
                state="SUCCEEDED",
                external_revision=1,
                execution_id=execution_id,
                stopped=True,
                cancel_requested=False,
                failure_count=0,
                last_external_observation=None,
            )
        )
        if workflow:
            connection.execute(
                tables.workflow_envelopes.insert().values(
                    action_id=action_id, amount=1, released=True
                )
            )
            connection.execute(
                tables.workflow_nodes.insert().values(
                    node_id="node-action",
                    action_id=action_id,
                    request={
                        "call_path": ["trend"],
                        "scatter_indices": [2],
                        "inputs": {},
                        "engine_job_id": "job",
                        "epoch": 1,
                    },
                    digest="node-digest",
                    slot_acquired=True,
                    executed=True,
                    stopped=True,
                    revision=1,
                    observation={"execution_id": "node-execution", "state": "SUCCEEDED"},
                )
            )
    ref = {
        "project_id": project,
        "asset_id": "asset",
        "version_id": "1",
        "version_manifest_digest": DIGEST,
    }
    manifest = {
        "kind": "OPERATOR_RESULT",
        "representations": [
            {
                "representation_id": "result",
                "format": "json",
                "digest": REP_DIGEST,
                "size": 12,
                "schema_id": "p3-result-v1",
            }
        ],
        "source": {
            "execution_id": "node-execution" if workflow else execution_id,
            "task_ref": {"task_id": task_id, "spec_revision": 1},
            "target": {
                "kind": "OPERATOR",
                "operator_id": "statistics-linear-trend",
                "version_id": "1.0.0",
            },
        },
    }
    if workflow:
        manifest["workflow"] = {"execution_id": execution_id, "member_id": "node-action"}
        output_ref = {
            "kind": "WORKFLOW_OUTPUT",
            "parent_action_id": action_id,
            "parent_execution_id": execution_id,
            "node_action_id": "node-action",
            "node_execution_id": "node-execution",
            "call_path": ["trend"],
            "scatter_path": [2],
            "published_output_port": "results",
            "published_path": [0],
            "output_port": "result",
            "asset_ref": ref,
            "representation_id": "result",
            "representation_digest": REP_DIGEST,
            "schema_id": "p3-result-v1",
            "schema_source": "MANIFEST",
        }
        receipt = {"outputs": {"results": [{"asset": ref, "representation_id": "result"}]}}
    else:
        output_ref = {
            "kind": "OPERATOR_OUTPUT",
            "action_id": action_id,
            "execution_id": execution_id,
            "output_port": "result",
            "asset_ref": ref,
            "representation_id": "result",
            "representation_digest": REP_DIGEST,
            "schema_id": "p3-result-v1",
            "schema_source": "MANIFEST",
        }
        receipt = {"asset": ref}
    execution = External(
        {
            f"/internal/projects/{project}/executions/{execution_id}": {
                "task_id": task_id,
                "state": "SUCCEEDED",
                "runtime": {"publication": "COMMITTED", "publication_receipt": receipt},
            }
        }
    )
    data = External(
        {
            f"/internal/projects/{project}/assets/asset/versions/1": {
                "ref": ref,
                "manifest": manifest,
            }
        }
    )
    return actor, project, task_id, Science(database, data, execution), output_ref


def request(output_ref):
    return {
        "expected_spec_revision": 1,
        "executions": [],
        "workflow_outputs": [],
        "role_bindings": [{"role_id": "trend_result", "output_ref": output_ref}],
    }


def test_direct_output_is_canonicalized_and_saved_as_exact(database, owner):
    actor, project, task, science, output_ref = setup(database, owner)
    value = science.candidate(actor, project, task, "candidate", request(output_ref))
    assert value["identity_mode"] == "EXACT"
    assert value["payload"]["role_bindings"] == [
        {"role_id": "trend_result", "output_ref": output_ref}
    ]


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("action_id", "wrong-action"),
        ("execution_id", "wrong-execution"),
        ("output_port", "table"),
        ("representation_id", "table"),
        ("representation_digest", "sha256:" + "9" * 64),
        ("schema_id", "wrong-schema"),
    ],
)
def test_direct_output_rejects_each_changed_identity(database, owner, field, wrong):
    actor, project, task, science, output_ref = setup(database, owner)
    output_ref[field] = wrong
    with pytest.raises(RuleViolation) as error:
        science.candidate(actor, project, task, "candidate", request(output_ref))
    assert error.value.code in {"INVALID_BINDING", "RESOURCE_NOT_FOUND"}


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("parent_action_id", "wrong-parent"),
        ("node_action_id", "wrong-node"),
        ("node_execution_id", "wrong-execution"),
        ("call_path", ["other"]),
        ("scatter_path", [1]),
        ("published_path", [1]),
    ],
)
def test_workflow_output_rejects_changed_node_or_array_identity(database, owner, field, wrong):
    actor, project, task, science, output_ref = setup(database, owner, workflow=True)
    output_ref[field] = wrong
    with pytest.raises(RuleViolation) as error:
        science.candidate(actor, project, task, "candidate", request(output_ref))
    assert error.value.code == "INVALID_BINDING"


def test_workflow_output_preserves_call_and_scatter_identity(database, owner):
    actor, project, task, science, output_ref = setup(database, owner, workflow=True)
    value = science.candidate(actor, project, task, "candidate", request(output_ref))
    saved = value["payload"]["role_bindings"][0]["output_ref"]
    assert saved["call_path"] == ["trend"]
    assert saved["scatter_path"] == [2]
    assert saved["published_path"] == [0]
