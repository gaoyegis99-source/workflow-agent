import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from platform_contracts.service import ServiceFault
from platform_core.application.workflow_compiler import compile_package
from platform_core.application.workflow_plan import build_execution_plan, plan_invocation
from platform_core.domain.control import RuleViolation
from platform_core.domain.workflows import execution_id, invocation_id
from platform_execution.adapters.repository import Repository
from platform_execution.api import create_app

TARGET = {
    "kind": "OPERATOR",
    "operator_id": "fixture",
    "version_id": "1",
    "execution_binding": {"kind": "LOCAL", "implementation_id": "fixture-v1"},
}


def resolver(target):
    assert target["operator_id"] == "fixture"
    return {
        "value": "int",
        "other": {"type": "int", "default": 0},
        "enabled": {"type": "boolean", "default": True},
    }, {"value": "int", "identity": "string", "values": {"type": "array", "items": "int"}}


def freeze(request):
    compiled = compile_package(request, resolver=resolver)
    encoded = json.dumps(compiled, sort_keys=True, separators=(",", ":")).encode()
    return compiled | {"manifest_digest": "sha256:" + hashlib.sha256(encoded).hexdigest()}


def workflow(method="dotproduct", *, nested=False):
    fields = ["value"] if method == "dotproduct" else ["value", "other"]
    step = {
        "run": "fixture",
        "in": {"value": "values", "other": "others", "enabled": "enabled"},
        "out": ["value", "identity"],
        "scatter": fields if len(fields) > 1 else fields[0],
        "scatterMethod": method,
        "when": "$(inputs.enabled)",
    }
    document = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {
            "values": {"type": {"type": "array", "items": "int"}},
            "others": {"type": {"type": "array", "items": "int"}},
            "enabled": "boolean",
        },
        "outputs": {
            "ids": {
                "type": {"type": "array", "items": ["null", "string"]},
                "outputSource": "call/identity",
            }
        },
        "steps": {"call": step},
    }
    if not nested:
        return {
            "entrypoint": "main",
            "documents": {"main": document},
            "implementations": {"fixture": TARGET},
        }
    root = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {"groups": {"type": {"type": "array", "items": step_array("int")}}},
        "outputs": {
            "ids": {
                "type": step_array(step_array(["null", "string"])),
                "outputSource": "outer/ids",
            }
        },
        "steps": {
            "outer": {
                "run": "sub",
                "in": {
                    "values": "groups",
                    "others": "groups",
                    "enabled": {"default": True},
                },
                "out": ["ids"],
                "scatter": "values",
            }
        },
    }
    return {
        "entrypoint": "main",
        "documents": {"main": root, "sub": document},
        "implementations": {"fixture": TARGET},
    }


def step_array(value):
    return {"type": "array", "items": value}


def make_plan(request, inputs, *, action="action", maximum=1000):
    return build_execution_plan(
        request,
        freeze(request),
        inputs,
        action_id=action,
        max_node_executions=maximum,
    )


@pytest.mark.parametrize(
    ("method", "count"),
    [("dotproduct", 2), ("flat_crossproduct", 4), ("nested_crossproduct", 4)],
)
def test_static_scatter_plan_is_deterministic_and_value_independent(method, count):
    request = workflow(method)
    inputs = {"values": [7, 7], "others": [3, 3], "enabled": True}
    first = make_plan(request, inputs)
    second = make_plan(request, inputs)
    assert first == second
    assert len(first["nodes"]) == count
    assert len({node["node_id"] for node in first["nodes"]}) == count
    assert not first["deferred_templates"]
    assert all(node["condition"] is True for node in first["nodes"])
    assert all(node["call_path"] == ["call"] for node in first["nodes"])


def test_nested_scatter_uses_full_path_and_index_vector():
    request = workflow(nested=True)
    plan = make_plan(request, {"groups": [[7, 7], [7, 7]]})
    assert [node["call_path"] for node in plan["nodes"]] == [["outer", "call"]] * 4
    assert [node["scatter_indices"] for node in plan["nodes"]] == [
        [0, 0],
        [0, 1],
        [1, 0],
        [1, 1],
    ]


def test_condition_is_frozen_without_creating_executable_allowance():
    request = workflow()
    plan = make_plan(request, {"values": [1, 2], "others": [3, 4], "enabled": False})
    assert len(plan["nodes"]) == 2
    assert all(node["condition"] is False for node in plan["nodes"])
    assert sum(node["condition"] is not False for node in plan["nodes"]) == 0
    node = plan["nodes"][0]
    assert (
        plan_invocation(plan, node["node_id"], node["call_path"], node["scatter_indices"])[
            "condition"
        ]
        is False
    )


