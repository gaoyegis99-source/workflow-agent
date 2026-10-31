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
from platform_contracts.science import TableCastParameters, TableExecutionCreate
from platform_core.application.catalog import build_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.checker_catalog import matches
from platform_core.application.science_validation import cast_references_match_fields
from platform_tabular_operators.operators import CastFailed, cast_table
from pydantic import ValidationError

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DESCRIPTIONS = [
    {"field_id": "f0", "name": "文本", "type": "string"},
    {"field_id": "f1", "name": "整数", "type": "int64"},
    {"field_id": "f2", "name": "小数", "type": "double"},
    {"field_id": "f3", "name": "开关", "type": "bool"},
]


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


def parameters(casts=None):
    return TableCastParameters.model_validate(
        {
            "semantics": "strict-scalar-cast-v1",
            "casts": casts
            or [
                {"field_id": "f0", "target_type": "int64", "on_error": "SET_NULL"},
                {"field_id": "f3", "target_type": "string", "on_error": "ERROR"},
            ],
        }
    ).model_dump(mode="json")


def request(casts=None):
    return {
        "task_id": "task",
        "expected_spec_revision": 1,
        "target": {
            "kind": "OPERATOR",
            "operator_id": "table-cast",
            "version_id": "1.0.0",
            "execution_binding": {
                "kind": "LOCAL",
                "implementation_id": "table-cast-native-v1",
            },
        },
        "inputs": {"table": selection()},
        "parameters": parameters(casts),
    }


def parquet_bytes(table):
    stream = io.BytesIO()
    pq.write_table(table, stream)
    return stream.getvalue()


def test_contract_catalog_agent_and_core_preflight_are_closed():
    parsed = TableExecutionCreate.model_validate(request())
    parsed.parameters.validate_fields(DESCRIPTIONS)
    spec = TaskSpec.model_validate(
        {
            "goal": "cast selected fields",
            "agent_operator_intents": [
                {
                    "operator_id": "table-cast",
                    "version_id": "1.0.0",
                    "input": selection(),
                    "parameters": request()["parameters"],
                }
            ],
        }
    )
    assert spec.agent_operator_intents[0].operator_id == "table-cast"
    operator = next(
        item for item in build_catalog()["operators"] if item["operator_id"] == "table-cast"
    )
    assert (operator["ownership"], operator["trust_level"], operator["lifecycle"]) == (
        "CURATED_PACKAGE",
        "VERIFIED",
        "ACTIVE",
    )
    assert operator["implementations"][0]["protocol"] == "TABULAR_V1"
    assert cast_references_match_fields(request()["parameters"], DESCRIPTIONS)
    assert not cast_references_match_fields(request()["parameters"], DESCRIPTIONS[1:])


@pytest.mark.parametrize(
    "casts",
    [
        [
            {"field_id": "f0", "target_type": "int64", "on_error": "ERROR"},
            {"field_id": "f0", "target_type": "double", "on_error": "SET_NULL"},
        ],
        [{"field_id": "missing", "target_type": "int64", "on_error": "ERROR"}],
        [{"field_id": "f0", "target_type": "string", "on_error": "ERROR"}],
    ],
)
def test_contract_or_schema_rejects_duplicate_unknown_and_identity_casts(casts):
    try:
        parsed = TableCastParameters.model_validate(
            {"semantics": "strict-scalar-cast-v1", "casts": casts}
        )
    except ValidationError:
        return
    with pytest.raises(ValueError):
        parsed.validate_fields(DESCRIPTIONS)


def test_set_null_preserves_rows_order_names_and_reports_failures():
    table = pa.table(
        {
            "f0": pa.array(["1", "-2", "01", "bad", None]),
            "f1": pa.array([0, 1, 2, 2**53 + 1, None], type=pa.int64()),
            "f2": pa.array([0.0, 1.0, 1.5, math.inf, math.nan]),
            "f3": pa.array([True, False, None, True, False]),
        }
    )
    casts = [
        {"field_id": "f2", "target_type": "bool", "on_error": "SET_NULL"},
        {"field_id": "f0", "target_type": "int64", "on_error": "SET_NULL"},
        {"field_id": "f1", "target_type": "double", "on_error": "SET_NULL"},
        {"field_id": "f3", "target_type": "string", "on_error": "ERROR"},
    ]
    result, summaries = cast_table(table, casts)
    assert result.column_names == table.column_names
    assert result.num_rows == table.num_rows
    assert [str(field.type) for field in result.schema] == ["int64", "double", "bool", "string"]
    assert result["f0"].to_pylist() == [1, -2, None, None, None]
    assert result["f1"].to_pylist() == [0.0, 1.0, 2.0, None, None]
    assert result["f2"].to_pylist() == [False, True, None, None, None]
    assert result["f3"].to_pylist() == ["true", "false", None, "true", "false"]
    assert [item["field_id"] for item in summaries] == ["f2", "f0", "f1", "f3"]
    assert summaries[0]["failure_count"] == 3
    assert summaries[1]["failure_count"] == 2
    assert summaries[2]["failure_count"] == 1
    assert summaries[3]["failure_count"] == 0


