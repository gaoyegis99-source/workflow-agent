import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from platform_core.application.workflow_compiler import compile_package
from platform_core.domain.control import RuleViolation

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "experiments/p1/toil/.venv/bin"
TARGET = {
    "kind": "OPERATOR",
    "operator_id": "fixture",
    "version_id": "1",
    "execution_binding": {"kind": "LOCAL", "implementation_id": "fixture"},
}


def resolver(target):
    assert target == TARGET
    return {
        "value": "int",
        "other": {"type": "int", "default": 0},
        "enabled": {"type": "boolean", "default": True},
    }, {"value": "int", "identity": "string"}


def workflow(scatter=None):
    inputs = {"values": {"type": {"type": "array", "items": "int"}}, "enabled": "boolean"}
    step = {
        "run": "fixture",
        "in": {"value": "values", "enabled": "enabled"},
        "out": ["value", "identity"],
        "scatter": "value",
        "when": "$(inputs.enabled)",
    }
    if scatter:
        inputs["others"] = {"type": {"type": "array", "items": "int"}}
        step["in"]["other"] = "others"
        step["scatter"] = ["value", "other"]
        step["scatterMethod"] = scatter
    item = ["null", "string"]
    if scatter == "nested_crossproduct":
        item = {"type": "array", "items": item}
    return {
        "class": "Workflow",
        "cwlVersion": "v1.2",
        "inputs": inputs,
        "outputs": {
            "ids": {"type": {"type": "array", "items": item}, "outputSource": "call/identity"}
        },
        "steps": {"call": step},
    }


def run(tmp_path, document, inputs, documents=None):
    request = {
        "entrypoint": "main",
        "documents": {"main": document} | (documents or {}),
        "implementations": {"fixture": TARGET},
    }
    compiled = compile_package(
        request,
        resolver=resolver,
        bridge_command=[sys.executable, str(Path(__file__).with_name("fixture_bridge.py"))],
    )
    for name, value in compiled["files"].items():
        (tmp_path / name).write_text(json.dumps(value))
    job = tmp_path / "job.json"
    job.write_text(json.dumps(inputs | {"v7_context": "[]"}))
    command = [
        str(BIN / "toil-cwl-runner"),
        "--jobStore",
        str(tmp_path / "store"),
        "--no-container",
        "--retryCount",
        "0",
        "--maxCores",
        "2",
        "--maxLocalJobs",
        "2",
        "--maxJobs",
        "2",
        "--clean",
        "never",
        "--outdir",
        str(tmp_path / "out"),
        "--publishWorkflowMetrics",
        "no",
        str(tmp_path / compiled["entrypoint"]),
        str(job),
    ]
    value = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        env=os.environ
        | {
            "PATH": str(BIN) + os.pathsep + os.environ["PATH"],
            # Toil otherwise falls back to the shared XDG runtime directory.
            # A concurrent workflow can reap another run's deferred state
            # file, producing a nondeterministic ResolveIndirect failure.
            "TOIL_COORDINATION_DIR_OVERRIDE": str(tmp_path / "coordination"),
        },
    )
    (tmp_path / "toil.log").write_text(value.stderr)
    assert value.returncode == 0, value.stderr[-12000:]
    return json.loads(value.stdout)


@pytest.mark.parametrize("method", [None, "dotproduct", "flat_crossproduct", "nested_crossproduct"])
def test_real_toil_scatter_identity(tmp_path, method):
    result = run(tmp_path, workflow(method), {"values": [7, 7], "others": [3, 3], "enabled": True})
    ids = result["ids"]
    if method == "nested_crossproduct":
        ids = [item for row in ids for item in row]
    assert len(ids) == (4 if method in {"flat_crossproduct", "nested_crossproduct"} else 2)
    assert len(set(ids)) == len(ids)


def test_real_toil_condition_skips_nodes(tmp_path):
    assert run(tmp_path, workflow(), {"values": [1, 1], "enabled": False})["ids"] == [None, None]


def test_real_toil_nested_subworkflow_scatter_keeps_full_identity(tmp_path):
    def array(value):
        return {"type": "array", "items": value}

    main = {
        "class": "Workflow",
        "inputs": {"values": {"type": array(array("int"))}},
        "outputs": {"ids": {"type": array(array(["null", "string"])), "outputSource": "outer/ids"}},
        "steps": {
            "outer": {
                "run": "sub",
                "in": {"values": "values", "enabled": {"default": True}},
                "out": ["ids"],
                "scatter": "values",
            }
        },
    }
    result = run(tmp_path, main, {"values": [[7, 7], [7, 7]]}, {"sub": workflow()})
    identities = [json.loads(value) for row in result["ids"] for value in row]
    assert [item[1] for item in identities] == [[0, 0], [0, 1], [1, 0], [1, 1]]
    assert all(item[0] == ["outer", "call"] for item in identities)


def test_real_toil_sequence_and_parallel(tmp_path):
    doc = {
        "class": "Workflow",
        "inputs": {"value": "int"},
        "outputs": {
            "answer": {"type": "int", "outputSource": "second/value"},
            "parallel": {"type": "int", "outputSource": "parallel/value"},
        },
        "steps": {
            "first": {"run": "fixture", "in": {"value": "value"}, "out": ["value"]},
            "second": {
                "run": "fixture",
                "in": {"value": "first/value", "other": {"default": 1}},
                "out": ["value"],
            },
            "parallel": {"run": "fixture", "in": {"value": "value"}, "out": ["value"]},
        },
    }
    assert run(tmp_path, doc, {"value": 4}) == {"answer": 5, "parallel": 4}


def test_generated_coordination_processes_have_explicit_bounded_resources():
    compiled = compile_package(
        {
            "entrypoint": "main",
            "documents": {"main": workflow()},
            "implementations": {"fixture": TARGET},
        },
        resolver=resolver,
    )
    processes = [
        value
        for value in compiled["files"].values()
        if value["class"] in {"CommandLineTool", "ExpressionTool"}
    ]
    assert {value["class"] for value in processes} == {"CommandLineTool", "ExpressionTool"}
    for process in processes:
        assert process["requirements"]["ResourceRequirement"] == {
            "coresMin": 0.2,
            "coresMax": 0.2,
            "ramMin": 128,
            "ramMax": 128,
        }


@pytest.mark.parametrize("feature", ["baseCommand", "$import", "DockerRequirement"])
def test_reject_user_execution_features(feature):
    doc = workflow()
    doc[feature] = "untrusted"
    with pytest.raises(RuleViolation):
        compile_package(
            {
                "entrypoint": "main",
                "documents": {"main": doc},
                "implementations": {"fixture": TARGET},
            },
            resolver=resolver,
        )


@pytest.mark.parametrize(
    "default",
    [
        {"class": "File", "location": "/etc/passwd"},
        {"nested": {"$import": "https://example.invalid/value"}},
        {"path": "/run/work/hidden"},
    ],
)
def test_reject_file_and_directive_defaults(default):
    doc = workflow()
    doc["steps"]["call"]["in"]["other"] = {"default": default}
    with pytest.raises(RuleViolation):
        compile_package(
            {
                "entrypoint": "main",
                "documents": {"main": doc},
                "implementations": {"fixture": TARGET},
            },
            resolver=resolver,
        )
