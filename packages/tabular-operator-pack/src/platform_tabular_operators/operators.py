"""Deterministic profiling and validation over a bounded Arrow table."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation

import pyarrow as pa
import pyarrow.compute as pc

SUPPORTED = {"string", "int64", "double", "bool"}
INTEGER = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")
FILTER_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
UNKNOWN, FALSE, TRUE = -1, 0, 1


class InputError(ValueError):
    pass


class ValidationFailed(Exception):
    def __init__(self, diagnostic):
        super().__init__("table validation failed")
        self.diagnostic = diagnostic


class CastFailed(Exception):
    def __init__(self, diagnostic):
        super().__init__("table cast failed")
        self.diagnostic = diagnostic


class AggregateFailed(Exception):
    def __init__(self, diagnostic):
        super().__init__("table aggregation failed")
        self.diagnostic = diagnostic


class _CastValueError(ValueError):
    pass


def physical_type(field: pa.Field) -> str:
    value = str(field.type)
    if value not in SUPPORTED:
        raise InputError("unsupported field type")
    return value


def checked_table(table: pa.Table) -> dict[str, str]:
    if table.num_rows > 100_000 or table.num_columns > 128:
        raise InputError("table exceeds pilot limits")
    if len(set(table.column_names)) != table.num_columns:
        raise InputError("field names must be unique")
    return {field.name: physical_type(field) for field in table.schema}


def project_table(table: pa.Table, fields: list[dict], descriptions: list[dict]):
    """Select and reorder columns without changing any Arrow scalar value."""

    types = checked_table(table)
    if not fields or len(fields) > 128:
        raise InputError("project fields must be a non-empty bounded list")
    source_ids = [item.get("source_field_id") for item in fields]
    if len(set(source_ids)) != len(source_ids) or not set(source_ids) <= set(types):
        raise InputError("project references duplicate or unknown source fields")
    source_names = {
        item.get("field_id"): item.get("name", item.get("field_id")) for item in descriptions
    }
    rows = []
    arrays = []
    output_names = []
    for ordinal, item in enumerate(fields):
        source_id = item["source_field_id"]
        output_id = f"f{ordinal}"
        source_name = source_names.get(source_id, source_id)
        output_name = item.get("display_name") or source_name
        if not isinstance(output_name, str):
            raise InputError("project display name is invalid")
        rows.append(
            {
                "ordinal": ordinal,
                "output_field_id": output_id,
                "source_field_id": source_id,
                "source_name": source_name,
                "output_name": output_name,
                "physical_type": types[source_id],
            }
        )
        arrays.append(table[source_id])
        output_names.append(output_id)
    return pa.Table.from_arrays(arrays, names=output_names), rows


def _filter_literal(value: str, kind: str):
    if not isinstance(value, str) or len(value) > 1000:
        raise InputError("filter literal is invalid")
    if kind == "string":
        if unicodedata.normalize("NFC", value) != value:
            raise InputError("filter string literal must use NFC")
        return value
    if kind == "bool":
        if value not in {"true", "false"}:
            raise InputError("filter boolean literal is invalid")
        return value == "true"
    if kind == "int64":
        return _integer(value)
    if kind == "double":
        if not FILTER_NUMBER.fullmatch(value):
            raise InputError("filter double literal is not a JSON number")
        return _double(value)
    raise InputError("filter literal type is unsupported")


def _filter_shape(node: dict) -> set[str]:
    return {
        name
        for name, present in {
            "field_id": node.get("field_id") is not None,
            "operator": node.get("operator") is not None,
            "value_type": node.get("value_type") is not None,
            "value": node.get("value") is not None,
            "values": node.get("values") is not None,
            "arguments": bool(node.get("arguments")),
        }.items()
        if present
    }


def _compile_filter(predicate: dict, types: dict[str, str]):
    if not isinstance(predicate, dict) or predicate.get("semantics") != "kleene-three-valued-v1":
        raise InputError("filter semantics are unsupported")
    nodes = predicate.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 128:
        raise InputError("filter node list is invalid")
    identities, available, references, depths, compiled = [], set(), {}, {}, []
    shapes = {
        "COMPARE": {"field_id", "operator", "value_type", "value"},
        "IN": {"field_id", "value_type", "values"},
        "IS_NULL": {"field_id"},
        "IS_NAN": {"field_id"},
        "IS_FINITE": {"field_id"},
        "NOT": {"arguments"},
        "AND": {"arguments"},
        "OR": {"arguments"},
    }
    for node in nodes:
        if not isinstance(node, dict):
            raise InputError("filter node is invalid")
        identity, kind = node.get("node_id"), node.get("kind")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in available
            or kind not in shapes
        ):
            raise InputError("filter node identity or kind is invalid")
        if _filter_shape(node) != shapes[kind]:
            raise InputError("filter node fields differ from its kind")
        arguments = node.get("arguments") or []
        if (
            not isinstance(arguments, list)
            or len(set(arguments)) != len(arguments)
            or not set(arguments) <= available
        ):
            raise InputError("filter arguments must be unique earlier nodes")
        if kind == "NOT" and len(arguments) != 1:
            raise InputError("filter NOT requires one argument")
        if kind in {"AND", "OR"} and not 2 <= len(arguments) <= 16:
            raise InputError("filter logical node arity is invalid")
        if kind not in {"NOT", "AND", "OR"} and arguments:
            raise InputError("filter leaf cannot have arguments")
        field = node.get("field_id")
        if field is not None and field not in types:
            raise InputError("filter references an unknown field")
        value_type = node.get("value_type")
        parsed = None
        if kind == "COMPARE":
            if value_type != types[field]:
                raise InputError("filter comparison type differs from its field")
            operator = node.get("operator")
            if operator not in {"EQ", "NE", "LT", "LTE", "GT", "GTE"}:
                raise InputError("filter comparison operator is invalid")
            if value_type == "bool" and operator not in {"EQ", "NE"}:
                raise InputError("filter boolean comparison is unordered")
            parsed = _filter_literal(node.get("value"), value_type)
        elif kind == "IN":
            if value_type != types[field]:
                raise InputError("filter set type differs from its field")
            values = node.get("values")
            if not isinstance(values, list) or not 1 <= len(values) <= 64:
                raise InputError("filter set is invalid")
            parsed = [_filter_literal(value, value_type) for value in values]
            if len(set(parsed)) != len(parsed):
                raise InputError("filter set values are not unique")
            parsed = frozenset(parsed)
        elif kind in {"IS_NAN", "IS_FINITE"} and types[field] != "double":
            raise InputError("filter non-finite predicate requires a double field")
        for argument in arguments:
            references[argument] += 1
        depth = 1 + max((depths[argument] for argument in arguments), default=0)
        if depth > 8:
            raise InputError("filter predicate exceeds depth eight")
        references[identity] = 0
        depths[identity] = depth
        identities.append(identity)
        available.add(identity)
        compiled.append((identity, kind, field, node.get("operator"), parsed, arguments))
    root = predicate.get("root_id")
    if (
        root != identities[-1]
        or references[root] != 0
        or any(count != 1 for identity, count in references.items() if identity != root)
    ):
        raise InputError("filter nodes do not form one rooted tree")
    return compiled


def _compare(value, operator: str, expected):
    if value is None or isinstance(value, float) and math.isnan(value):
        return UNKNOWN
    return (
        TRUE
        if {
            "EQ": value == expected,
            "NE": value != expected,
            "LT": value < expected,
            "LTE": value <= expected,
            "GT": value > expected,
            "GTE": value >= expected,
        }[operator]
        else FALSE
    )


def _and(values):
    if FALSE in values:
        return FALSE
    return UNKNOWN if UNKNOWN in values else TRUE


def _or(values):
    if TRUE in values:
        return TRUE
    return UNKNOWN if UNKNOWN in values else FALSE


def filter_table(table: pa.Table, predicate: dict):
    """Apply a bounded predicate with explicit Kleene three-valued semantics."""

    types = checked_table(table)
    compiled = _compile_filter(predicate, types)
    columns = {name: table[name].to_pylist() for name in types}
    mask, counts = [], {"true_count": 0, "false_count": 0, "unknown_count": 0}
    for row in range(table.num_rows):
        results = {}
        for identity, kind, field, operator, parsed, arguments in compiled:
            value = columns[field][row] if field is not None else None
            if kind == "COMPARE":
                result = _compare(value, operator, parsed)
            elif kind == "IN":
                result = (
                    UNKNOWN
                    if value is None or isinstance(value, float) and math.isnan(value)
                    else TRUE
                    if value in parsed
                    else FALSE
                )
            elif kind == "IS_NULL":
                result = TRUE if value is None else FALSE
            elif kind == "IS_NAN":
                result = (
                    UNKNOWN
                    if value is None
                    else TRUE
                    if isinstance(value, float) and math.isnan(value)
                    else FALSE
                )
            elif kind == "IS_FINITE":
                result = UNKNOWN if value is None else TRUE if math.isfinite(value) else FALSE
            elif kind == "NOT":
                child = results[arguments[0]]
                result = UNKNOWN if child == UNKNOWN else TRUE if child == FALSE else FALSE
            elif kind == "AND":
                result = _and([results[argument] for argument in arguments])
            else:
                result = _or([results[argument] for argument in arguments])
            results[identity] = result
        outcome = results[predicate["root_id"]]
        counts[{TRUE: "true_count", FALSE: "false_count", UNKNOWN: "unknown_count"}[outcome]] += 1
        mask.append(outcome == TRUE)
    return table.filter(pa.array(mask, type=pa.bool_())), counts


def _cast_scalar(value, source_type: str, target_type: str):
    if target_type == "string":
        if source_type == "bool":
            return "true" if value else "false"
        if source_type == "int64":
            return str(value)
        if source_type == "double" and math.isfinite(value):
            return json.dumps(value, allow_nan=False, separators=(",", ":"))
        raise _CastValueError("value has no canonical string representation")
    if target_type == "int64":
        if source_type == "string":
            try:
                return _integer(value)
            except InputError as exc:
                raise _CastValueError("string is not a canonical int64") from exc
        if source_type == "double":
            if math.isfinite(value) and value.is_integer():
                result = int(value)
                if -(2**63) <= result <= 2**63 - 1:
                    return result
            raise _CastValueError("double is not an in-range integer")
        if source_type == "bool":
            return int(value)
    if target_type == "double":
        if source_type == "string":
            if not FILTER_NUMBER.fullmatch(value):
                raise _CastValueError("string is not a finite JSON number")
            try:
                return _double(value)
            except InputError as exc:
                raise _CastValueError("string is not a finite double") from exc
        if source_type == "int64":
            result = float(value)
            if math.isfinite(result) and int(result) == value:
                return result
            raise _CastValueError("int64 is not exactly representable as double")
        if source_type == "bool":
            return 1.0 if value else 0.0
    if target_type == "bool":
        if source_type == "string" and value in {"true", "false"}:
            return value == "true"
        if source_type == "int64" and value in {0, 1}:
            return bool(value)
        if source_type == "double" and math.isfinite(value) and value in {0.0, 1.0}:
            return bool(value)
        raise _CastValueError("value is not the canonical boolean encoding")
    raise _CastValueError("conversion is not supported")


def cast_table(table: pa.Table, casts: list[dict]):
    """Apply explicit, lossless-or-declared-null scalar conversions without changing rows."""

    types = checked_table(table)
    if not isinstance(casts, list) or not 1 <= len(casts) <= 128:
        raise InputError("casts must be a non-empty bounded list")
    identities = [item.get("field_id") for item in casts if isinstance(item, dict)]
    if len(identities) != len(casts) or len(set(identities)) != len(identities):
        raise InputError("cast fields must be unique records")
    supported = {"string", "int64", "double", "bool"}
    plans = {}
    for item in casts:
        if set(item) != {"field_id", "target_type", "on_error"}:
            raise InputError("cast fields differ from the fixed contract")
        identity, target, policy = item["field_id"], item["target_type"], item["on_error"]
        source = types.get(identity)
        if (
            source not in supported
            or target not in supported
            or source == target
            or policy not in {"ERROR", "SET_NULL"}
        ):
            raise InputError("cast source, target or failure policy is invalid")
        plans[identity] = (source, target, policy)

    arrays, summary_by_id, failures = [], {}, []
    arrow_types = {
        "string": pa.string(),
        "int64": pa.int64(),
        "double": pa.float64(),
        "bool": pa.bool_(),
    }
    for field in table.schema:
        identity = field.name
        if identity not in plans:
            arrays.append(table[identity])
            continue
        source, target, policy = plans[identity]
        values, converted = table[identity].to_pylist(), []
        input_nulls = failed = 0
        for value in values:
            if value is None:
                input_nulls += 1
                converted.append(None)
                continue
            try:
                converted.append(_cast_scalar(value, source, target))
            except _CastValueError:
                failed += 1
                converted.append(None)
        summary = {
            "field_id": identity,
            "source_type": source,
            "target_type": target,
            "on_error": policy,
            "input_null_count": input_nulls,
            "converted_count": len(values) - input_nulls - failed,
            "failure_count": failed,
            "output_null_count": input_nulls + failed,
        }
        summary_by_id[identity] = summary
        if failed and policy == "ERROR":
            failures.append(
                {
                    key: summary[key]
                    for key in ("field_id", "source_type", "target_type", "failure_count")
                }
            )
        arrays.append(pa.array(converted, type=arrow_types[target]))
    if failures:
        raise CastFailed(
            {
                "code": "CAST_FAILED",
                "status": "INVALID",
                "cast_count": len(casts),
                "failed_cast_count": len(failures),
                "violation_count": sum(item["failure_count"] for item in failures),
                "failures": failures,
            }
        )
    summaries = [summary_by_id[item["field_id"]] for item in casts]
    return pa.Table.from_arrays(arrays, names=table.column_names), summaries


def _aggregate_failure(failures):
    raise AggregateFailed(
        {
            "code": "AGGREGATION_FAILED",
            "status": "INVALID",
            "failure_count": len(failures),
            "violation_count": sum(item["count"] for item in failures),
            "failures": failures,
        }
    )


def aggregate_table(table: pa.Table, parameters: dict, descriptions: list[dict]):
    """Group a bounded table with frozen null, ordering and numeric semantics."""

    types = checked_table(table)
    if (
        not isinstance(parameters, dict)
        or parameters.get("semantics") != "strict-table-aggregate-v1"
    ):
        raise InputError("aggregate semantics are unsupported")
    group_by = parameters.get("group_by")
    measures = parameters.get("measures")
    policy = parameters.get("null_key_policy")
    if (
        not isinstance(group_by, list)
        or len(group_by) > 16
        or len(set(group_by)) != len(group_by)
        or any(types.get(identity) not in {"string", "int64", "bool"} for identity in group_by)
        or policy not in {"GROUP", "DROP", "ERROR"}
        or not isinstance(measures, list)
        or not 1 <= len(measures) <= 64
    ):
        raise InputError("aggregate grouping or measure list is invalid")
    operations = {"COUNT_ROWS", "COUNT_NON_NULL", "COUNT_DISTINCT", "MIN", "MAX", "SUM", "MEAN"}
    measure_ids, plans = [], []
    for item in measures:
        if not isinstance(item, dict) or not {"measure_id", "operation"} <= set(item):
            raise InputError("aggregate measure is invalid")
        if not set(item) <= {"measure_id", "operation", "field_id", "display_name"}:
            raise InputError("aggregate measure fields differ from the contract")
        identity, operation, field_id = (
            item.get("measure_id"),
            item.get("operation"),
            item.get("field_id"),
        )
        if not isinstance(identity, str) or not identity or operation not in operations:
            raise InputError("aggregate measure identity or operation is invalid")
        if operation == "COUNT_ROWS":
            if field_id is not None:
                raise InputError("COUNT_ROWS must omit field_id")
            source_type, output_type = None, "int64"
        else:
            source_type = types.get(field_id)
            if source_type is None:
                raise InputError("aggregate measure references an unknown field")
            if operation in {"SUM", "MEAN"} and source_type not in {"int64", "double"}:
                raise InputError("SUM and MEAN require numeric fields")
            output_type = (
                "int64"
                if operation in {"COUNT_NON_NULL", "COUNT_DISTINCT"}
                else "double"
                if operation == "MEAN"
                else source_type
            )
        measure_ids.append(identity)
        plans.append((item, source_type, output_type))
    if len(set(measure_ids)) != len(measure_ids):
        raise InputError("aggregate measure identifiers must be unique")

    columns = {
        name: table[name].to_pylist()
        for name in set(group_by)
        | {item.get("field_id") for item in measures if item.get("field_id")}
    }
    groups: dict[tuple, list[int]] = {}
    participating = []
    dropped = 0
    null_failures = []
    null_counts = {identity: 0 for identity in group_by}
    for row in range(table.num_rows):
        key = tuple(columns[identity][row] for identity in group_by)
        if any(value is None for value in key):
            for identity, value in zip(group_by, key, strict=True):
                if value is None:
                    null_counts[identity] += 1
            if policy == "DROP":
                dropped += 1
                continue
            if policy == "ERROR":
                continue
        participating.append(row)
        groups.setdefault(key, []).append(row)
    if policy == "ERROR":
        null_failures = [
            {
                "scope": "GROUP_KEY",
                "measure_id": None,
                "field_id": identity,
                "category": "NULL_GROUP_KEY",
                "count": count,
            }
            for identity, count in null_counts.items()
            if count
        ]
        if null_failures:
            _aggregate_failure(null_failures)
    if not group_by and not groups:
        groups[()] = []

    nonfinite_failures = []
    for item, source_type, _ in plans:
        if source_type != "double":
            continue
        values = columns[item["field_id"]]
        count = sum(
            value is not None and not math.isfinite(value)
            for row in participating
            if (value := values[row]) is not None
        )
        if count:
            nonfinite_failures.append(
                {
                    "scope": "MEASURE",
                    "measure_id": item["measure_id"],
                    "field_id": item["field_id"],
                    "category": "NONFINITE_VALUE",
                    "count": count,
                }
            )
    if nonfinite_failures:
        _aggregate_failure(nonfinite_failures)

    output_columns = [[] for _ in range(len(group_by) + len(plans))]
    for key, rows in groups.items():
        for index, value in enumerate(key):
            output_columns[index].append(value)
        for offset, (item, source_type, _) in enumerate(plans, start=len(group_by)):
            operation, field_id = item["operation"], item.get("field_id")
            values = (
                [columns[field_id][row] for row in rows if columns[field_id][row] is not None]
                if field_id is not None
                else []
            )
            if operation == "COUNT_ROWS":
                result = len(rows)
            elif operation == "COUNT_NON_NULL":
                result = len(values)
            elif operation == "COUNT_DISTINCT":
                result = len(set(values))
            elif not values:
                result = None
            elif operation == "MIN":
                result = min(values)
            elif operation == "MAX":
                result = max(values)
            elif operation == "SUM" and source_type == "int64":
                result = sum(values)
                if not -(2**63) <= result <= 2**63 - 1:
                    _aggregate_failure(
                        [
                            {
                                "scope": "MEASURE",
                                "measure_id": item["measure_id"],
                                "field_id": field_id,
                                "category": "NUMERIC_OVERFLOW",
                                "count": 1,
                            }
                        ]
                    )
            elif operation == "SUM":
                try:
                    result = math.fsum(values)
                except OverflowError:
                    result = math.inf
                if not math.isfinite(result):
                    _aggregate_failure(
                        [
                            {
                                "scope": "MEASURE",
                                "measure_id": item["measure_id"],
                                "field_id": field_id,
                                "category": "NUMERIC_OVERFLOW",
                                "count": 1,
                            }
                        ]
                    )
            elif operation == "MEAN" and source_type == "int64":
                result = float(Decimal(sum(values)) / Decimal(len(values)))
            elif operation == "MEAN":
                try:
                    result = math.fsum(values) / len(values)
                except OverflowError:
                    result = math.inf
                if not math.isfinite(result):
                    _aggregate_failure(
                        [
                            {
                                "scope": "MEASURE",
                                "measure_id": item["measure_id"],
                                "field_id": field_id,
                                "category": "NUMERIC_OVERFLOW",
                                "count": 1,
                            }
                        ]
                    )
            else:
                raise InputError("aggregate operation is unsupported")
            output_columns[offset].append(result)

    arrow_types = {
        "string": pa.string(),
        "int64": pa.int64(),
        "double": pa.float64(),
        "bool": pa.bool_(),
    }
    description_by_id = {
        item.get("field_id"): item for item in descriptions if isinstance(item, dict)
    }
    fields = []
    for ordinal, identity in enumerate(group_by):
        description = description_by_id.get(identity, {})
        fields.append(
            {
                "field_id": f"f{ordinal}",
                "name": description.get("name", identity),
                "type": types[identity],
                "source_kind": "GROUP_KEY",
                "source_field_id": identity,
            }
        )
    summaries = []
    for index, (item, source_type, output_type) in enumerate(plans):
        ordinal = len(group_by) + index
        field_id = item.get("field_id")
        values = columns[field_id] if field_id is not None else None
        nonnull = (
            len(participating)
            if values is None
            else sum(values[row] is not None for row in participating)
        )
        summary = {
            "measure_id": item["measure_id"],
            "operation": item["operation"],
            "source_field_id": field_id,
            "source_type": source_type,
            "output_field_id": f"f{ordinal}",
            "output_type": output_type,
            "input_value_count": nonnull,
            "null_value_count": 0 if values is None else len(participating) - nonnull,
            "nonfinite_count": 0,
        }
        summaries.append(summary)
        fields.append(
            {
                "field_id": f"f{ordinal}",
                "name": item.get("display_name") or item["measure_id"],
                "type": output_type,
                "source_kind": "MEASURE",
                "source_field_id": field_id,
                "measure_id": item["measure_id"],
                "operation": item["operation"],
            }
        )
    arrays = [
        pa.array(values, type=arrow_types[field["type"]])
        for values, field in zip(output_columns, fields, strict=True)
    ]
    result = pa.Table.from_arrays(arrays, names=[field["field_id"] for field in fields])
    return (
        result,
        fields,
        summaries,
        {
            "input_row_count": table.num_rows,
            "participating_row_count": len(participating),
            "dropped_key_row_count": dropped,
            "group_count": len(groups),
        },
    )


def _number_text(value) -> str:
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise InputError("non-finite value has no canonical number")
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def profile_table(table: pa.Table, fields: list[str]) -> list[dict]:
    types = checked_table(table)
    if not fields or len(fields) > 128 or len(set(fields)) != len(fields):
        raise InputError("profile fields must be a unique non-empty list")
    if not set(fields) <= set(types):
        raise InputError("profile references an unknown field")
    rows = []
    for identity in fields:
        values = table[identity].to_pylist()
        kind = types[identity]
        nonnull = [value for value in values if value is not None]
        finite = (
            [value for value in nonnull if math.isfinite(value)] if kind == "double" else nonnull
        )
        row = {
            "field_id": identity,
            "source_name": identity,
            "physical_type": kind,
            "row_count": len(values),
            "null_count": len(values) - len(nonnull),
            "non_null_count": len(nonnull),
            # Arrow gives deterministic SQL-like null and NaN semantics without
            # materialising or exposing any of the distinct values.
            "distinct_count": pc.count_distinct(table[identity]).as_py(),
            "usable_count": len(finite),
            "nonfinite_count": len(nonnull) - len(finite),
            "minimum": None,
            "maximum": None,
            "empty_count": None,
            "min_length": None,
            "max_length": None,
            "true_count": None,
            "false_count": None,
        }
        if kind in {"int64", "double"} and finite:
            row["minimum"] = _number_text(min(finite))
            row["maximum"] = _number_text(max(finite))
        elif kind == "string":
            lengths = [len(value) for value in nonnull]
            row["empty_count"] = sum(value == "" for value in nonnull)
            row["min_length"] = min(lengths) if lengths else None
            row["max_length"] = max(lengths) if lengths else None
        elif kind == "bool":
            row["true_count"] = sum(value is True for value in nonnull)
            row["false_count"] = sum(value is False for value in nonnull)
        rows.append(row)
    return rows


def _integer(value: str) -> int:
    if not INTEGER.fullmatch(value):
        raise InputError("integer bound is not canonical")
    result = int(value)
    if result < -(2**63) or result > 2**63 - 1:
        raise InputError("integer bound exceeds int64")
    return result


def _double(value: str) -> float:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise InputError("double bound is invalid") from exc
    result = float(parsed)
    if not parsed.is_finite() or not math.isfinite(result):
        raise InputError("double bound must be finite")
    if json.loads(value) != result:
        raise InputError("double bound must be a JSON number")
    return result


def _typed(value: str, kind: str):
    if kind == "string":
        if unicodedata.normalize("NFC", value) != value:
            raise InputError("string allowed value must be NFC")
        return value
    if kind == "int64":
        return _integer(value)
    if kind == "double":
        return _double(value)
    if kind == "bool" and value in {"true", "false"}:
        return value == "true"
    raise InputError("allowed value differs from the field type")


def _bounds(rule: dict, kind: str):
    parse = _integer if kind == "int64" else _double
    low = parse(rule["minimum"]) if rule.get("minimum") is not None else None
    high = parse(rule["maximum"]) if rule.get("maximum") is not None else None
    if low is not None and high is not None and low > high:
        raise InputError("minimum exceeds maximum")
    return low, high


def _key_value(value, kind: str):
    """Make every non-null supported scalar comparable for UNIQUE.

    NaN is a value rather than a null in Arrow.  Normalising it here makes two
    NaN keys duplicates while keeping positive and negative infinity distinct.
    """

    if kind == "double":
        if math.isnan(value):
            return ("nan",)
        if value == math.inf:
            return ("positive-infinity",)
        if value == -math.inf:
            return ("negative-infinity",)
    return ("value", value)


def validate_table(table: pa.Table, rules: list[dict]) -> list[dict]:
    types = checked_table(table)
    if not rules or len(rules) > 64 or len({rule.get("rule_id") for rule in rules}) != len(rules):
        raise InputError("validation rules must have unique identifiers")
    results = []
    for rule in rules:
        kind = rule["kind"]
        failed = 0
        checked = table.num_rows
        if kind == "ROW_COUNT":
            low = _integer(rule["minimum"]) if rule.get("minimum") is not None else None
            high = _integer(rule["maximum"]) if rule.get("maximum") is not None else None
            if low is None and high is None:
                raise InputError("row count requires a bound")
            if (low is not None and table.num_rows < low) or (
                high is not None and table.num_rows > high
            ):
                failed = 1
            checked = 1
        elif kind == "UNIQUE":
            fields = rule.get("field_ids") or []
            if not fields or len(fields) > 16 or len(set(fields)) != len(fields):
                raise InputError("unique fields are invalid")
            if not set(fields) <= set(types):
                raise InputError("unique rule references an unknown field")
            columns = [table[field].to_pylist() for field in fields]
            keys = []
            for values in zip(*columns, strict=True):
                if any(value is None for value in values):
                    continue
                keys.append(
                    tuple(
                        _key_value(value, types[field])
                        for field, value in zip(fields, values, strict=True)
                    )
                )
            counts = Counter(keys)
            failed = sum(count for count in counts.values() if count > 1)
            checked = len(keys)
        else:
            field = rule.get("field_id")
            if field not in types:
                raise InputError("rule references an unknown field")
            values = table[field].to_pylist()
            field_type = types[field]
            if kind == "TYPE":
                failed = 0 if field_type == rule.get("expected_type") else 1
                checked = 1
            elif kind == "NOT_NULL":
                failed = sum(value is None for value in values)
            elif kind == "RANGE":
                if field_type not in {"int64", "double"}:
                    raise InputError("range requires a numeric field")
                low, high = _bounds(rule, field_type)
                for value in values:
                    if value is None:
                        continue
                    if isinstance(value, float) and not math.isfinite(value):
                        failed += 1
                    elif (low is not None and value < low) or (high is not None and value > high):
                        failed += 1
            elif kind == "ALLOWED_VALUES":
                allowed = {_typed(value, field_type) for value in rule.get("allowed_values") or []}
                if not allowed:
                    raise InputError("allowed values cannot be empty")
                for value in values:
                    if value is None:
                        continue
                    if (
                        isinstance(value, float) and not math.isfinite(value)
                    ) or value not in allowed:
                        failed += 1
            else:
                raise InputError("unknown validation rule")
        results.append(
            {
                "rule_id": rule["rule_id"],
                "kind": kind,
                "status": "PASS" if failed == 0 else "FAIL",
                "checked_count": checked,
                "failed_count": failed,
            }
        )
    failures = [row for row in results if row["status"] == "FAIL"]
    if failures:
        raise ValidationFailed(
            {
                "code": "VALIDATION_FAILED",
                "status": "INVALID",
                "rule_count": len(results),
                "failed_rule_count": len(failures),
                "violation_count": sum(row["failed_count"] for row in failures),
                "failures": [
                    {key: row[key] for key in ("rule_id", "kind", "failed_count")}
                    for row in failures
                ],
            }
        )
    return results