def test_error_policy_fails_whole_result_without_values_or_rows_in_diagnostic():
    table = pa.table({"f0": ["1", "bad", None]})
    with pytest.raises(CastFailed) as captured:
        cast_table(
            table,
            [{"field_id": "f0", "target_type": "int64", "on_error": "ERROR"}],
        )
    diagnostic = captured.value.diagnostic
    assert diagnostic == {
        "code": "CAST_FAILED",
        "status": "INVALID",
        "cast_count": 1,
        "failed_cast_count": 1,
        "violation_count": 1,
        "failures": [
            {
                "field_id": "f0",
                "source_type": "string",
                "target_type": "int64",
                "failure_count": 1,
            }
        ],
    }
    encoded = json.dumps(diagnostic)
    assert "bad" not in encoded and "row" not in encoded


def test_strict_numeric_boundaries_reject_overflow_precision_and_nonfinite_values():
    string_table = pa.table({"f0": [str(2**63 - 1), str(2**63), "1.0", "+1"]})
    result, summary = cast_table(
        string_table,
        [{"field_id": "f0", "target_type": "int64", "on_error": "SET_NULL"}],
    )
    assert result["f0"].to_pylist() == [2**63 - 1, None, None, None]
    assert summary[0]["failure_count"] == 3
    int_table = pa.table({"f0": pa.array([2**53, 2**53 + 1], type=pa.int64())})
    result, summary = cast_table(
        int_table,
        [{"field_id": "f0", "target_type": "double", "on_error": "SET_NULL"}],
    )
    assert result["f0"].to_pylist() == [float(2**53), None]
    assert summary[0]["failure_count"] == 1
    double_table = pa.table({"f0": [float(2**63), math.inf, math.nan, 1.5]})
    result, summary = cast_table(
        double_table,
        [{"field_id": "f0", "target_type": "int64", "on_error": "SET_NULL"}],
    )
    assert result["f0"].to_pylist() == [None, None, None, None]
    assert summary[0]["failure_count"] == 4


def checker_entry(tmp_path):
    source = pa.table(
        {
            "f0": pa.array(["1", "bad", None]),
            "f1": pa.array([True, False, None]),
        }
    )
    descriptions = [
        {"field_id": "f0", "name": "值", "type": "string"},
        {"field_id": "f1", "name": "有效", "type": "bool"},
    ]
    frozen = parameters(
        [
            {"field_id": "f0", "target_type": "int64", "on_error": "SET_NULL"},
            {"field_id": "f1", "target_type": "string", "on_error": "ERROR"},
        ]
    )
    result, summaries = cast_table(source, frozen["casts"])
    source_content = parquet_bytes(source)
    source_digest = "sha256:" + hashlib.sha256(source_content).hexdigest()
    selected = selection() | {"representation_digest": source_digest}
    report = {
        "schema": "table-cast-v1",
        "operator_id": "table-cast",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": frozen,
        "row_count": source.num_rows,
        "cast_count": len(summaries),
        "casts": summaries,
    }
    method = {
        "schema": "table-cast-v1",
        "operator_id": "table-cast",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
        "cast_semantics": "strict-scalar-cast-v1",
    }
    contents = {
        "report": json.dumps(report, sort_keys=True, ensure_ascii=False).encode(),
        "table": parquet_bytes(result),
        "method": json.dumps(method, sort_keys=True).encode(),
    }
    schemas = {
        "report": "table-cast-report-v1",
        "table": "table-cast-table-v1",
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
        "tabular": {
            "fields": [
                {"field_id": "f0", "name": "值", "type": "int64"},
                {"field_id": "f1", "name": "有效", "type": "string"},
            ],
            "row_count": 3,
        },
        "source": {
            "target": request()["target"],
            "inputs": {"table": selected},
            "parameters": frozen,
            "provenance": {
                "implementation_id": "table-cast-native-v1",
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
            "manifest": {"kind": "OBSERVATIONS", "fields": descriptions, "row_count": 3},
        },
    }


def test_independent_checker_recomputes_cast_and_rejects_tampering(tmp_path):
    entry = checker_entry(tmp_path)
    assert reproduce_role(tmp_path, entry, "table-cast") is True
    report_path = tmp_path / entry["files"]["report"]["path"]
    report = json.loads(report_path.read_text())
    report["casts"][0]["failure_count"] = 0
    report_path.write_text(json.dumps(report, sort_keys=True))
    digest = "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
    entry["files"]["report"]["digest"] = digest
    representation = next(
        item
        for item in entry["manifest"]["representations"]
        if item["representation_id"] == "report"
    )
    representation["digest"] = digest
    representation["size"] = report_path.stat().st_size
    assert reproduce_role(tmp_path, entry, "table-cast") is False


def test_checker_capability_matches_only_exact_cast_report():
    capability = next(
        item
        for item in build_checker_catalog()["capabilities"]
        if item["capability_id"] == "tabular-cast-reproduction-v1"
    )
    expected = {
        "result_kind": "OPERATOR_RESULT",
        "operator_id": "table-cast",
        "operator_version": "1.0.0",
        "output_port": "report",
        "schema_id": "table-cast-report-v1",
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


def test_frozen_project_filter_cast_profile_workflow_compiles():
    from platform_core.application.workflow_compiler import compile_package

    package = json.loads(
        Path("examples/workflows/table-project-filter-cast-profile-v1.json").read_text()
    )
    compiled = compile_package(package, catalog=build_catalog())
    assert set(compiled["invocations"]) == {"project", "filter", "cast", "profile"}
    assert compiled["invocations"]["cast"]["target"]["operator_id"] == "table-cast"
