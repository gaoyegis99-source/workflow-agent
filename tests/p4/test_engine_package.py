import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from platform_core.application.workflow_compiler import compile_package
from platform_core.application.workflow_engine import (
    build_engine_package,
    validate_engine_package,
)
from platform_core.application.workflow_plan import build_execution_plan
from platform_core.domain.control import RuleViolation
from platform_execution.adapters.repository import Repository
from platform_execution.api import create_app

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "experiments/p1/toil/.venv/bin"
BRIDGE = [sys.executable, str(Path(__file__).with_name("fixture_bridge.py"))]
TARGET = {
    "kind": "OPERATOR",
    "operator_id": "fixture",
    "version_id": "1",
    "execution_binding": {"kind": "LOCAL", "implementation_id": "fixture-v1"},
}


def resolver(target):
    assert target == TARGET
    return {
        "value": "int",
        "other": {"type": "int", "default": 0},
        "enabled": {"type": "boolean", "default": True},
    }, {
        "value": "int",
        "identity": "string",
        "values": ["null", {"type": "array", "items": "int"}],
    }


def array(items):
    return {"type": "array", "items": items}


def package(method="dotproduct", nested=False):
    scatter = "value" if method == "dotproduct" else ["value", "other"]
    item = ["null", "string"]
    if method == "nested_crossproduct":
        item = array(item)
    bindings = {"value": "values", "enabled": "enabled"}
    if method != "dotproduct":
        bindings["other"] = "others"
    sub = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {
            "values": {"type": array("int")},
            "others": {"type": array("int")},
            "enabled": "boolean",
        },
        "outputs": {"ids": {"type": array(item), "outputSource": "call/identity"}},
        "steps": {
            "call": {
                "run": "fixture",
                "in": bindings,
                "out": ["value", "identity"],
                "scatter": scatter,
                "scatterMethod": method,
                "when": "$(inputs.enabled)",
            }
        },
    }
    if not nested:
        return {
            "entrypoint": "main",
            "documents": {"main": sub},
            "implementations": {"fixture": TARGET},
        }
    root = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {"groups": {"type": array(array("int"))}},
        "outputs": {"ids": {"type": array(array(["null", "string"])), "outputSource": "outer/ids"}},
        "steps": {
            "outer": {
                "run": "sub",
                "in": {
                    "values": "groups",
                    "others": {"default": [0, 0]},
                    "enabled": {"default": True},
                },
                "out": ["ids"],
                "scatter": "values",
            }
        },
    }
    return {
        "entrypoint": "main",
        "documents": {"main": root, "sub": sub},
        "implementations": {"fixture": TARGET},
    }


def frozen(request):
    compiled = compile_package(request, resolver=resolver, bridge_command=BRIDGE)
    encoded = json.dumps(compiled, sort_keys=True, separators=(",", ":")).encode()
    return compiled | {"manifest_digest": "sha256:" + hashlib.sha256(encoded).hexdigest()}


def engine(request, inputs, *, action="action", maximum=1000):
    compiled = frozen(request)
    plan = build_execution_plan(
        request, compiled, inputs, action_id=action, max_node_executions=maximum
    )
    return (
        build_engine_package(request, compiled, plan, inputs, bridge_command=BRIDGE),
        compiled,
        plan,
    )


