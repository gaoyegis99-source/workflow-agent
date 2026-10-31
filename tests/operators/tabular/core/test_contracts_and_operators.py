import json
import math

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from platform_contracts.control import TaskSpec
from platform_contracts.science import TableExecutionCreate
from platform_contracts.service import ServiceFault
from platform_core.application.catalog import build_catalog
from platform_core.domain.control import RuleViolation
from platform_data.application.data import Data
from platform_data.application.publication import descriptor_fields
from platform_execution.adapters.parquet import ParquetInspector
from platform_execution.application.components import OutputCollector
from platform_execution.implementations import CHECKER_IMPLEMENTATIONS, COMPUTE_IMPLEMENTATIONS
from platform_tabular_operators.operators import ValidationFailed, profile_table, validate_table
from pydantic import ValidationError

from tests.pathing import SOURCE_ROOT

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def selection():
    return {
        "kind": "TABLE",
        "asset": {
            "project_id": "p",
            "asset_id": "a",
            "version_id": "1",
            "version_manifest_digest": DIGEST_A,
        },
        "representation_id": "normalized",
        "representation_digest": DIGEST_B,
    }


def request(operator="table-profile", parameters=None):
    implementation = {
        "table-profile": "table-profile-native-v1",
        "table-validate": "table-validate-native-v1",
    }[operator]
    return {
        "task_id": "task",
        "expected_spec_revision": 1,
        "target": {
            "kind": "OPERATOR",
            "operator_id": operator,
            "version_id": "1.0.0",
            "execution_binding": {"kind": "LOCAL", "implementation_id": implementation},
        },
        "inputs": {"table": selection()},
        "parameters": parameters
        or ({"fields": ["f0"]} if operator == "table-profile" else {"rules": []}),
    }


def test_table_execution_and_agent_intent_contracts_are_strict():
    value = TableExecutionCreate.model_validate(request())
    assert value.inputs.table.kind == "TABLE"
    spec = TaskSpec.model_validate(
        {
            "goal": "profile selected fields",
            "agent_operator_intents": [
                {
                    "operator_id": "table-profile",
                    "version_id": "1.0.0",
                    "input": selection(),
                    "parameters": {"fields": ["f0"]},
                }
            ],
        }
    )
    assert spec.agent_operator_intents[0].operator_id == "table-profile"
    with pytest.raises(ValidationError):
        TableExecutionCreate.model_validate(request(parameters={"fields": ["f0", "f0"]}))
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "goal": "duplicate",
                "agent_operator_intents": [
                    {
                        "operator_id": "table-profile",
                        "input": selection(),
                        "parameters": {"fields": ["f0"]},
                    },
                    {
                        "operator_id": "table-profile",
                        "input": selection(),
                        "parameters": {"fields": ["f1"]},
                    },
                ],
            }
        )


@pytest.mark.parametrize(
    "rule",
    [
        {"rule_id": "r", "kind": "ROW_COUNT", "minimum": "01"},
        {"rule_id": "r", "kind": "TYPE", "field_id": "f0"},
        {"rule_id": "r", "kind": "NOT_NULL", "field_id": "f0", "maximum": "1"},
        {"rule_id": "r", "kind": "UNIQUE", "field_ids": ["f0", "f0"]},
        {"rule_id": "r", "kind": "ALLOWED_VALUES", "field_id": "f0"},
    ],
)
def test_invalid_rule_shapes_are_rejected(rule):
    with pytest.raises(ValidationError):
        TableExecutionCreate.model_validate(request("table-validate", {"rules": [rule]}))


def test_catalog_marks_standalone_operators_and_keeps_registries_disjoint():
    catalog = build_catalog()
    operators = {item["operator_id"]: item for item in catalog["operators"]}
    assert len(operators) >= 7
    for identity in ("table-profile", "table-validate"):
        assert operators[identity]["ownership"] == "CURATED_PACKAGE"
        assert operators[identity]["trust_level"] == "VERIFIED"
        implementation = operators[identity]["implementations"][0]
        assert implementation["protocol"] == "TABULAR_V1"
        assert implementation["resource_profile"] == "table-small-v1"
    assert set(COMPUTE_IMPLEMENTATIONS).isdisjoint(CHECKER_IMPLEMENTATIONS)


