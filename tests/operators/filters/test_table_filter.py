import hashlib
import io
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from platform_checkers.tabular import reproduce_role
from platform_contracts.control import TaskSpec
from platform_contracts.science import TableExecutionCreate, TableFilterParameters
from platform_core.application.catalog import build_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.checker_catalog import matches
from platform_core.application.science_validation import filter_references_match_fields
from platform_tabular_operators.operators import filter_table
from pydantic import ValidationError

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DESCRIPTIONS = [
    {"field_id": "f0", "name": "水位", "type": "double"},
    {"field_id": "f1", "name": "站点", "type": "string"},
    {"field_id": "f2", "name": "序号", "type": "int64"},
    {"field_id": "f3", "name": "启用", "type": "bool"},
]


def source_table():
    return pa.table(
        {
            "f0": pa.array([-1.0, None, math.nan, math.inf, 2.0, -0.0, 5.0]),
            "f1": pa.array(["A", "A", "B", None, "C", "C", "D"]),
            "f2": pa.array(range(7), type=pa.int64()),
            "f3": pa.array([True, False, True, False, None, True, False]),
        }
    )


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


def node(identity, kind, **values):
    return {
        "node_id": identity,
        "kind": kind,
        "field_id": None,
        "operator": None,
        "value_type": None,
        "value": None,
        "values": None,
        "arguments": [],
    } | values


def predicate():
    return {
        "semantics": "kleene-three-valued-v1",
        "root_id": "any",
        "nodes": [
            node(
                "positive",
                "COMPARE",
                field_id="f0",
                operator="GT",
                value_type="double",
                value="0",
            ),
            node("finite", "IS_FINITE", field_id="f0"),
            node("positive_finite", "AND", arguments=["positive", "finite"]),
            node("station_missing", "IS_NULL", field_id="f1"),
            node("any", "OR", arguments=["positive_finite", "station_missing"]),
        ],
    }


def parameters(value=None):
    return TableFilterParameters.model_validate({"predicate": value or predicate()}).model_dump(
        mode="json"
    )


def request(value=None):
    return {
        "task_id": "task",
        "expected_spec_revision": 1,
        "target": {
            "kind": "OPERATOR",
            "operator_id": "table-filter",
            "version_id": "1.0.0",
            "execution_binding": {
                "kind": "LOCAL",
                "implementation_id": "table-filter-native-v1",
            },
        },
        "inputs": {"table": selection()},
        "parameters": parameters(value),
    }


def parquet_bytes(table):
    stream = io.BytesIO()
    pq.write_table(table, stream)
    return stream.getvalue()


def test_contract_catalog_and_agent_intent_are_closed():
    parsed = TableExecutionCreate.model_validate(request())
    parsed.parameters.predicate.validate_fields(DESCRIPTIONS)
    spec = TaskSpec.model_validate(
        {
            "goal": "filter rows",
            "agent_operator_intents": [
                {
                    "operator_id": "table-filter",
                    "version_id": "1.0.0",
                    "input": selection(),
                    "parameters": request()["parameters"],
                }
            ],
        }
    )
    assert spec.agent_operator_intents[0].operator_id == "table-filter"
    operator = next(
        item for item in build_catalog()["operators"] if item["operator_id"] == "table-filter"
    )
    assert (operator["ownership"], operator["trust_level"], operator["lifecycle"]) == (
        "CURATED_PACKAGE",
        "VERIFIED",
        "ACTIVE",
    )
    assert operator["implementations"][0]["protocol"] == "TABULAR_V1"


def test_kleene_filter_retains_only_true_rows_and_preserves_order_and_schema():
    result, counts = filter_table(source_table(), parameters()["predicate"])
    assert counts == {"true_count": 3, "false_count": 3, "unknown_count": 1}
    assert result.schema == source_table().schema
    assert result["f2"].to_pylist() == [3, 4, 6]
    assert result["f0"][0].as_py() == math.inf


def test_not_does_not_turn_null_or_nan_into_matches():
    value = {
        "semantics": "kleene-three-valued-v1",
        "root_id": "not_positive",
        "nodes": [
            node(
                "positive",
                "COMPARE",
                field_id="f0",
                operator="GT",
                value_type="double",
                value="0",
            ),
            node("not_positive", "NOT", arguments=["positive"]),
        ],
    }
    result, counts = filter_table(source_table(), parameters(value)["predicate"])
    assert result["f2"].to_pylist() == [0, 5]
    assert counts == {"true_count": 2, "false_count": 3, "unknown_count": 2}


