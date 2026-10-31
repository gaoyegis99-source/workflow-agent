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
from platform_contracts.science import TableAggregateParameters, TableExecutionCreate
from platform_core.application.catalog import build_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.checker_catalog import matches
from platform_core.application.science_validation import aggregate_references_match_fields
from platform_tabular_operators.operators import AggregateFailed, aggregate_table
from pydantic import ValidationError

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DESCRIPTIONS = [
    {"field_id": "f0", "name": "站点", "type": "string"},
    {"field_id": "f1", "name": "整数", "type": "int64"},
    {"field_id": "f2", "name": "水位", "type": "double"},
    {"field_id": "f3", "name": "有效", "type": "bool"},
]


def selection(digest=DIGEST_B):
    return {
        "kind": "TABLE",
        "asset": {
            "project_id": "p",
            "asset_id": "a",
            "version_id": "1",
            "version_manifest_digest": DIGEST_A,
        },
        "representation_id": "normalized",
        "representation_digest": digest,
    }


def parameters(group_by=None, policy="GROUP", measures=None):
    return TableAggregateParameters.model_validate(
        {
            "semantics": "strict-table-aggregate-v1",
            "group_by": ["f0"] if group_by is None else group_by,
            "null_key_policy": policy,
            "measures": measures
            or [
                {"measure_id": "rows", "operation": "COUNT_ROWS"},
                {
                    "measure_id": "values",
                    "operation": "COUNT_NON_NULL",
                    "field_id": "f2",
                },
                {
                    "measure_id": "stations",
                    "operation": "COUNT_DISTINCT",
                    "field_id": "f3",
                },
                {"measure_id": "minimum", "operation": "MIN", "field_id": "f1"},
                {"measure_id": "maximum", "operation": "MAX", "field_id": "f2"},
                {"measure_id": "total", "operation": "SUM", "field_id": "f1"},
                {
                    "measure_id": "average",
                    "operation": "MEAN",
                    "field_id": "f2",
                    "display_name": "平均水位",
                },
            ],
        }
    ).model_dump(mode="json")


def request(**overrides):
    value = {
        "task_id": "task",
        "expected_spec_revision": 1,
        "target": {
            "kind": "OPERATOR",
            "operator_id": "table-aggregate",
            "version_id": "1.0.0",
            "execution_binding": {
                "kind": "LOCAL",
                "implementation_id": "table-aggregate-native-v1",
            },
        },
        "inputs": {"table": selection()},
        "parameters": parameters(),
    }
    value.update(overrides)
    return value


def parquet_bytes(table):
    stream = io.BytesIO()
    pq.write_table(table, stream)
    return stream.getvalue()


def source_table():
    return pa.table(
        {
            "f0": pa.array(["B", "A", "B", None, "A"], type=pa.string()),
            "f1": pa.array([1, 2, None, 4, 5], type=pa.int64()),
            "f2": pa.array([1.0, 2.0, 3.0, None, 6.0], type=pa.float64()),
            "f3": pa.array([True, False, True, None, False], type=pa.bool_()),
        }
    )


def test_contract_catalog_agent_and_core_preflight_are_closed():
    parsed = TableExecutionCreate.model_validate(request())
    parsed.parameters.validate_fields(DESCRIPTIONS)
    spec = TaskSpec.model_validate(
        {
            "goal": "aggregate selected fields",
            "agent_operator_intents": [
                {
                    "operator_id": "table-aggregate",
                    "version_id": "1.0.0",
                    "input": selection(),
                    "parameters": request()["parameters"],
                }
            ],
        }
    )
    assert spec.agent_operator_intents[0].operator_id == "table-aggregate"
    operator = next(
        item for item in build_catalog()["operators"] if item["operator_id"] == "table-aggregate"
    )
    assert (operator["ownership"], operator["trust_level"], operator["lifecycle"]) == (
        "CURATED_PACKAGE",
        "VERIFIED",
        "ACTIVE",
    )
    assert operator["implementations"][0]["protocol"] == "TABULAR_V1"
    assert aggregate_references_match_fields(request()["parameters"], DESCRIPTIONS)
    assert not aggregate_references_match_fields(request()["parameters"], DESCRIPTIONS[1:])