@pytest.mark.parametrize("name", ["table-profile-validate-v1.json", "completeness-profile-v1.json"])
def test_frozen_tabular_workflow_packages_compile(name):
    from platform_core.application.workflow_compiler import compile_package

    request = json.loads((SOURCE_ROOT / "examples/workflows" / name).read_text(encoding="utf-8"))
    compiled = compile_package(request, catalog=build_catalog())
    assert set(compiled["invocations"])


def table():
    return pa.table(
        {
            "f0": pa.array([1, 2, 2, None], type=pa.int64()),
            "f1": pa.array(["", "水", "水", None], type=pa.string()),
            "f2": pa.array([True, False, True, None], type=pa.bool_()),
            "f3": pa.array([1.5, math.nan, 3.5, None], type=pa.float64()),
        }
    )


def test_profile_is_bounded_and_contains_no_input_values():
    rows = profile_table(table(), ["f0", "f1", "f2", "f3"])
    by_id = {row["field_id"]: row for row in rows}
    assert by_id["f0"]["distinct_count"] == 2
    assert by_id["f1"]["empty_count"] == 1
    assert by_id["f2"]["true_count"] == 2
    assert by_id["f3"]["nonfinite_count"] == 1
    assert by_id["f3"]["distinct_count"] == 3
    rendered = repr(rows)
    assert "水" not in rendered


def test_all_six_validation_rules_and_bounded_failure_diagnostic():
    passing = [
        {"rule_id": "rows", "kind": "ROW_COUNT", "minimum": "4", "maximum": "4"},
        {"rule_id": "type", "kind": "TYPE", "field_id": "f0", "expected_type": "int64"},
        {"rule_id": "null", "kind": "NOT_NULL", "field_id": "f2"},
        {"rule_id": "uniq", "kind": "UNIQUE", "field_ids": ["f0", "f2"]},
        {"rule_id": "range", "kind": "RANGE", "field_id": "f0", "minimum": "1", "maximum": "2"},
        {
            "rule_id": "allowed",
            "kind": "ALLOWED_VALUES",
            "field_id": "f1",
            "allowed_values": ["", "水"],
        },
    ]
    # NOT_NULL deliberately fails on the final row and the failure never contains raw values.
    with pytest.raises(ValidationFailed) as captured:
        validate_table(table(), passing)
    diagnostic = captured.value.diagnostic
    assert diagnostic["code"] == "VALIDATION_FAILED"
    assert diagnostic["failed_rule_count"] == 1
    assert diagnostic["failures"] == [{"rule_id": "null", "kind": "NOT_NULL", "failed_count": 1}]
    assert "水" not in repr(diagnostic)


def test_profile_handles_empty_and_all_null_columns():
    empty = pa.table(
        {
            "text": pa.array([], type=pa.string()),
            "number": pa.array([], type=pa.int64()),
        }
    )
    rows = {row["field_id"]: row for row in profile_table(empty, ["text", "number"])}
    assert rows["text"]["row_count"] == rows["text"]["distinct_count"] == 0
    assert rows["text"]["min_length"] is None
    all_null = pa.table({"number": pa.array([None, None], type=pa.int64())})
    row = profile_table(all_null, ["number"])[0]
    assert (row["null_count"], row["non_null_count"], row["distinct_count"]) == (2, 0, 0)
    assert row["minimum"] is None and row["maximum"] is None