def test_explicit_nan_and_in_predicates_have_defined_null_semantics():
    nan = {
        "semantics": "kleene-three-valued-v1",
        "root_id": "nan",
        "nodes": [node("nan", "IS_NAN", field_id="f0")],
    }
    result, counts = filter_table(source_table(), parameters(nan)["predicate"])
    assert result["f2"].to_pylist() == [2]
    assert counts == {"true_count": 1, "false_count": 5, "unknown_count": 1}
    in_set = {
        "semantics": "kleene-three-valued-v1",
        "root_id": "stations",
        "nodes": [
            node(
                "stations",
                "IN",
                field_id="f1",
                value_type="string",
                values=["A", "C"],
            )
        ],
    }
    result, counts = filter_table(source_table(), parameters(in_set)["predicate"])
    assert result["f2"].to_pylist() == [0, 1, 4, 5]
    assert counts == {"true_count": 4, "false_count": 2, "unknown_count": 1}


@pytest.mark.parametrize(
    "value",
    [
        {
            "semantics": "kleene-three-valued-v1",
            "root_id": "n0",
            "nodes": [
                node("n0", "NOT", arguments=["later"]),
                node("later", "IS_NULL", field_id="f0"),
            ],
        },
        {
            "semantics": "kleene-three-valued-v1",
            "root_id": "root",
            "nodes": [
                node("orphan", "IS_NULL", field_id="f0"),
                node("root", "IS_NULL", field_id="f1"),
            ],
        },
        {
            "semantics": "kleene-three-valued-v1",
            "root_id": "ordered",
            "nodes": [
                node(
                    "ordered",
                    "COMPARE",
                    field_id="f3",
                    operator="GT",
                    value_type="bool",
                    value="true",
                )
            ],
        },
        {
            "semantics": "kleene-three-valued-v1",
            "root_id": "duplicates",
            "nodes": [
                node(
                    "duplicates",
                    "IN",
                    field_id="f0",
                    value_type="double",
                    values=["0", "0.0"],
                )
            ],
        },
    ],
)
def test_contract_rejects_non_tree_or_ambiguous_predicates(value):
    with pytest.raises(ValidationError):
        TableFilterParameters.model_validate({"predicate": value})


def test_contract_rejects_depth_over_eight_and_schema_mismatch():
    nodes = [node("n0", "IS_NULL", field_id="f0")]
    for index in range(1, 9):
        nodes.append(node(f"n{index}", "NOT", arguments=[f"n{index - 1}"]))
    with pytest.raises(ValidationError):
        TableFilterParameters.model_validate(
            {
                "predicate": {
                    "semantics": "kleene-three-valued-v1",
                    "root_id": "n8",
                    "nodes": nodes,
                }
            }
        )
    wrong_type = parameters(
        {
            "semantics": "kleene-three-valued-v1",
            "root_id": "n0",
            "nodes": [
                node(
                    "n0",
                    "COMPARE",
                    field_id="f0",
                    operator="EQ",
                    value_type="int64",
                    value="1",
                )
            ],
        }
    )
    with pytest.raises(ValueError):
        TableFilterParameters.model_validate(wrong_type).predicate.validate_fields(DESCRIPTIONS)
    invalid_nan = parameters(
        {
            "semantics": "kleene-three-valued-v1",
            "root_id": "n0",
            "nodes": [node("n0", "IS_NAN", field_id="f1")],
        }
    )
    with pytest.raises(ValueError):
        TableFilterParameters.model_validate(invalid_nan).predicate.validate_fields(DESCRIPTIONS)


def test_core_preflight_matches_only_the_resolved_table_schema():
    assert filter_references_match_fields(parameters(), DESCRIPTIONS)
    assert not filter_references_match_fields(parameters(), DESCRIPTIONS[1:])
    mismatched = parameters()
    mismatched["predicate"]["nodes"][0]["value_type"] = "int64"
    assert not filter_references_match_fields(mismatched, DESCRIPTIONS)
    invalid_nan = parameters(
        {
            "semantics": "kleene-three-valued-v1",
            "root_id": "n0",
            "nodes": [node("n0", "IS_NAN", field_id="f1")],
        }
    )
    assert not filter_references_match_fields(invalid_nan, DESCRIPTIONS)


