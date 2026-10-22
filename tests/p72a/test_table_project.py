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
from platform_contracts.science import TableExecutionCreate
from platform_core.application.catalog import build_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.checker_catalog import matches
from platform_tabular_operators.operators import InputError, project_table
from pydantic import ValidationError

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


def request(parameters=None):
    return {
        "task_id": "task",
        "expected_spec_revision": 1,
        "target": {
            "kind": "OPERATOR",
            "operator_id": "table-project",
            "version_id": "1.0.0",
            "execution_binding": {
                "kind": "LOCAL",
                "implementation_id": "table-project-native-v1",
            },
        },
        "inputs": {"table": selection()},
        "parameters": parameters
        or {
            "fields": [
                {"source_field_id": "f2", "display_name": "水位"},
                {"source_field_id": "f0", "display_name": None},
            ]
        },
    }


def source_table():
    return pa.table(
        {
            "f0": pa.array([1, None, 3], type=pa.int64()),
            "f1": pa.array(["A", "水", None], type=pa.string()),
            "f2": pa.array([-0.0, math.nan, math.inf], type=pa.float64()),
            "f3": pa.array([True, False, None], type=pa.bool_()),
        }
    )


DESCRIPTIONS = [
    {"field_id": "f0", "name": "编号", "type": "int64"},
    {"field_id": "f1", "name": "站点", "type": "string"},
    {"field_id": "f2", "name": "原始水位", "type": "double"},
    {"field_id": "f3", "name": "启用", "type": "bool"},
]


def parquet_bytes(table):
    stream = io.BytesIO()
    pq.write_table(table, stream)
    return stream.getvalue()


def test_contract_catalog_and_agent_intent_are_closed():
    parsed = TableExecutionCreate.model_validate(request())
    assert [item.source_field_id for item in parsed.parameters.fields] == ["f2", "f0"]
    spec = TaskSpec.model_validate(
        {
            "goal": "project fields",
            "agent_operator_intents": [
                {
                    "operator_id": "table-project",
                    "version_id": "1.0.0",
                    "input": selection(),
                    "parameters": request()["parameters"],
                }
            ],
        }
    )
    assert spec.agent_operator_intents[0].operator_id == "table-project"
    operator = next(
        item for item in build_catalog()["operators"] if item["operator_id"] == "table-project"
    )
    assert (operator["ownership"], operator["trust_level"], operator["lifecycle"]) == (
        "CURATED_PACKAGE",
        "VERIFIED",
        "ACTIVE",
    )
    assert operator["implementations"][0]["protocol"] == "TABULAR_V1"


@pytest.mark.parametrize(
    "parameters",
    [
        {"fields": []},
        {"fields": [{"source_field_id": "f0"}, {"source_field_id": "f0"}]},
        {"fields": [{"source_field_id": "f0", "display_name": " 水位"}]},
        {"fields": [{"source_field_id": "f0", "display_name": "水\u202e位"}]},
    ],
)
def test_contract_rejects_ambiguous_project_fields(parameters):
    with pytest.raises(ValidationError):
        TableExecutionCreate.model_validate(request(parameters))


def test_project_selects_reorders_and_renames_without_changing_values():
    projected, mapping = project_table(
        source_table(), request()["parameters"]["fields"], DESCRIPTIONS
    )
    assert projected.column_names == ["f0", "f1"]
    assert [str(field.type) for field in projected.schema] == ["double", "int64"]
    assert math.copysign(1, projected["f0"][0].as_py()) == -1
    assert math.isnan(projected["f0"][1].as_py())
    assert projected["f0"][2].as_py() == math.inf
    assert projected["f1"].to_pylist() == [1, None, 3]
    assert mapping == [
        {
            "ordinal": 0,
            "output_field_id": "f0",
            "source_field_id": "f2",
            "source_name": "原始水位",
            "output_name": "水位",
            "physical_type": "double",
        },
        {
            "ordinal": 1,
            "output_field_id": "f1",
            "source_field_id": "f0",
            "source_name": "编号",
            "output_name": "编号",
            "physical_type": "int64",
        },
    ]
    with pytest.raises(InputError):
        project_table(source_table(), [{"source_field_id": "missing"}], DESCRIPTIONS)