def test_validation_boundaries_unicode_nonfinite_and_nullable_unique_keys():
    boundary = pa.table(
        {
            "i": pa.array([-(2**63), 2**63 - 1, None], type=pa.int64()),
            "d": pa.array([math.nan, math.inf, math.nan], type=pa.float64()),
            "s": pa.array(["水", "", None], type=pa.string()),
            "key": pa.array([1, 1, None], type=pa.int64()),
        }
    )
    passing = [
        {
            "rule_id": "integer-boundary",
            "kind": "RANGE",
            "field_id": "i",
            "minimum": str(-(2**63)),
            "maximum": str(2**63 - 1),
        },
        {
            "rule_id": "unicode",
            "kind": "ALLOWED_VALUES",
            "field_id": "s",
            "allowed_values": ["水", ""],
        },
    ]
    assert all(row["status"] == "PASS" for row in validate_table(boundary, passing))

    with pytest.raises(ValidationFailed) as range_failure:
        validate_table(
            boundary,
            [{"rule_id": "finite", "kind": "RANGE", "field_id": "d", "minimum": "0"}],
        )
    assert range_failure.value.diagnostic["violation_count"] == 3

    with pytest.raises(ValidationFailed) as duplicate_failure:
        validate_table(
            boundary,
            [{"rule_id": "key", "kind": "UNIQUE", "field_ids": ["key"]}],
        )
    assert duplicate_failure.value.diagnostic["violation_count"] == 2

    with pytest.raises(ValidationFailed) as nan_failure:
        validate_table(
            boundary,
            [{"rule_id": "nan-key", "kind": "UNIQUE", "field_ids": ["d"]}],
        )
    assert nan_failure.value.diagnostic["violation_count"] == 2

    invalid_unicode = request(
        "table-validate",
        {
            "rules": [
                {
                    "rule_id": "unicode",
                    "kind": "ALLOWED_VALUES",
                    "field_id": "f0",
                    "allowed_values": ["e\u0301"],
                }
            ]
        },
    )
    with pytest.raises(ValidationError):
        TableExecutionCreate.model_validate(invalid_unicode)


@pytest.mark.parametrize(
    ("rule", "violations"),
    [
        ({"rule_id": "rows", "kind": "ROW_COUNT", "maximum": "3"}, 1),
        (
            {
                "rule_id": "type",
                "kind": "TYPE",
                "field_id": "f0",
                "expected_type": "string",
            },
            1,
        ),
        ({"rule_id": "null", "kind": "NOT_NULL", "field_id": "f0"}, 1),
        ({"rule_id": "unique", "kind": "UNIQUE", "field_ids": ["f0"]}, 2),
        ({"rule_id": "range", "kind": "RANGE", "field_id": "f0", "maximum": "1"}, 2),
        (
            {
                "rule_id": "allowed",
                "kind": "ALLOWED_VALUES",
                "field_id": "f1",
                "allowed_values": [""],
            },
            2,
        ),
    ],
)
def test_each_validation_rule_has_a_bounded_negative_result(rule, violations):
    with pytest.raises(ValidationFailed) as failure:
        validate_table(table(), [rule])
    assert failure.value.diagnostic["violation_count"] == violations


def test_data_resolves_only_checked_table_representations():
    service = object.__new__(Data)
    manifest = {
        "kind": "OBSERVATIONS",
        "fields": [
            {"field_id": "f0", "name": "a", "type": "int64"},
            {"field_id": "f1", "name": "b", "type": "string"},
        ],
        "row_count": 2,
        "representations": [
            {
                "representation_id": "normalized",
                "format": "parquet",
                "digest": DIGEST_B,
                "size": 10,
            }
        ],
    }
    service.asset = lambda project, asset, version: {
        "ref": selection()["asset"],
        "manifest": manifest,
        "semantic_revisions": [],
    }
    value = service.resolve("p", selection())
    assert value["semantics"] is None
    assert value["table"]["row_count"] == 2


def test_operator_result_descriptor_is_explicit():
    descriptor = {
        "schema_version": 1,
        "kind": "OPERATOR_RESULT",
        "tabular": {
            "fields": [{"field_id": "field_id", "name": "field_id", "type": "string"}],
            "row_count": 1,
        },
    }
    assert descriptor_fields(descriptor) == {"tabular": descriptor["tabular"]}


