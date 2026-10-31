"""Tabular output validation and provenance collection."""

import hashlib
import json
import math

from platform_contracts.service import canonical, ensure

from ._schemas import TABULAR_FILE_SCHEMAS, output_schema
from .protocols import ActionComponent


class TabularOutputCollector(ActionComponent):
    def collect_tabular(self, row, root):
        output = root / "output"
        profile = row["runtime"]["implementation_profile"]
        expected = profile["outputs"]
        names = {path.name for path in output.iterdir() if path.name != "started.marker"}
        ensure(
            names == set(expected.values())
            and all(
                (output / name).is_file() and not (output / name).is_symlink() for name in names
            ),
            "INVALID_BINDING",
            "Tabular output set differs",
        )
        report = json.loads((output / expected["report"]).read_text())
        method = json.loads((output / expected["method"]).read_text())
        ensure(
            self.table_inspector is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Parquet output inspector is unavailable",
            503,
        )
        payload = row["payload"]
        operator = payload["target"]["operator_id"]
        table = self.table_inspector.inspect(
            output / expected["table"],
            rows=operator not in {"table-project", "table-filter", "table-cast"},
        )
        binding = payload["bindings"]["table"]
        file_schema = table["schema"]
        table_description = binding.get("table", binding.get("manifest", {}))
        if isinstance(table_description.get("tabular"), dict):
            table_description = table_description["tabular"]
        source_fields = {item["field_id"]: item for item in table_description.get("fields", [])}
        expected_method = {
            "schema": report.get("schema"),
            "operator_id": operator,
            "version_id": "1.0.0",
            "source_digest": binding["representation"]["digest"],
            "method": "deterministic-pyarrow-v1",
        }
        if operator == "table-filter":
            expected_method["predicate_semantics"] = "kleene-three-valued-v1"
        elif operator == "table-cast":
            expected_method["cast_semantics"] = "strict-scalar-cast-v1"
        elif operator == "table-aggregate":
            expected_method |= {
                "aggregation_semantics": "strict-table-aggregate-v1",
                "numeric_algorithm": "int64-checked-decimal-mean-and-fsum-v1",
                "group_order": "first-source-occurrence-v1",
                "null_measure_policy": "ignore-v1",
            }
        common_valid = bool(
            report.get("operator_id") == operator
            and report.get("version_id") == "1.0.0"
            and report.get("source_digest") == binding["representation"]["digest"]
            and report.get("parameters") == payload["parameters"]
            and method == expected_method
        )
        if operator == "table-project":
            requested = payload["parameters"]["fields"]
            raw_mappings = report.get("fields", [])
            mappings = raw_mappings if isinstance(raw_mappings, list) else []
            mapping_shape_valid = bool(
                isinstance(raw_mappings, list)
                and all(
                    isinstance(item, dict)
                    and {
                        "ordinal",
                        "output_field_id",
                        "source_field_id",
                        "source_name",
                        "output_name",
                        "physical_type",
                    }
                    == set(item)
                    for item in mappings
                )
            )
            fields = [
                {
                    "field_id": item["output_field_id"],
                    "name": item["output_name"],
                    "type": item["physical_type"],
                    "source_field_id": item["source_field_id"],
                }
                for item in mappings
                if isinstance(item, dict)
                and {
                    "ordinal",
                    "output_field_id",
                    "source_field_id",
                    "source_name",
                    "output_name",
                    "physical_type",
                }
                == set(item)
            ]
            project_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "row_count",
                    "field_count",
                    "fields",
                }
                and report.get("schema") == "table-project-v1"
                and report.get("row_count") == table["row_count"]
                and report.get("field_count") == len(requested) == len(mappings)
                and mapping_shape_valid
                and file_schema
                == [(item["output_field_id"], item["physical_type"]) for item in mappings]
                and all(
                    item["ordinal"] == ordinal
                    and item["output_field_id"] == f"f{ordinal}"
                    and item["source_field_id"] == selected["source_field_id"]
                    and item["source_field_id"] in source_fields
                    and item["source_name"]
                    == source_fields[item["source_field_id"]].get("name", item["source_field_id"])
                    and item["output_name"] == (selected.get("display_name") or item["source_name"])
                    and item["physical_type"] == source_fields[item["source_field_id"]]["type"]
                    for ordinal, (selected, item) in enumerate(
                        zip(requested, mappings, strict=True)
                    )
                )
            )
            ensure(
                common_valid and project_valid,
                "INVALID_BINDING",
                "Projected table differs from its frozen request",
            )
        elif operator == "table-filter":
            source_field_list = table_description.get("fields", [])
            predicate_digest = (
                "sha256:"
                + hashlib.sha256(canonical(payload["parameters"]["predicate"])).hexdigest()
            )
            counts = [
                report.get("true_count"),
                report.get("false_count"),
                report.get("unknown_count"),
            ]
            input_rows = table_description.get("row_count")
            filter_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "predicate_digest",
                    "input_row_count",
                    "output_row_count",
                    "dropped_row_count",
                    "true_count",
                    "false_count",
                    "unknown_count",
                }
                and report.get("schema") == "table-filter-v1"
                and report.get("predicate_digest") == predicate_digest
                and type(input_rows) is int
                and report.get("input_row_count") == input_rows
                and report.get("output_row_count") == table["row_count"]
                and report.get("dropped_row_count") == input_rows - table["row_count"]
                and all(type(value) is int and value >= 0 for value in counts)
                and sum(counts) == input_rows
                and report.get("true_count") == table["row_count"]
                and file_schema
                == [(item.get("field_id"), item.get("type")) for item in source_field_list]
            )
            ensure(
                common_valid and filter_valid,
                "INVALID_BINDING",
                "Filtered table differs from its frozen request",
            )
            fields = [dict(item) for item in source_field_list]
        elif operator == "table-cast":
            source_field_list = table_description.get("fields", [])
            requested = payload["parameters"]["casts"]
            expected_fields = [dict(item) for item in source_field_list]
            expected_by_id = {item["field_id"]: item for item in expected_fields}
            for item in requested:
                expected_by_id[item["field_id"]]["type"] = item["target_type"]
            raw_summaries = report.get("casts")
            summaries = raw_summaries if isinstance(raw_summaries, list) else []
            cast_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "row_count",
                    "cast_count",
                    "casts",
                }
                and report.get("schema") == "table-cast-v1"
                and report.get("row_count")
                == table_description.get("row_count")
                == table["row_count"]
                and report.get("cast_count") == len(requested) == len(summaries)
                and file_schema
                == [(item.get("field_id"), item.get("type")) for item in expected_fields]
                and all(
                    isinstance(summary, dict)
                    and set(summary)
                    == {
                        "field_id",
                        "source_type",
                        "target_type",
                        "on_error",
                        "input_null_count",
                        "converted_count",
                        "failure_count",
                        "output_null_count",
                    }
                    and summary["field_id"] == selected["field_id"]
                    and summary["source_type"] == source_fields[selected["field_id"]]["type"]
                    and summary["target_type"] == selected["target_type"]
                    and summary["on_error"] == selected["on_error"]
                    and all(
                        type(summary[key]) is int and summary[key] >= 0
                        for key in (
                            "input_null_count",
                            "converted_count",
                            "failure_count",
                            "output_null_count",
                        )
                    )
                    and summary["input_null_count"]
                    + summary["converted_count"]
                    + summary["failure_count"]
                    == table["row_count"]
                    and summary["output_null_count"]
                    == summary["input_null_count"] + summary["failure_count"]
                    and (selected["on_error"] != "ERROR" or summary["failure_count"] == 0)
                    and table["null_counts"].get(selected["field_id"])
                    == summary["output_null_count"]
                    for selected, summary in zip(requested, summaries, strict=True)
                )
            )
            ensure(
                common_valid and cast_valid,
                "INVALID_BINDING",
                "Cast table differs from its frozen request",
            )
            fields = expected_fields
        elif operator == "table-aggregate":
            parameters = payload["parameters"]
            group_by, requested = parameters["group_by"], parameters["measures"]
            raw_fields, raw_measures = report.get("fields"), report.get("measures")
            fields = raw_fields if isinstance(raw_fields, list) else []
            summaries = raw_measures if isinstance(raw_measures, list) else []
            output_types = []
            expected_fields = []
            for ordinal, identity in enumerate(group_by):
                source = source_fields.get(identity, {})
                expected_fields.append(
                    {
                        "field_id": f"f{ordinal}",
                        "name": source.get("name", identity),
                        "type": source.get("type"),
                        "source_kind": "GROUP_KEY",
                        "source_field_id": identity,
                    }
                )
                output_types.append(source.get("type"))
            expected_summaries = []
            for index, item in enumerate(requested):
                ordinal = len(group_by) + index
                field_id, operation = item.get("field_id"), item["operation"]
                source_type = source_fields.get(field_id, {}).get("type")
                output_type = (
                    "int64"
                    if operation in {"COUNT_ROWS", "COUNT_NON_NULL", "COUNT_DISTINCT"}
                    else "double"
                    if operation == "MEAN"
                    else source_type
                )
                expected_fields.append(
                    {
                        "field_id": f"f{ordinal}",
                        "name": item.get("display_name") or item["measure_id"],
                        "type": output_type,
                        "source_kind": "MEASURE",
                        "source_field_id": field_id,
                        "measure_id": item["measure_id"],
                        "operation": operation,
                    }
                )
                output_types.append(output_type)
                expected_summaries.append(
                    {
                        "measure_id": item["measure_id"],
                        "operation": operation,
                        "source_field_id": field_id,
                        "source_type": source_type,
                        "output_field_id": f"f{ordinal}",
                        "output_type": output_type,
                    }
                )
            input_rows = table_description.get("row_count")
            counts = [
                report.get("input_row_count"),
                report.get("participating_row_count"),
                report.get("dropped_key_row_count"),
                report.get("output_row_count"),
                report.get("group_count"),
                report.get("field_count"),
                report.get("measure_count"),
            ]
            aggregate_valid = bool(
                set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "input_row_count",
                    "participating_row_count",
                    "dropped_key_row_count",
                    "group_count",
                    "output_row_count",
                    "field_count",
                    "fields",
                    "measure_count",
                    "measures",
                }
                and report.get("schema") == "table-aggregate-v1"
                and all(type(value) is int and value >= 0 for value in counts)
                and report.get("input_row_count") == input_rows
                and report.get("participating_row_count") + report.get("dropped_key_row_count")
                == input_rows
                and report.get("group_count")
                == report.get("output_row_count")
                == table["row_count"]
                and table["row_count"] <= max(1, input_rows)
                and report.get("field_count")
                == len(fields)
                == len(file_schema)
                == len(group_by) + len(requested)
                and report.get("measure_count") == len(summaries) == len(requested)
                and fields == expected_fields
                and file_schema
                == [
                    (f"f{index}", physical_type) for index, physical_type in enumerate(output_types)
                ]
                and all(
                    isinstance(summary, dict)
                    and set(summary)
                    == set(expected) | {"input_value_count", "null_value_count", "nonfinite_count"}
                    and all(summary[key] == value for key, value in expected.items())
                    and all(
                        type(summary[key]) is int and summary[key] >= 0
                        for key in ("input_value_count", "null_value_count", "nonfinite_count")
                    )
                    and summary["input_value_count"] + summary["null_value_count"]
                    == report["participating_row_count"]
                    and summary["nonfinite_count"] == 0
                    for summary, expected in zip(summaries, expected_summaries, strict=True)
                )
                and all(
                    not isinstance(value, float) or math.isfinite(value)
                    for result_row in table["rows"]
                    for value in result_row.values()
                    if value is not None
                )
            )
            ensure(
                common_valid and aggregate_valid,
                "INVALID_BINDING",
                "Aggregate table differs from its frozen request",
            )
        else:
            expected_rows = (
                payload["parameters"]["fields"]
                if operator == "table-profile"
                else [item["rule_id"] for item in payload["parameters"]["rules"]]
            )
            rules = {item["rule_id"]: item for item in payload["parameters"].get("rules", [])}
            ensure(
                common_valid
                and set(report)
                == {
                    "schema",
                    "operator_id",
                    "version_id",
                    "source_digest",
                    "parameters",
                    "row_count",
                    "rows",
                }
                and file_schema == TABULAR_FILE_SCHEMAS.get(operator)
                and report.get("row_count") == table["row_count"]
                and report.get("rows") == table["rows"]
                and table["row_count"] == len(expected_rows)
                and [
                    item["field_id"] if operator == "table-profile" else item["rule_id"]
                    for item in report["rows"]
                ]
                == expected_rows
                and (
                    operator != "table-validate"
                    or all(
                        item["status"] == "PASS"
                        and item["failed_count"] == 0
                        and rules[item["rule_id"]]["kind"] == item["kind"]
                        for item in report["rows"]
                    )
                )
                and (
                    operator != "table-profile"
                    or all(
                        item["field_id"] in source_fields
                        and item["source_name"]
                        == source_fields[item["field_id"]].get("name", item["field_id"])
                        and item["physical_type"] == source_fields[item["field_id"]]["type"]
                        for item in report["rows"]
                    )
                )
                and report.get("schema")
                == ("table-profile-v1" if operator == "table-profile" else "table-validation-v1"),
                "INVALID_BINDING",
                "Tabular output differs from its frozen request",
            )
            fields = [
                {"field_id": name, "name": name, "type": physical_type}
                for name, physical_type in file_schema
            ]
        ensure(
            0 < len(fields) <= 128
            and all(field["type"] in {"string", "int64", "double", "bool"} for field in fields),
            "INVALID_BINDING",
            "Tabular output schema is unsupported",
        )
        formats = {"report": "json", "table": "parquet", "method": "json"}
        files = []
        for identity, filename in expected.items():
            content = (output / filename).read_bytes()
            files.append(
                {
                    "representation_id": identity,
                    "filename": filename,
                    "format": formats[identity],
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "schema_id": output_schema(payload, identity),
                }
            )
        ensure(
            sum(item["size"] for item in files) <= 100 * 1024 * 1024,
            "INVALID_BINDING",
            "Tabular output exceeds limit",
        )
        manifest = {
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "project_id": row["project_id"],
            "task_ref": {
                "task_id": row["task_id"],
                "spec_revision": payload["expected_spec_revision"],
            },
            "target": payload["target"],
            "inputs": {"table": binding["selection"]},
            "parameters": payload["parameters"],
            "semantics": {"table": None},
            "files": files,
            "asset_descriptor": {
                "schema_version": 1,
                "kind": "OPERATOR_RESULT",
                "tabular": {"fields": fields, "row_count": table["row_count"]},
            },
            "provenance": {
                "implementation_id": profile["implementation_id"],
                "image_id": profile["image_id"],
                **{
                    key: profile[key]
                    for key in (
                        "catalog_digest",
                        "operator_digest",
                        "implementation_digest",
                        "ownership",
                        "trust_level",
                    )
                    if key in profile
                },
                "input_digests": {"table": binding["representation"]["digest"]},
            },
        }
        if row.get("parent_execution_id"):
            return self.execution.transition(
                row,
                "REPORTING",
                pending_state="SUCCEEDED",
                node_revision=row["revision"] + 1,
                publication="PRIVATE",
                files=files,
                manifest=manifest,
            )
        return self.execution.transition(row, "PUBLISHING", manifest=manifest)
