"""Fixed network-free entrypoint for the curated tabular image."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .operators import (
    AggregateFailed,
    CastFailed,
    InputError,
    ValidationFailed,
    aggregate_table,
    cast_table,
    filter_table,
    profile_table,
    project_table,
    validate_table,
)

PROFILE_SCHEMA = pa.schema(
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
)
VALIDATION_SCHEMA = pa.schema(
    [
        ("rule_id", pa.string()),
        ("kind", pa.string()),
        ("status", pa.string()),
        ("checked_count", pa.int64()),
        ("failed_count", pa.int64()),
    ]
)


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def main() -> None:
    signal.alarm(120)
    resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
    source, output = Path("/input"), Path("/output")
    try:
        marker = os.open(output / "started.marker", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(marker)
        request = json.loads((source / "request.json").read_text())
        content = (source / "table.parquet").read_bytes()
        binding = request["bindings"]["table"]
        if "sha256:" + hashlib.sha256(content).hexdigest() != binding["representation"]["digest"]:
            raise InputError("input digest mismatch")
        table = pq.read_table(pa.BufferReader(content))
        operator = request["target"]["operator_id"]
        table_description = binding.get("table", binding.get("manifest", {}))
        if isinstance(table_description.get("tabular"), dict):
            table_description = table_description["tabular"]
        descriptions = table_description.get("fields", [])
        if operator == "table-profile":
            rows = profile_table(table, request["parameters"]["fields"])
            source_names = {
                field["field_id"]: field.get("name", field["field_id"]) for field in descriptions
            }
            for row in rows:
                row["source_name"] = source_names.get(row["field_id"], row["field_id"])
            schema = "table-profile-v1"
            table_schema = PROFILE_SCHEMA
        elif operator == "table-validate":
            rows = validate_table(table, request["parameters"]["rules"])
            schema = "table-validation-v1"
            table_schema = VALIDATION_SCHEMA
        elif operator == "table-project":
            projected, fields = project_table(
                table,
                request["parameters"]["fields"],
                descriptions,
            )
            report = {
                "schema": "table-project-v1",
                "operator_id": operator,
                "version_id": "1.0.0",
                "source_digest": binding["representation"]["digest"],
                "parameters": request["parameters"],
                "row_count": projected.num_rows,
                "field_count": projected.num_columns,
                "fields": fields,
            }
            _write(output / "report.json", report)
            pq.write_table(projected, output / "table.parquet")
            _write(
                output / "method.json",
                {
                    "schema": "table-project-v1",
                    "operator_id": operator,
                    "version_id": "1.0.0",
                    "source_digest": binding["representation"]["digest"],
                    "method": "deterministic-pyarrow-v1",
                },
            )
            for path in output.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            return
        elif operator == "table-filter":
            predicate = request["parameters"]["predicate"]
            filtered, counts = filter_table(table, predicate)
            predicate_digest = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        predicate,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
            report = {
                "schema": "table-filter-v1",
                "operator_id": operator,
                "version_id": "1.0.0",
                "source_digest": binding["representation"]["digest"],
                "parameters": request["parameters"],
                "predicate_digest": predicate_digest,
                "input_row_count": table.num_rows,
                "output_row_count": filtered.num_rows,
                "dropped_row_count": table.num_rows - filtered.num_rows,
                **counts,
            }
            _write(output / "report.json", report)
            pq.write_table(filtered, output / "table.parquet")
            _write(
                output / "method.json",
                {
                    "schema": "table-filter-v1",
                    "operator_id": operator,
                    "version_id": "1.0.0",
                    "source_digest": binding["representation"]["digest"],
                    "method": "deterministic-pyarrow-v1",
                    "predicate_semantics": "kleene-three-valued-v1",
                },
            )
            for path in output.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            return
        elif operator == "table-cast":
            casted, casts = cast_table(table, request["parameters"]["casts"])
            report = {
                "schema": "table-cast-v1",
                "operator_id": operator,
                "version_id": "1.0.0",
                "source_digest": binding["representation"]["digest"],
                "parameters": request["parameters"],
                "row_count": casted.num_rows,
                "cast_count": len(casts),
                "casts": casts,
            }
            _write(output / "report.json", report)
            pq.write_table(casted, output / "table.parquet")
            _write(
                output / "method.json",
                {
                    "schema": "table-cast-v1",
                    "operator_id": operator,
                    "version_id": "1.0.0",
                    "source_digest": binding["representation"]["digest"],
                    "method": "deterministic-pyarrow-v1",
                    "cast_semantics": "strict-scalar-cast-v1",
                },
            )
            for path in output.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            return
        elif operator == "table-aggregate":
            aggregated, fields, measures, counts = aggregate_table(
                table, request["parameters"], descriptions
            )
            report = {
                "schema": "table-aggregate-v1",
                "operator_id": operator,
                "version_id": "1.0.0",
                "source_digest": binding["representation"]["digest"],
                "parameters": request["parameters"],
                **counts,
                "output_row_count": aggregated.num_rows,
                "field_count": aggregated.num_columns,
                "fields": fields,
                "measure_count": len(measures),
                "measures": measures,
            }
            _write(output / "report.json", report)
            pq.write_table(aggregated, output / "table.parquet")
            _write(
                output / "method.json",
                {
                    "schema": "table-aggregate-v1",
                    "operator_id": operator,
                    "version_id": "1.0.0",
                    "source_digest": binding["representation"]["digest"],
                    "method": "deterministic-pyarrow-v1",
                    "aggregation_semantics": "strict-table-aggregate-v1",
                    "numeric_algorithm": "int64-checked-decimal-mean-and-fsum-v1",
                    "group_order": "first-source-occurrence-v1",
                    "null_measure_policy": "ignore-v1",
                },
            )
            for path in output.iterdir():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            return
        else:
            raise InputError("operator is not registered in the tabular image")
        report = {
            "schema": schema,
            "operator_id": operator,
            "version_id": "1.0.0",
            "source_digest": binding["representation"]["digest"],
            "parameters": request["parameters"],
            "row_count": len(rows),
            "rows": rows,
        }
        _write(output / "report.json", report)
        pq.write_table(pa.Table.from_pylist(rows, schema=table_schema), output / "table.parquet")
        _write(
            output / "method.json",
            {
                "schema": schema,
                "operator_id": operator,
                "version_id": "1.0.0",
                "source_digest": binding["representation"]["digest"],
                "method": "deterministic-pyarrow-v1",
            },
        )
        for path in output.iterdir():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    except ValidationFailed as exc:
        _write(output / "error.json", exc.diagnostic)
        raise SystemExit(10) from None
    except CastFailed as exc:
        _write(output / "error.json", exc.diagnostic)
        raise SystemExit(11) from None
    except AggregateFailed as exc:
        _write(output / "error.json", exc.diagnostic)
        raise SystemExit(12) from None
    except (InputError, ValueError, KeyError, pa.ArrowException) as exc:
        _write(output / "error.json", {"code": "INPUT_ERROR", "category": type(exc).__name__})
        raise SystemExit(2) from None