@pytest.mark.parametrize("tamper", ["extra", "missing", "symlink", "source", "schema"])
def test_execution_rejects_tampered_tabular_outputs(tmp_path, tamper):
    output = tmp_path / "output"
    output.mkdir()
    rows = [
        {
            "rule_id": "rows",
            "kind": "ROW_COUNT",
            "status": "PASS",
            "checked_count": 1,
            "failed_count": 0,
        }
    ]
    schema = pa.schema(
        [
            ("rule_id", pa.string()),
            ("kind", pa.string()),
            ("status", pa.string()),
            ("checked_count", pa.int64()),
            ("failed_count", pa.int64()),
        ]
    )
    report = {
        "schema": "table-validation-v1",
        "operator_id": "table-validate",
        "version_id": "1.0.0",
        "source_digest": DIGEST_B,
        "parameters": {
            "rules": [
                {
                    "rule_id": "rows",
                    "kind": "ROW_COUNT",
                    "field_id": None,
                    "field_ids": None,
                    "expected_type": None,
                    "minimum": "1",
                    "maximum": None,
                    "allowed_values": None,
                }
            ]
        },
        "row_count": 1,
        "rows": rows,
    }
    method = {
        "schema": "table-validation-v1",
        "operator_id": "table-validate",
        "version_id": "1.0.0",
        "source_digest": DIGEST_B,
        "method": "deterministic-pyarrow-v1",
    }
    (output / "report.json").write_text(json.dumps(report))
    (output / "method.json").write_text(json.dumps(method))
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), output / "table.parquet")
    if tamper == "extra":
        (output / "secret.txt").write_text("forbidden")
    elif tamper == "missing":
        (output / "method.json").unlink()
    elif tamper == "symlink":
        (output / "method.json").unlink()
        (output / "method.json").symlink_to(output / "report.json")
    elif tamper == "source":
        report["source_digest"] = DIGEST_A
        (output / "report.json").write_text(json.dumps(report))
    elif tamper == "schema":
        pq.write_table(pa.table({"rule_id": ["rows"]}), output / "table.parquet")

    row = {
        "runtime": {
            "implementation_profile": {
                "implementation_id": "table-validate-native-v1",
                "outputs": {
                    "report": "report.json",
                    "table": "table.parquet",
                    "method": "method.json",
                },
            }
        },
        "payload": {
            "target": {"operator_id": "table-validate"},
            "bindings": {"table": {"representation": {"digest": DIGEST_B}}},
            "parameters": report["parameters"],
        },
    }
    collector = object.__new__(OutputCollector)
    collector.table_inspector = ParquetInspector()
    with pytest.raises(ServiceFault):
        OutputCollector.collect_tabular(collector, row, tmp_path)


def test_standalone_package_has_no_platform_imports():
    root = SOURCE_ROOT / "packages/tabular-operator-pack/src"
    text = "\n".join(path.read_text() for path in root.rglob("*.py"))
    for forbidden in ("platform_core", "platform_operators", "platform_execution", "sqlalchemy"):
        assert forbidden not in text


def test_agent_v2_submits_only_the_frozen_table_intent():
    from platform_agent_runtime.action_tools import PlatformActionTools

    class Science:
        def __init__(self):
            self.body = None

        def submit(self, actor, project, key, body, agent_context=None):
            self.body = body
            return {"action_id": "action", "command_id": "command", "status": "RECORDED"}

    backend = object.__new__(PlatformActionTools)
    backend.operator_catalog = build_catalog()
    backend.science = Science()
    backend._task_spec = lambda context: {
        "goal": "profile",
        "agent_operator_intents": [
            {
                "operator_id": "table-profile",
                "version_id": "1.0.0",
                "input": selection(),
                "parameters": {"fields": ["f0"]},
            }
        ],
    }
    context = {
        "project_id": "p",
        "task_id": "task",
        "task_spec_revision": 1,
        "principal_id": "agent",
        "agent_run_id": "run",
        "frame_id": "frame",
        "tool_version": "2.0.0",
    }
    result = backend.execute(
        "execution.submit_operator",
        {"operator_id": "table-profile", "version_id": "1.0.0"},
        context,
        "request",
    )
    assert result.action_id == "action"
    assert backend.science.body["inputs"] == {"table": selection()}
    assert backend.science.body["parameters"] == {"fields": ["f0"]}
    with pytest.raises(RuleViolation) as error:
        backend.execute(
            "execution.submit_operator",
            {"operator_id": "table-validate", "version_id": "1.0.0"},
            context,
            "other",
        )
    assert error.value.code == "PRECONDITION_UNRESOLVED"