def run_toil(tmp_path, engine_package, inputs):
    (tmp_path / "work").mkdir()
    for name, value in engine_package["files"].items():
        (tmp_path / name).write_text(json.dumps(value))
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            inputs | ({"v7_context": "[]"} if engine_package["strategy"] == "legacy_v1" else {})
        )
    )
    command = [
        str(BIN / "toil-cwl-runner"),
        "--jobStore",
        str(tmp_path / "store"),
        "--workDir",
        str(tmp_path / "work"),
        "--no-container",
        "--retryCount",
        "0",
        "--maxCores",
        "2",
        "--maxLocalJobs",
        "10",
        "--maxJobs",
        "10",
        "--clean",
        "never",
        "--outdir",
        str(tmp_path / "out"),
        "--publishWorkflowMetrics",
        "no",
        str(tmp_path / engine_package["entrypoint"]),
        str(job),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        env=os.environ | {"PATH": str(BIN) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr[-12000:]
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("method", "expected"),
    [("dotproduct", 2), ("flat_crossproduct", 4), ("nested_crossproduct", 4)],
)
def test_flat_package_has_one_direct_step_per_node_and_one_gather(method, expected):
    request = package(method)
    value, compiled, plan = engine(request, {"values": [7, 7], "others": [3, 3], "enabled": True})
    assert value["strategy"] == "flat_v1"
    assert len(value["node_map"]) == expected
    root = value["files"][value["entrypoint"]]
    assert set(root["steps"]) == set(value["node_map"]) | {"v7_gather"}
    assert not any("scatter" in step for step in root["steps"].values())
    assert not any(name.startswith("v7_tag_") for name in root["steps"])
    assert validate_engine_package(value, compiled, plan) == value


def test_known_false_conditions_create_no_compute_steps_and_preserve_nulls(tmp_path):
    request = package()
    value, _, plan = engine(request, {"values": [1, 2], "others": [3, 4], "enabled": False})
    assert len(plan["nodes"]) == 2
    assert not value["node_map"]
    assert set(value["files"]["workflow.cwl"]["steps"]) == {"v7_gather"}
    assert run_toil(tmp_path, value, {"values": [1, 2], "others": [3, 4], "enabled": False}) == {
        "ids": [None, None]
    }


def test_empty_scatter_preserves_empty_output_without_compute(tmp_path):
    request = package()
    inputs = {"values": [], "others": [], "enabled": True}
    value, _, plan = engine(request, inputs)
    assert plan["nodes"] == []
    assert value["node_map"] == {}
    assert run_toil(tmp_path, value, inputs) == {"ids": []}


def test_nested_duplicate_values_keep_full_identity_and_shape(tmp_path):
    request = package(nested=True)
    inputs = {"groups": [[7, 7], [7, 7]]}
    value, _, _ = engine(request, inputs)
    result = run_toil(tmp_path, value, inputs)
    identities = [json.loads(item) for row in result["ids"] for item in row]
    assert [item[0] for item in identities] == [["outer", "call"]] * 4
    assert [item[1] for item in identities] == [[0, 0], [0, 1], [1, 0], [1, 1]]


def test_static_package_is_deterministic_and_tampering_is_rejected():
    request = package("flat_crossproduct")
    inputs = {"values": [7, 7], "others": [3, 3], "enabled": True}
    first, compiled, plan = engine(request, inputs)
    second, _, _ = engine(request, inputs)
    assert first == second
    changed, _, _ = engine(request, inputs, action="another")
    assert changed["package_digest"] != first["package_digest"]
    first["node_map"].pop(next(iter(first["node_map"])))
    with pytest.raises(RuleViolation) as raised:
        validate_engine_package(first, compiled, plan)
    assert raised.value.code == "INVALID_BINDING"


def test_sequence_uses_direct_node_output_edge(tmp_path):
    request = package()
    request["documents"]["main"] = {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {"seed": "int"},
        "outputs": {"answer": {"type": "int", "outputSource": "second/value"}},
        "steps": {
            "first": {"run": "fixture", "in": {"value": "seed"}, "out": ["value"]},
            "second": {
                "run": "fixture",
                "in": {"value": "first/value", "other": {"default": 1}},
                "out": ["value"],
            },
        },
    }
    value, _, _ = engine(request, {"seed": 4}, maximum=2)
    root = value["files"]["workflow.cwl"]
    assert len(value["node_map"]) == 2
    assert run_toil(tmp_path, value, {"seed": 4}) == {"answer": 5}
    assert all("scatter" not in step for step in root["steps"].values())


def test_flat_package_supports_exactly_one_thousand_static_nodes():
    request = package()
    inputs = {
        "values": list(range(1000)),
        "others": list(range(1000)),
        "enabled": True,
    }
    value, _, plan = engine(request, inputs, maximum=1000)
    assert len(plan["nodes"]) == len(value["node_map"]) == 1000
    assert len(json.dumps(value).encode()) <= 8 * 1024 * 1024


def test_upstream_dynamic_scatter_falls_back_as_one_legacy_execution():
    request = package()
    main = request["documents"]["main"]
    main["inputs"] = {"seed": "int", "enabled": "boolean"}
    main["steps"] = {
        "produce": {"run": "fixture", "in": {"value": "seed"}, "out": ["values"]},
        "call": {
            "run": "fixture",
            "in": {"value": "produce/values", "enabled": "enabled"},
            "out": ["identity"],
            "scatter": "value",
        },
    }
    value, compiled, plan = engine(request, {"seed": 1, "enabled": True}, maximum=10)
    assert plan["deferred_templates"]
    assert value["strategy"] == "legacy_v1"
    assert value["files"] == compiled["files"]


def test_engine_package_rejects_unsafe_members_and_wrong_digest():
    request = package()
    value, compiled, plan = engine(request, {"values": [1], "others": [2], "enabled": True})
    value["files"]["../escape.cwl"] = {}
    with pytest.raises(RuleViolation):
        validate_engine_package(value, compiled, plan)


def execution_body(action="flat_action"):
    request = package()
    inputs = {"values": [1], "others": [2], "enabled": True}
    value, compiled, plan = engine(request, inputs, action=action)
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
        "engine_package": value,
        "inputs": inputs,
        "resources": [],
        "leaf_targets": [TARGET],
        "max_node_executions": 1,
    }


def test_execution_accepts_exact_engine_package_and_rejects_tampering(service_databases):
    database = service_databases[1]
    token = "platform-" + "x" * 32
    accepted = execution_body()
    with TestClient(create_app(database, token)) as client:
        response = client.post(
            "/internal/actions/flat_action",
            json=accepted,
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 202, response.text
        tampered = execution_body("bad_action")
        tampered["engine_package"]["files"]["workflow.cwl"]["steps"].pop("v7_gather")
        response = client.post(
            "/internal/actions/bad_action",
            json=tampered,
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_BINDING"
    assert (
        Repository(database).get(action="flat_action")["payload"]["engine_package"]
        == accepted["engine_package"]
    )
