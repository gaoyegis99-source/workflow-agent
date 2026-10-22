import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from platform_contracts.schema import CONTRACTS, export_schemas
from platform_contracts.v1 import CommandReceipt
from pydantic import TypeAdapter, ValidationError

EXAMPLES = json.loads(Path(__file__).with_name("examples.json").read_text())


@pytest.mark.parametrize("name", EXAMPLES)
def test_valid_examples_round_trip_and_match_json_schema(name):
    example = EXAMPLES[name]
    adapter = TypeAdapter(CONTRACTS[example["contract"]])
    model = adapter.validate_python(example["value"])
    assert adapter.validate_json(adapter.dump_json(model)) == model
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example["value"])


# These mutations model common malformed client/model payloads, including coercions.
INVALID_CASES = [
    ("task", ["spec_revision"], 0),
    ("task", ["spec_revision"], -1),
    ("task", ["spec_revision"], "3"),
    ("task", ["spec_revision"], True),
    ("task", ["spec_revision"], 3.5),
    ("task", ["spec_revision"], None),
    ("task", ["task_id"], ""),
    ("task", ["task_id"], "../task"),
    ("task", ["task_id"], " task "),
    ("task", ["task_id"], "x" * 129),
    ("task", ["task_id"], "task\n"),
    ("task", ["control_version"], 3),
    ("asset", ["version_id"], "latest"),
    ("asset", ["version_id"], "LaTeSt"),
    ("asset", ["version_id"], "version-1\n"),
    ("asset", ["version_manifest_digest"], "sha256:123"),
    ("asset", ["version_manifest_digest"], "sha256:" + "A" * 64),
    ("asset", ["version_manifest_digest"], "sha256:" + "a" * 64 + "\n"),
    ("asset", ["project_id"], "https://example.org"),
    ("local_operator", ["kind"], "SHELL"),
    ("local_operator", ["version_id"], "latest"),
    ("local_operator", ["command"], "run anything"),
    ("local_operator", ["execution_binding", "service_binding_id"], "binding-1"),
    ("local_operator", ["execution_binding", "implementation_id"], "/tmp/operator.py"),
    ("local_operator", ["execution_binding", "kind"], "REMOTE"),
    ("remote_operator", ["execution_binding", "implementation_id"], "implementation-1"),
    ("remote_operator", ["execution_binding", "url"], "https://example.org"),
    ("remote_operator", ["execution_binding", "binding_revision"], "2"),
    ("remote_operator", ["execution_binding", "binding_revision"], False),
    ("remote_operator", ["execution_binding", "binding_revision"], 0),
    ("workflow", ["package_ref", "version_id"], "LATEST"),
    ("workflow", ["package_ref", "manifest_digest"], "sha256:..."),
    ("code_package", ["package_ref", "path"], "/tmp/code.py"),
    ("recorded_command", ["status"], "COMPLETED"),
    ("recorded_command", ["status_url"], "https://example.org/commands/command-example"),
    (
        "recorded_command",
        ["status_url"],
        "/api/v1/projects/project-example/commands/command-example\n",
    ),
    ("error", ["retry_mode"], True),
    ("error", ["retry_mode"], "RETRY"),
    ("error", ["code"], "SOMETHING_FAILED"),
    ("error", ["traceback"], "secret"),
    ("error", ["locations"], [{"path": ["/etc/private"]}]),
]


@pytest.mark.parametrize("name,path,value", INVALID_CASES)
def test_invalid_examples_rejected_by_python_json_and_schema(name, path, value):
    example = copy.deepcopy(EXAMPLES[name])
    target = example["value"]
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    adapter = TypeAdapter(CONTRACTS[example["contract"]])
    with pytest.raises(ValidationError):
        adapter.validate_python(example["value"])
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(example["value"]))
    assert not Draft202012Validator(adapter.json_schema()).is_valid(example["value"])


@pytest.mark.parametrize("name", EXAMPLES)
def test_missing_required_field_rejected(name):
    example = copy.deepcopy(EXAMPLES[name])
    adapter = TypeAdapter(CONTRACTS[example["contract"]])
    # Every example deliberately starts with a required field.
    example["value"].pop(next(iter(example["value"])))
    with pytest.raises(ValidationError):
        adapter.validate_python(example["value"])


@pytest.mark.parametrize("value", [3.0, float("nan"), float("inf")])
def test_revision_requires_an_actual_finite_integer(value):
    # JSON Schema regards 3.0 as an integer; the runtime deliberately requires an int.
    payload = {"task_id": "task-example", "spec_revision": value}
    with pytest.raises(ValidationError):
        TypeAdapter(CONTRACTS["TaskRef"]).validate_python(payload)
    with pytest.raises(ValidationError):
        TypeAdapter(CONTRACTS["TaskRef"]).validate_json(json.dumps(payload))


@pytest.mark.parametrize("status", ["RECORDED", "DISPATCHING", "RECONCILING", "RESOLVED"])
def test_receipt_does_not_require_an_invented_execution_id(status):
    payload = copy.deepcopy(EXAMPLES["recorded_command"]["value"])
    payload["status"] = status
    assert CommandReceipt.model_validate(payload).result is None


def test_resolved_receipt_is_a_reference_not_execution_success():
    payload = copy.deepcopy(EXAMPLES["recorded_command"]["value"])
    payload.update(status="RESOLVED", result={"execution_id": "execution-1"})
    assert CommandReceipt.model_validate(payload).result.execution_id == "execution-1"
    payload["result"]["status"] = "SUCCEEDED"
    with pytest.raises(ValidationError):
        CommandReceipt.model_validate(payload)


def test_rejection_requires_error_without_result():
    payload = copy.deepcopy(EXAMPLES["recorded_command"]["value"])
    payload["status"] = "REJECTED"
    with pytest.raises(ValidationError):
        CommandReceipt.model_validate(payload)
    payload["error"] = EXAMPLES["error"]["value"]
    assert CommandReceipt.model_validate(payload).error.code == "REVISION_CONFLICT"
    payload["result"] = {"execution_id": "execution-1"}
    with pytest.raises(ValidationError):
        CommandReceipt.model_validate(payload)


@pytest.mark.parametrize("status", ["RECORDED", "DISPATCHING", "RECONCILING"])
def test_pending_receipt_cannot_claim_a_resolved_result(status):
    payload = copy.deepcopy(EXAMPLES["recorded_command"]["value"])
    payload.update(status=status, result={"execution_id": "execution-1"})
    with pytest.raises(ValidationError):
        CommandReceipt.model_validate(payload)


def test_receipt_cannot_link_a_different_command():
    payload = copy.deepcopy(EXAMPLES["recorded_command"]["value"])
    payload["status_url"] = "/api/v1/projects/project-example/commands/another-command"
    with pytest.raises(ValidationError):
        CommandReceipt.model_validate(payload)


def test_error_is_not_silently_attached_to_a_non_rejection():
    payload = copy.deepcopy(EXAMPLES["recorded_command"]["value"])
    payload["error"] = EXAMPLES["error"]["value"]
    with pytest.raises(ValidationError):
        CommandReceipt.model_validate(payload)


def test_schema_export_covers_all_initial_contracts():
    exported = export_schemas()
    assert exported["contract_version"] == "1"
    assert set(exported["schemas"]) == set(CONTRACTS)
