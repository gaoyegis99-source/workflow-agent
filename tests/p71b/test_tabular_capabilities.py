import hashlib
import io
import json
import math
from copy import deepcopy

import pyarrow as pa
import pyarrow.parquet as pq
from platform_checkers.capabilities import check
from platform_checkers.tabular import profile_rows, validation_rows
from platform_core.application.checker_catalog import build_catalog
from platform_core.application.control import digest
from platform_core.application.open_acceptance import compile_coverage


def registration():
    return {
        "registration_id": "registration",
        "analysis_id": "analysis",
        "revision_id": "analysis-revision",
        "revision": 1,
        "task_spec_revision": 1,
        "observation_watermark": [],
        "watermark_digest": "sha256:" + "9" * 64,
        "policy_decision_id": "policy",
        "registered_at": "2026-01-01T00:00:00+00:00",
    }


def specification(operator, schema):
    return {
        "profile": "open-science-v1",
        "delivery_class": "VERIFIED",
        "requirements": [
            {
                "requirement_id": "tabular-reproduction",
                "description": "Independently reproduce the frozen table result",
                "necessary": True,
                "checks": ["DATA_VALIDITY", "RESULT_REPRODUCTION"],
            }
        ],
        "required_outputs": [
            {
                "role_id": "tabular_report",
                "necessary": True,
                "expected": {
                    "result_kind": "OPERATOR_RESULT",
                    "operator_id": operator,
                    "operator_version": "1.0.0",
                    "output_port": "report",
                    "schema_id": schema,
                },
            }
        ],
        "allow_manual_review": False,
        "max_check_executions": 2,
    }


def parquet_bytes(table):
    stream = io.BytesIO()
    pq.write_table(table, stream)
    return stream.getvalue()