def checker_entry(tmp_path):
    source = source_table()
    frozen = parameters()
    result, counts = filter_table(source, frozen["predicate"])
    source_content = parquet_bytes(source)
    source_digest = "sha256:" + hashlib.sha256(source_content).hexdigest()
    selected = selection() | {"representation_digest": source_digest}
    encoded = json.dumps(
        frozen["predicate"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    report = {
        "schema": "table-filter-v1",
        "operator_id": "table-filter",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": frozen,
        "predicate_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "input_row_count": source.num_rows,
        "output_row_count": result.num_rows,
        "dropped_row_count": source.num_rows - result.num_rows,
        **counts,
    }
    method = {
        "schema": "table-filter-v1",
        "operator_id": "table-filter",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
        "predicate_semantics": "kleene-three-valued-v1",
    }
    contents = {
        "report": json.dumps(report, sort_keys=True, ensure_ascii=False).encode(),
        "table": parquet_bytes(result),
        "method": json.dumps(method, sort_keys=True).encode(),
    }
    schemas = {
        "report": "table-filter-report-v1",
        "table": "table-filter-table-v1",
        "method": "tabular-method-v1",
    }
    formats = {"report": "json", "table": "parquet", "method": "json"}
    root = tmp_path / "role"
    root.mkdir()
    files, representations = {}, []
    for identity, content in contents.items():
        path = root / f"{identity}.bin"
        path.write_bytes(content)
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        files[identity] = {
            "path": f"role/{identity}.bin",
            "digest": digest,
            "format": formats[identity],
            "schema_id": schemas[identity],
        }
        representations.append(
            {
                "representation_id": identity,
                "format": formats[identity],
                "digest": digest,
                "size": len(content),
                "schema_id": schemas[identity],
            }
        )
    source_path = root / "source-table.parquet"
    source_path.write_bytes(source_content)
    manifest = {
        "kind": "OPERATOR_RESULT",
        "result_kind": "OPERATOR_RESULT",
        "representations": representations,
        "tabular": {"fields": DESCRIPTIONS, "row_count": result.num_rows},
        "source": {
            "target": {
                "kind": "OPERATOR",
                "operator_id": "table-filter",
                "version_id": "1.0.0",
                "execution_binding": {
                    "kind": "LOCAL",
                    "implementation_id": "table-filter-native-v1",
                },
            },
            "inputs": {"table": selected},
            "parameters": frozen,
            "provenance": {
                "implementation_id": "table-filter-native-v1",
                "image_id": "sha256:" + "1" * 64,
                "catalog_digest": "sha256:" + "2" * 64,
                "operator_digest": "sha256:" + "3" * 64,
                "implementation_digest": "sha256:" + "4" * 64,
                "ownership": "CURATED_PACKAGE",
                "trust_level": "VERIFIED",
                "input_digests": {"table": source_digest},
            },
        },
    }
    return {
        "manifest": manifest,
        "files": files,
        "source_table": {
            "path": "role/source-table.parquet",
            "digest": source_digest,
            "selection": selected,
            "manifest": {"kind": "OBSERVATIONS", "fields": DESCRIPTIONS, "row_count": 7},
        },
    }


def test_independent_checker_recomputes_rows_and_rejects_equal_size_tampering(tmp_path):
    entry = checker_entry(tmp_path)
    assert reproduce_role(tmp_path, entry, "table-filter") is True
    table_path = tmp_path / entry["files"]["table"]["path"]
    wrong = source_table().take(pa.array([0, 4, 6]))
    pq.write_table(wrong, table_path)
    entry["files"]["table"]["digest"] = (
        "sha256:" + hashlib.sha256(table_path.read_bytes()).hexdigest()
    )
    representation = next(
        item
        for item in entry["manifest"]["representations"]
        if item["representation_id"] == "table"
    )
    representation["digest"] = entry["files"]["table"]["digest"]
    representation["size"] = table_path.stat().st_size
    assert reproduce_role(tmp_path, entry, "table-filter") is False


def test_checker_capability_matches_only_exact_filter_report():
    capability = next(
        item
        for item in build_checker_catalog()["capabilities"]
        if item["capability_id"] == "tabular-filter-reproduction-v1"
    )
    expected = {
        "result_kind": "OPERATOR_RESULT",
        "operator_id": "table-filter",
        "operator_version": "1.0.0",
        "output_port": "report",
        "schema_id": "table-filter-report-v1",
    }
    assert matches(capability, "DATA_VALIDITY", expected)
    assert matches(capability, "RESULT_REPRODUCTION", expected)
    for key, value in (
        ("operator_id", "table-project"),
        ("operator_version", "2.0.0"),
        ("output_port", "table"),
        ("schema_id", "unknown-v1"),
    ):
        assert not matches(capability, "RESULT_REPRODUCTION", expected | {key: value})


def test_frozen_project_filter_profile_workflow_compiles():
    from platform_core.application.workflow_compiler import compile_package

    package = json.loads(
        Path("examples/workflows/table-project-filter-profile-v1.json").read_text()
    )
    compiled = compile_package(package, catalog=build_catalog())
    assert set(compiled["invocations"]) == {"project", "filter", "profile"}
    assert compiled["invocations"]["filter"]["target"]["operator_id"] == "table-filter"