def test_input_package_and_implementation_changes_change_plan_digest():
    request = workflow()
    inputs = {"values": [1, 2], "others": [3, 4], "enabled": True}
    original = make_plan(request, inputs)
    changed_input = make_plan(request, inputs | {"values": [1, 9]})
    assert changed_input["plan_digest"] != original["plan_digest"]

    compiled = freeze(request)
    compiled["manifest_digest"] = "sha256:" + "f" * 64
    changed_package = build_execution_plan(
        request, compiled, inputs, action_id="action", max_node_executions=1000
    )
    assert changed_package["plan_digest"] != original["plan_digest"]

    compiled = freeze(request)
    compiled["invocations"]["call"]["target"]["execution_binding"]["implementation_id"] = (
        "fixture-v2"
    )
    changed_implementation = build_execution_plan(
        request, compiled, inputs, action_id="action", max_node_executions=1000
    )
    assert changed_implementation["plan_digest"] != original["plan_digest"]


def test_static_expansion_over_envelope_is_rejected_before_execution():
    request = workflow()
    inputs = {"values": list(range(1001)), "others": list(range(1001)), "enabled": True}
    with pytest.raises(RuleViolation) as raised:
        make_plan(request, inputs, maximum=1000)
    assert raised.value.code == "INVALID_BINDING"


def test_upstream_scatter_is_a_bounded_deferred_template():
    request = {
        "entrypoint": "main",
        "documents": {
            "main": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "inputs": {"seed": "int"},
                "outputs": {
                    "ids": {
                        "type": step_array("string"),
                        "outputSource": "fanout/identity",
                    }
                },
                "steps": {
                    "produce": {
                        "run": "fixture",
                        "in": {"value": "seed"},
                        "out": ["values"],
                    },
                    "fanout": {
                        "run": "fixture",
                        "in": {"value": "produce/values"},
                        "out": ["identity"],
                        "scatter": "value",
                    },
                },
            }
        },
        "implementations": {"fixture": TARGET},
    }
    plan = make_plan(request, {"seed": 1}, maximum=5)
    assert [node["call_path"] for node in plan["nodes"]] == [["produce"]]
    assert len(plan["deferred_templates"]) == 1
    template = plan["deferred_templates"][0]
    assert template["call_path"] == ["fanout"]
    assert template["index_depth"] == 1
    node = invocation_id(execution_id("action"), ["fanout"], [4])
    assert plan_invocation(plan, node, ["fanout"], [4]) == template
    assert plan_invocation(plan, node, ["fanout"], [4, 5]) is None


def execution_request(action="workflow_action"):
    request = workflow()
    compiled = freeze(request)
    inputs = {"values": [1, 2], "others": [3, 4], "enabled": True}
    plan = build_execution_plan(
        request,
        compiled,
        inputs,
        action_id=action,
        max_node_executions=2,
    )
    return {
        "project_id": "project",
        "task_id": "task",
        "expected_spec_revision": 1,
        "kind": "WORKFLOW",
        "target": {
            "kind": "WORKFLOW",
            "package_ref": {
                "package_id": "package",
                "version_id": "1",
                "manifest_digest": compiled["manifest_digest"],
            },
        },
        "compiled": compiled,
        "execution_plan": plan,
        "inputs": inputs,
        "resources": [],
        "leaf_targets": [TARGET],
        "max_node_executions": 2,
    }


def test_execution_accepts_and_persists_exact_plan(service_databases):
    database = service_databases[1]
    action = "workflow_action"
    body = execution_request(action)
    token = "platform-" + "x" * 32
    with TestClient(create_app(database, token)) as client:
        response = client.post(
            "/internal/actions/" + action,
            json=body,
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 202, response.text
    stored = Repository(database).get(action=action)
    assert stored["payload"]["execution_plan"] == body["execution_plan"]


def test_platform_and_execution_derive_the_same_parent_identity():
    assert execution_id("workflow_action") == Repository.identity("workflow_action")


def test_execution_still_accepts_legacy_workflow_without_plan(service_databases):
    database = service_databases[1]
    action = "legacy_workflow"
    body = execution_request(action)
    body.pop("execution_plan")
    token = "platform-" + "x" * 32
    with TestClient(create_app(database, token)) as client:
        response = client.post(
            "/internal/actions/" + action,
            json=body,
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 202, response.text
    assert "execution_plan" not in Repository(database).get(action=action)["payload"]


def test_execution_rejects_tampered_plan_before_persisting(service_databases):
    database = service_databases[1]
    action = "tampered_workflow"
    body = execution_request(action)
    body["execution_plan"]["nodes"][0]["scatter_indices"] = [999]
    token = "platform-" + "x" * 32
    with TestClient(create_app(database, token)) as client:
        response = client.post(
            "/internal/actions/" + action,
            json=body,
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_BINDING"
    with pytest.raises(ServiceFault):
        Repository(database).get(action=action)