def checker_entry(tmp_path):
    source = source_table()
    parameters = request()["parameters"]
    projected, fields = project_table(source, parameters["fields"], DESCRIPTIONS)
    source_content = parquet_bytes(source)
    source_digest = "sha256:" + hashlib.sha256(source_content).hexdigest()
    source_selection = selection() | {"representation_digest": source_digest}
    report = {
        "schema": "table-project-v1",
        "operator_id": "table-project",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": parameters,
        "row_count": source.num_rows,
        "field_count": len(fields),
        "fields": fields,
    }
    method = {
        "schema": "table-project-v1",
        "operator_id": "table-project",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
    }
    contents = {
        "report": json.dumps(report, sort_keys=True, ensure_ascii=False).encode(),
        "table": parquet_bytes(projected),
        "method": json.dumps(method, sort_keys=True).encode(),
    }
    schemas = {
        "report": "table-project-report-v1",
        "table": "table-project-table-v1",
        "method": "tabular-method-v1",
    }
    formats = {"report": "json", "table": "parquet", "method": "json"}
    files, representations = {}, []
    root = tmp_path / "role"
    root.mkdir()
    for identity, content in contents.items():
        path = root / (identity + ".bin")
        path.write_bytes(content)
        fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()
        files[identity] = {
            "path": f"role/{identity}.bin",
            "digest": fingerprint,
            "format": formats[identity],
            "schema_id": schemas[identity],
        }
        representations.append(
            {
                "representation_id": identity,
                "format": formats[identity],
                "digest": fingerprint,
                "size": len(content),
                "schema_id": schemas[identity],
            }
        )
    source_path = root / "source-table.parquet"
    source_path.write_bytes(source_content)
    descriptor_fields = [
        {
            "field_id": item["output_field_id"],
            "name": item["output_name"],
            "type": item["physical_type"],
            "source_field_id": item["source_field_id"],
        }
        for item in fields
    ]
    manifest = {
        "kind": "OPERATOR_RESULT",
        "result_kind": "OPERATOR_RESULT",
        "representations": representations,
        "tabular": {"fields": descriptor_fields, "row_count": source.num_rows},
        "source": {
            "target": {
                "kind": "OPERATOR",
                "operator_id": "table-project",
                "version_id": "1.0.0",
                "execution_binding": {
                    "kind": "LOCAL",
                    "implementation_id": "table-project-native-v1",
                },
            },
            "inputs": {"table": source_selection},
            "parameters": parameters,
            "provenance": {
                "implementation_id": "table-project-native-v1",
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
            "selection": source_selection,
            "manifest": {
                "kind": "OBSERVATIONS",
                "fields": DESCRIPTIONS,
                "row_count": source.num_rows,
            },
        },
    }


def test_independent_checker_recomputes_projection_and_detects_tampering(tmp_path):
    entry = checker_entry(tmp_path)
    assert reproduce_role(tmp_path, entry, "table-project") is True
    table_path = tmp_path / entry["files"]["table"]["path"]
    altered = pq.read_table(table_path).set_column(1, "f1", pa.array([1, 9, 3], type=pa.int64()))
    pq.write_table(altered, table_path)
    entry["files"]["table"]["digest"] = (
        "sha256:" + hashlib.sha256(table_path.read_bytes()).hexdigest()
    )
    assert reproduce_role(tmp_path, entry, "table-project") is False


def test_checker_capability_matches_only_exact_project_report():
    capability = next(
        item
        for item in build_checker_catalog()["capabilities"]
        if item["capability_id"] == "tabular-project-reproduction-v1"
    )
    expected = {
        "result_kind": "OPERATOR_RESULT",
        "operator_id": "table-project",
        "operator_version": "1.0.0",
        "output_port": "report",
        "schema_id": "table-project-report-v1",
    }
    assert matches(capability, "DATA_VALIDITY", expected)
    assert matches(capability, "RESULT_REPRODUCTION", expected)
    for key, value in (
        ("operator_id", "table-profile"),
        ("operator_version", "2.0.0"),
        ("output_port", "table"),
        ("schema_id", "unknown-v1"),
    ):
        assert not matches(capability, "RESULT_REPRODUCTION", expected | {key: value})


def test_frozen_project_profile_workflow_compiles_with_stable_node_contracts():
    from platform_core.application.workflow_compiler import compile_package

    package = json.loads(
        Path("examples/workflows/table-project-profile-v1.json").read_text(encoding="utf-8")
    )
    compiled = compile_package(package)
    invocations = compiled["invocations"]
    assert set(invocations) == {"project", "profile"}
    assert invocations["project"]["target"]["operator_id"] == "table-project"
    assert invocations["profile"]["target"]["operator_id"] == "table-profile"