@pytest.mark.parametrize(
    "value",
    [
        {
            "group_by": ["f0", "f0"],
            "null_key_policy": "GROUP",
            "measures": [{"measure_id": "rows", "operation": "COUNT_ROWS"}],
        },
        {
            "group_by": ["f2"],
            "null_key_policy": "GROUP",
            "measures": [{"measure_id": "rows", "operation": "COUNT_ROWS"}],
        },
        {
            "group_by": [],
            "null_key_policy": "GROUP",
            "measures": [{"measure_id": "x", "operation": "COUNT_ROWS", "field_id": "f1"}],
        },
        {
            "group_by": [],
            "null_key_policy": "GROUP",
            "measures": [{"measure_id": "x", "operation": "SUM", "field_id": "f0"}],
        },
        {
            "group_by": [],
            "null_key_policy": "GROUP",
            "measures": [{"measure_id": "x", "operation": "MIN", "field_id": "missing"}],
        },
        {
            "group_by": [],
            "null_key_policy": "GROUP",
            "measures": [
                {"measure_id": "x", "operation": "COUNT_ROWS"},
                {"measure_id": "x", "operation": "COUNT_ROWS"},
            ],
        },
    ],
)
def test_contract_or_schema_rejects_invalid_shapes(value):
    try:
        parsed = TableAggregateParameters.model_validate(
            {"semantics": "strict-table-aggregate-v1", **value}
        )
    except ValidationError:
        return
    with pytest.raises(ValueError):
        parsed.validate_fields(DESCRIPTIONS)


def test_all_operations_preserve_first_group_occurrence_and_ignore_measure_nulls():
    result, fields, summaries, counts = aggregate_table(source_table(), parameters(), DESCRIPTIONS)
    assert result.to_pylist() == [
        {"f0": "B", "f1": 2, "f2": 2, "f3": 1, "f4": 1, "f5": 3.0, "f6": 1, "f7": 2.0},
        {"f0": "A", "f1": 2, "f2": 2, "f3": 1, "f4": 2, "f5": 6.0, "f6": 7, "f7": 4.0},
        {"f0": None, "f1": 1, "f2": 0, "f3": 0, "f4": 4, "f5": None, "f6": 4, "f7": None},
    ]
    assert counts == {
        "input_row_count": 5,
        "participating_row_count": 5,
        "dropped_key_row_count": 0,
        "group_count": 3,
    }
    assert [item["name"] for item in fields] == [
        "站点",
        "rows",
        "values",
        "stations",
        "minimum",
        "maximum",
        "total",
        "平均水位",
    ]
    assert [item["measure_id"] for item in summaries] == [
        "rows",
        "values",
        "stations",
        "minimum",
        "maximum",
        "total",
        "average",
    ]


def test_null_group_policies_are_explicit():
    dropped, _, _, counts = aggregate_table(source_table(), parameters(policy="DROP"), DESCRIPTIONS)
    assert [item["f0"] for item in dropped.to_pylist()] == ["B", "A"]
    assert counts["dropped_key_row_count"] == 1
    with pytest.raises(AggregateFailed) as captured:
        aggregate_table(source_table(), parameters(policy="ERROR"), DESCRIPTIONS)
    assert captured.value.diagnostic["failures"] == [
        {
            "scope": "GROUP_KEY",
            "measure_id": None,
            "field_id": "f0",
            "category": "NULL_GROUP_KEY",
            "count": 1,
        }
    ]


def test_empty_input_has_one_global_group_and_no_grouped_rows():
    empty = pa.table(
        {
            "f0": pa.array([], type=pa.string()),
            "f1": pa.array([], type=pa.int64()),
            "f2": pa.array([], type=pa.float64()),
            "f3": pa.array([], type=pa.bool_()),
        }
    )
    selected = [
        {"measure_id": "rows", "operation": "COUNT_ROWS"},
        {"measure_id": "nonnull", "operation": "COUNT_NON_NULL", "field_id": "f1"},
        {"measure_id": "distinct", "operation": "COUNT_DISTINCT", "field_id": "f1"},
        {"measure_id": "minimum", "operation": "MIN", "field_id": "f1"},
        {"measure_id": "total", "operation": "SUM", "field_id": "f1"},
        {"measure_id": "mean", "operation": "MEAN", "field_id": "f1"},
    ]
    global_result = aggregate_table(
        empty, parameters(group_by=[], measures=selected), DESCRIPTIONS
    )[0]
    assert global_result.to_pylist() == [
        {"f0": 0, "f1": 0, "f2": 0, "f3": None, "f4": None, "f5": None}
    ]
    grouped = aggregate_table(empty, parameters(group_by=["f0"], measures=selected), DESCRIPTIONS)[
        0
    ]
    assert grouped.num_rows == 0


def test_overflow_and_nonfinite_values_fail_without_exposing_values_or_rows():
    failures = []
    with pytest.raises(AggregateFailed) as captured:
        aggregate_table(
            pa.table(
                {
                    "f0": ["A", "A"],
                    "f1": pa.array([2**63 - 1, 1], type=pa.int64()),
                    "f2": [1.0, 2.0],
                    "f3": [True, False],
                }
            ),
            parameters(measures=[{"measure_id": "total", "operation": "SUM", "field_id": "f1"}]),
            DESCRIPTIONS,
        )
    failures.append(captured.value.diagnostic)
    with pytest.raises(AggregateFailed) as captured:
        aggregate_table(
            pa.table(
                {
                    "f0": ["A", "A"],
                    "f1": pa.array([1, 2], type=pa.int64()),
                    "f2": [math.inf, math.nan],
                    "f3": [True, False],
                }
            ),
            parameters(measures=[{"measure_id": "mean", "operation": "MEAN", "field_id": "f2"}]),
            DESCRIPTIONS,
        )
    failures.append(captured.value.diagnostic)
    assert [value["failures"][0]["category"] for value in failures] == [
        "NUMERIC_OVERFLOW",
        "NONFINITE_VALUE",
    ]
    encoded = json.dumps(failures)
    assert str(2**63 - 1) not in encoded and "Infinity" not in encoded and "row" not in encoded