def fixture(tmp_path, operator="table-profile"):
    if operator == "table-profile":
        source = pa.table(
            {
                "f0": pa.array([1, 2, None], type=pa.int64()),
                "f1": pa.array(["", "水", None], type=pa.string()),
                "f2": pa.array([True, False, None], type=pa.bool_()),
                "f3": pa.array([1.5, math.nan, math.inf], type=pa.float64()),
            }
        )
        descriptions = [
            {"field_id": "f0", "name": "count", "type": "int64"},
            {"field_id": "f1", "name": "名称", "type": "string"},
            {"field_id": "f2", "name": "active", "type": "bool"},
            {"field_id": "f3", "name": "value", "type": "double"},
        ]
        parameters = {"fields": ["f0", "f1", "f2", "f3"]}
        rows = profile_rows(source, parameters["fields"], descriptions)
        report_schema, table_schema = "table-profile-v1", "table-profile-report-v1"
        output_table = pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    ("field_id", pa.string()),
                    ("source_name", pa.string()),
                    ("physical_type", pa.string()),
                    ("row_count", pa.int64()),
                    ("null_count", pa.int64()),
                    ("non_null_count", pa.int64()),
                    ("distinct_count", pa.int64()),
                    ("usable_count", pa.int64()),
                    ("nonfinite_count", pa.int64()),
                    ("minimum", pa.string()),
                    ("maximum", pa.string()),
                    ("empty_count", pa.int64()),
                    ("min_length", pa.int64()),
                    ("max_length", pa.int64()),
                    ("true_count", pa.int64()),
                    ("false_count", pa.int64()),
                ]
            ),
        )
        implementation = "table-profile-native-v1"
    else:
        source = pa.table(
            {
                "f0": pa.array([1, 2, 3], type=pa.int64()),
                "f1": pa.array(["A", "B", "A"], type=pa.string()),
                "f2": pa.array([True, False, True], type=pa.bool_()),
            }
        )
        descriptions = [
            {"field_id": "f0", "name": "id", "type": "int64"},
            {"field_id": "f1", "name": "class", "type": "string"},
            {"field_id": "f2", "name": "active", "type": "bool"},
        ]
        parameters = {
            "rules": [
                {"rule_id": "rows", "kind": "ROW_COUNT", "minimum": "3", "maximum": "3"},
                {"rule_id": "type", "kind": "TYPE", "field_id": "f0", "expected_type": "int64"},
                {"rule_id": "nonnull", "kind": "NOT_NULL", "field_id": "f0"},
                {"rule_id": "unique", "kind": "UNIQUE", "field_ids": ["f0"]},
                {
                    "rule_id": "range",
                    "kind": "RANGE",
                    "field_id": "f0",
                    "minimum": "1",
                    "maximum": "3",
                },
                {
                    "rule_id": "allowed",
                    "kind": "ALLOWED_VALUES",
                    "field_id": "f2",
                    "allowed_values": ["true", "false"],
                },
            ]
        }
        rows = validation_rows(source, parameters["rules"])
        report_schema, table_schema = "table-validation-v1", "table-validation-report-v1"
        output_table = pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    ("rule_id", pa.string()),
                    ("kind", pa.string()),
                    ("status", pa.string()),
                    ("checked_count", pa.int64()),
                    ("failed_count", pa.int64()),
                ]
            ),
        )
        implementation = "table-validate-native-v1"

    source_content = parquet_bytes(source)
    source_digest = "sha256:" + hashlib.sha256(source_content).hexdigest()
    source_selection = {
        "kind": "TABLE",
        "asset": {
            "project_id": "project",
            "asset_id": "source",
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "a" * 64,
        },
        "representation_id": "normalized",
        "representation_digest": source_digest,
    }
    report = {
        "schema": report_schema,
        "operator_id": operator,
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": parameters,
        "row_count": len(rows),
        "rows": rows,
    }
    method = {
        "schema": report_schema,
        "operator_id": operator,
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
    }
    contents = {
        "report": json.dumps(report, sort_keys=True, ensure_ascii=False).encode(),
        "table": parquet_bytes(output_table),
        "method": json.dumps(method, sort_keys=True).encode(),
    }
    schemas = {
        "report": table_schema,
        "table": table_schema.replace("report", "table"),
        "method": "tabular-method-v1",
    }
    formats = {"report": "json", "table": "parquet", "method": "json"}
    representations = []
    files = {}
    role_root = tmp_path / "roles" / "tabular_report"
    role_root.mkdir(parents=True)
    for identity, content in contents.items():
        path = role_root / (identity + ".bin")
        path.write_bytes(content)
        fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()
        representations.append(
            {
                "representation_id": identity,
                "format": formats[identity],
                "digest": fingerprint,
                "size": len(content),
                "schema_id": schemas[identity],
            }
        )
        files[identity] = {
            "path": f"roles/tabular_report/{identity}.bin",
            "digest": fingerprint,
            "format": formats[identity],
            "schema_id": schemas[identity],
        }
    source_path = role_root / "source-table.parquet"
    source_path.write_bytes(source_content)
    manifest = {
        "kind": "OPERATOR_RESULT",
        "result_kind": "OPERATOR_RESULT",
        "representations": representations,
        "source": {
            "target": {
                "kind": "OPERATOR",
                "operator_id": operator,
                "version_id": "1.0.0",
                "execution_binding": {"kind": "LOCAL", "implementation_id": implementation},
            },
            "inputs": {"table": source_selection},
            "parameters": parameters,
            "provenance": {
                "implementation_id": implementation,
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
    ref = {
        "project_id": "project",
        "asset_id": "result",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + digest(manifest),
    }
    output_ref = {
        "kind": "OPERATOR_OUTPUT",
        "action_id": "action",
        "execution_id": "execution",
        "output_port": "report",
        "asset_ref": ref,
        "representation_id": "report",
        "representation_digest": files["report"]["digest"],
        "schema_id": table_schema,
        "schema_source": "MANIFEST",
    }
    candidate = {
        "digest": "candidate",
        "spec_revision": 1,
        "identity_mode": "EXACT",
        "payload": {
            "identity_mode": "EXACT",
            "artifacts": [{"ref": ref, "manifest": manifest}],
            "executions": [],
            "workflow_outputs": [],
            "role_bindings": [{"role_id": "tabular_report", "output_ref": output_ref}],
        },
    }
    plan, _ = compile_coverage(
        specification(operator, table_schema),
        candidate,
        [registration()],
        [],
        [],
        [],
        [],
        "registration",
        build_catalog(),
    )
    capability_id = (
        "tabular-profile-reproduction-v1"
        if operator == "table-profile"
        else "tabular-validation-reproduction-v1"
    )
    frozen = next(
        value for value in plan["checker_runs"] if value["capability_id"] == capability_id
    )
    run = frozen | {"run_id": "run", "catalog_digest": plan["checker_catalog"]}
    request = {
        "coverage_plan": plan,
        "checker_run": run,
        "artifacts": candidate["payload"]["artifacts"],
        "role_bindings": candidate["payload"]["role_bindings"],
    }
    index = {
        "roles": {
            "tabular_report": {
                "output_ref": output_ref,
                "manifest": deepcopy(manifest),
                "files": deepcopy(files),
                "source_table": {
                    "path": "roles/tabular_report/source-table.parquet",
                    "digest": source_digest,
                    "selection": deepcopy(source_selection),
                    "manifest": {
                        "kind": "OBSERVATIONS",
                        "fields": descriptions,
                        "row_count": source.num_rows,
                    },
                },
            }
        }
    }
    (tmp_path / "input-index.json").write_text(json.dumps(index), encoding="utf-8")
    return request, index


def test_catalog_matches_only_exact_tabular_operator_port_and_schema(tmp_path):
    request, _ = fixture(tmp_path)
    assert [run["capability_id"] for run in request["coverage_plan"]["checker_runs"]] == [
        "platform-runtime-integrity-v1",
        "tabular-profile-reproduction-v1",
    ]
    assert request["checker_run"]["depends_on"] == ["platform-runtime-integrity-v1"]
    assert len(request["checker_run"]["item_ids"]) == 2

    for field, wrong in (
        ("operator_version", "2.0.0"),
        ("output_port", "table"),
        ("schema_id", "unknown-v1"),
    ):
        spec = specification("table-profile", "table-profile-report-v1")
        spec["required_outputs"][0]["expected"][field] = wrong
        plan, _ = compile_coverage(
            spec,
            {
                "digest": "candidate",
                "spec_revision": 1,
                "identity_mode": "EXACT",
                "payload": {
                    "identity_mode": "EXACT",
                    "artifacts": request["artifacts"],
                    "executions": [],
                    "workflow_outputs": [],
                    "role_bindings": request["role_bindings"],
                },
            },
            [registration()],
            [],
            [],
            [],
            [],
            "registration",
            build_catalog(),
        )
        assert any(item["coverage_status"] == "UNCOVERED" for item in plan["items"])
        assert all(
            run["capability_id"] != "tabular-profile-reproduction-v1"
            for run in plan["checker_runs"]
        )


def test_tabular_claim_support_remains_uncovered_and_indeterminate(tmp_path):
    request, _ = fixture(tmp_path)
    spec = specification("table-profile", "table-profile-report-v1")
    spec["requirements"].append(
        {
            "requirement_id": "unsupported-claim",
            "description": "No tabular claim checker exists in this release",
            "necessary": True,
            "checks": ["CLAIM_SUPPORT"],
        }
    )
    plan, _ = compile_coverage(
        spec,
        {
            "digest": "candidate",
            "spec_revision": 1,
            "identity_mode": "EXACT",
            "payload": {
                "identity_mode": "EXACT",
                "artifacts": request["artifacts"],
                "executions": [],
                "workflow_outputs": [],
                "role_bindings": request["role_bindings"],
            },
        },
        [registration()],
        [],
        [],
        [],
        [],
        "registration",
        build_catalog(),
    )
    unsupported = [item for item in plan["items"] if item["check_type"] == "CLAIM_SUPPORT"]
    assert unsupported
    assert {item["coverage_status"] for item in unsupported} == {"UNCOVERED"}
    assert {item["initial_status"] for item in unsupported} == {"INDETERMINATE"}


def test_profile_and_validation_independent_reproduction_pass(tmp_path):
    for operator in ("table-profile", "table-validate"):
        root = tmp_path / operator
        root.mkdir()
        request, _ = fixture(root, operator)
        report = check(request, root)
        assert report["status"] == "PASS", report
        assert {item["status"] for item in report["items"]} == {"PASS"}


def test_profile_tampering_and_validation_false_success_fail(tmp_path):
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    request, index = fixture(profile_root)
    report_path = profile_root / index["roles"]["tabular_report"]["files"]["report"]["path"]
    value = json.loads(report_path.read_text())
    value["rows"][0]["null_count"] += 1
    report_path.write_text(json.dumps(value))
    index["roles"]["tabular_report"]["files"]["report"]["digest"] = (
        "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
    )
    (profile_root / "input-index.json").write_text(json.dumps(index))
    assert check(request, profile_root)["status"] == "FAIL"

    validation_root = tmp_path / "validation"
    validation_root.mkdir()
    request, index = fixture(validation_root, "table-validate")
    source_path = validation_root / index["roles"]["tabular_report"]["source_table"]["path"]
    source = pq.read_table(source_path).set_column(0, "f0", pa.array([1, 1, 3], type=pa.int64()))
    pq.write_table(source, source_path)
    changed = "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_entry = index["roles"]["tabular_report"]["source_table"]
    source_entry["digest"] = changed
    source_entry["selection"]["representation_digest"] = changed
    manifest = index["roles"]["tabular_report"]["manifest"]
    manifest["source"]["inputs"]["table"]["representation_digest"] = changed
    manifest["source"]["provenance"]["input_digests"]["table"] = changed
    (validation_root / "input-index.json").write_text(json.dumps(index))
    assert check(request, validation_root)["status"] == "FAIL"


def test_reference_semantics_cover_nan_infinity_null_and_six_rules():
    table = pa.table(
        {
            "f0": pa.array([0.0, -0.0, math.nan, math.nan, math.inf, -math.inf, None]),
            "f1": pa.array(["", "水", None, "abc", "水", "", "z"]),
            "f2": pa.array([True, False, None, True, False, True, False]),
        }
    )
    rows = profile_rows(
        table,
        ["f0", "f1", "f2"],
        [
            {"field_id": "f0", "name": "number", "type": "double"},
            {"field_id": "f1", "name": "text", "type": "string"},
            {"field_id": "f2", "name": "flag", "type": "bool"},
        ],
    )
    assert rows[0]["distinct_count"] == 4
    assert rows[0]["usable_count"] == 2 and rows[0]["nonfinite_count"] == 4
    assert rows[1]["empty_count"] == 2 and rows[1]["max_length"] == 3
    assert (rows[2]["true_count"], rows[2]["false_count"]) == (3, 3)

    rules = [
        {"rule_id": "rows", "kind": "ROW_COUNT", "minimum": "7"},
        {"rule_id": "type", "kind": "TYPE", "field_id": "f1", "expected_type": "string"},
        {"rule_id": "null", "kind": "NOT_NULL", "field_id": "f2"},
        {"rule_id": "unique", "kind": "UNIQUE", "field_ids": ["f1"]},
        {"rule_id": "range", "kind": "RANGE", "field_id": "f0", "minimum": "-1", "maximum": "1"},
        {
            "rule_id": "allowed",
            "kind": "ALLOWED_VALUES",
            "field_id": "f2",
            "allowed_values": ["true"],
        },
    ]
    checked = {row["rule_id"]: row for row in validation_rows(table, rules)}
    assert checked["rows"]["status"] == checked["type"]["status"] == "PASS"
    assert checked["null"]["failed_count"] == 1
    assert checked["unique"]["failed_count"] == 4
    assert checked["range"]["failed_count"] == 4
    assert checked["allowed"]["failed_count"] == 3


def test_checker_source_has_no_production_tabular_import():
    import inspect

    import platform_checkers.tabular as reference

    source = inspect.getsource(reference)
    assert "platform_tabular_operators" not in source