def checker_entry(tmp_path):
    table = source_table()
    frozen = parameters()
    result, fields, summaries, counts = aggregate_table(table, frozen, DESCRIPTIONS)
    source_content = parquet_bytes(table)
    source_digest = "sha256:" + hashlib.sha256(source_content).hexdigest()
    selected = selection(source_digest)
    report = {
        "schema": "table-aggregate-v1",
        "operator_id": "table-aggregate",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": frozen,
        **counts,
        "output_row_count": result.num_rows,
        "field_count": result.num_columns,
        "fields": fields,
        "measure_count": len(summaries),
        "measures": summaries,
    }
    method = {
        "schema": "table-aggregate-v1",
        "operator_id": "table-aggregate",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
        "aggregation_semantics": "strict-table-aggregate-v1",
        "numeric_algorithm": "int64-checked-decimal-mean-and-fsum-v1",
        "group_order": "first-source-occurrence-v1",
        "null_measure_policy": "ignore-v1",
    }
    contents = {
        "report": json.dumps(report, sort_keys=True, ensure_ascii=False).encode(),
        "table": parquet_bytes(result),
        "method": json.dumps(method, sort_keys=True).encode(),
    }
    schemas = {
        "report": "table-aggregate-report-v1",
        "table": "table-aggregate-table-v1",
        "method": "tabular-method-v1",
    }
    formats = {"report": "json", "table": "parquet", "method": "json"}
    role_root = tmp_path / "role"
    role_root.mkdir()
    files, representations = {}, []
    for identity, content in contents.items():
        path = role_root / f"{identity}.bin"
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
    source_path = role_root / "source-table.parquet"
    source_path.write_bytes(source_content)
    manifest = {
        "kind": "OPERATOR_RESULT",
        "result_kind": "OPERATOR_RESULT",
        "representations": representations,
        "tabular": {"fields": fields, "row_count": result.num_rows},
        "source": {
            "target": request()["target"],
            "inputs": {"table": selected},
            "parameters": frozen,
            "provenance": {
                "implementation_id": "table-aggregate-native-v1",
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
            "manifest": {"kind": "OBSERVATIONS", "fields": DESCRIPTIONS, "row_count": 5},
        },
    }


def test_independent_decimal_checker_recomputes_and_rejects_tampering(tmp_path):
    entry = checker_entry(tmp_path)
    assert reproduce_role(tmp_path, entry, "table-aggregate") is True
    table_path = tmp_path / entry["files"]["table"]["path"]
    result = pq.read_table(table_path)
    columns = [result[name] for name in result.column_names]
    columns[-1] = pa.array([999.0, 4.0, None], type=pa.float64())
    pq.write_table(pa.Table.from_arrays(columns, names=result.column_names), table_path)
    digest = "sha256:" + hashlib.sha256(table_path.read_bytes()).hexdigest()
    entry["files"]["table"]["digest"] = digest
    representation = next(
        item
        for item in entry["manifest"]["representations"]
        if item["representation_id"] == "table"
    )
    representation["digest"] = digest
    representation["size"] = table_path.stat().st_size
    assert reproduce_role(tmp_path, entry, "table-aggregate") is False


def test_checker_capability_matches_only_exact_aggregate_report():
    capability = next(
        item
        for item in build_checker_catalog()["capabilities"]
        if item["capability_id"] == "tabular-aggregate-reproduction-v1"
    )
    expected = {
        "result_kind": "OPERATOR_RESULT",
        "operator_id": "table-aggregate",
        "operator_version": "1.0.0",
        "output_port": "report",
        "schema_id": "table-aggregate-report-v1",
    }
    assert matches(capability, "DATA_VALIDITY", expected)
    assert matches(capability, "RESULT_REPRODUCTION", expected)
    for key, value in (
        ("operator_id", "table-filter"),
        ("operator_version", "2.0.0"),
        ("output_port", "table"),
        ("schema_id", "unknown-v1"),
    ):
        assert not matches(capability, "RESULT_REPRODUCTION", expected | {key: value})


def test_frozen_filter_aggregate_profile_workflow_compiles():
    from platform_core.application.workflow_compiler import compile_package

    package = json.loads(
        Path("examples/workflows/table-filter-aggregate-profile-v1.json").read_text()
    )
    compiled = compile_package(package, catalog=build_catalog())
    assert set(compiled["invocations"]) == {"filter", "aggregate", "profile"}
    assert compiled["invocations"]["aggregate"]["target"]["operator_id"] == "table-aggregate"
