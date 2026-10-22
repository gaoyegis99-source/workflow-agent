"""Independent reference checks for the curated tabular operator protocol.

This module deliberately does not import the production tabular operator pack.
PyArrow is used only to read the frozen Parquet representations; all statistics
and validation rules are implemented here from the published protocol.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation

import pyarrow as pa
import pyarrow.parquet as pq

PROFILE_SCHEMA = [
    ("field_id", "string"),
    ("source_name", "string"),
    ("physical_type", "string"),
    ("row_count", "int64"),
    ("null_count", "int64"),
    ("non_null_count", "int64"),
    ("distinct_count", "int64"),
    ("usable_count", "int64"),
    ("nonfinite_count", "int64"),
    ("minimum", "string"),
    ("maximum", "string"),
    ("empty_count", "int64"),
    ("min_length", "int64"),
    ("max_length", "int64"),
    ("true_count", "int64"),
    ("false_count", "int64"),
]
VALIDATION_SCHEMA = [
    ("rule_id", "string"),
    ("kind", "string"),
    ("status", "string"),
    ("checked_count", "int64"),
    ("failed_count", "int64"),
]
OUTPUT_SCHEMAS = {
    "table-profile": {
        "report": "table-profile-report-v1",
        "table": "table-profile-table-v1",
        "method": "tabular-method-v1",
    },
    "table-validate": {
        "report": "table-validation-report-v1",
        "table": "table-validation-table-v1",
        "method": "tabular-method-v1",
    },
    "table-project": {
        "report": "table-project-report-v1",
        "table": "table-project-table-v1",
        "method": "tabular-method-v1",
    },
    "table-filter": {
        "report": "table-filter-report-v1",
        "table": "table-filter-table-v1",
        "method": "tabular-method-v1",
    },
    "table-cast": {
        "report": "table-cast-report-v1",
        "table": "table-cast-table-v1",
        "method": "tabular-method-v1",
    },
    "table-aggregate": {
        "report": "table-aggregate-report-v1",
        "table": "table-aggregate-table-v1",
        "method": "tabular-method-v1",
    },
}
IMPLEMENTATIONS = {
    "table-profile": "table-profile-native-v1",
    "table-validate": "table-validate-native-v1",
    "table-project": "table-project-native-v1",
    "table-filter": "table-filter-native-v1",
    "table-cast": "table-cast-native-v1",
    "table-aggregate": "table-aggregate-native-v1",
}
INTEGER = re.compile(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z")
FILTER_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
UNKNOWN, FALSE, TRUE = -1, 0, 1


def _bytes(root, entry):
    path = (root / entry["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
        raise ValueError("Tabular checker input path is invalid")
    content = path.read_bytes()
    if "sha256:" + hashlib.sha256(content).hexdigest() != entry["digest"]:
        raise ValueError("Tabular checker input digest differs")
    return content


def _schema(table):
    return [(field.name, str(field.type)) for field in table.schema]


def _distinct(value, kind):
    if kind == "double":
        if math.isnan(value):
            return ("nan",)
        if value == math.inf:
            return ("positive-infinity",)
        if value == -math.inf:
            return ("negative-infinity",)
        # Python equality intentionally gives -0.0 and 0.0 one identity.
        return ("finite", value)
    return (kind, value)


def _number_text(value):
    return str(value) if isinstance(value, int) else json.dumps(value, allow_nan=False)


def profile_rows(table, fields, descriptions):
    types = {field.name: str(field.type) for field in table.schema}
    if not fields or len(fields) != len(set(fields)) or not set(fields) <= set(types):
        raise ValueError("Invalid profile field selection")
    names = {field["field_id"]: field.get("name", field["field_id"]) for field in descriptions}
    rows = []
    for identity in fields:
        kind = types[identity]
        values = table[identity].to_pylist()
        nonnull = [value for value in values if value is not None]
        finite = (
            [value for value in nonnull if math.isfinite(value)] if kind == "double" else nonnull
        )
        row = {
            "field_id": identity,
            "source_name": names[identity],
            "physical_type": kind,
            "row_count": len(values),
            "null_count": len(values) - len(nonnull),
            "non_null_count": len(nonnull),
            "distinct_count": len({_distinct(value, kind) for value in nonnull}),
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


def _integer(value):
    if not isinstance(value, str) or not INTEGER.fullmatch(value):
        raise ValueError("Integer rule value is not canonical")
    result = int(value)
    if result < -(2**63) or result > 2**63 - 1:
        raise ValueError("Integer rule value exceeds int64")
    return result


def _double(value):
    if not isinstance(value, str):
        raise ValueError("Double rule value must be text")
    try:
        exact = Decimal(value)
        decoded = json.loads(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Double rule value is invalid") from error
    result = float(exact)
    if type(decoded) not in {int, float} or not exact.is_finite() or not math.isfinite(result):
        raise ValueError("Double rule value must be finite")
    return result


def _typed(value, kind):
    if kind == "string":
        if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
            raise ValueError("String rule value must be NFC")
        return value
    if kind == "int64":
        return _integer(value)
    if kind == "double":
        return _double(value)
    if kind == "bool" and value in {"true", "false"}:
        return value == "true"
    raise ValueError("Rule value differs from the field type")


def validation_rows(table, rules):
    types = {field.name: str(field.type) for field in table.schema}
    if not rules or len(rules) != len({rule.get("rule_id") for rule in rules}):
        raise ValueError("Validation rule identities are invalid")
    rows = []
    for rule in rules:
        kind, failed, checked = rule.get("kind"), 0, table.num_rows
        if kind == "ROW_COUNT":
            low = _integer(rule["minimum"]) if rule.get("minimum") is not None else None
            high = _integer(rule["maximum"]) if rule.get("maximum") is not None else None
            if low is None and high is None:
                raise ValueError("ROW_COUNT requires a bound")
            failed = int(
                (low is not None and table.num_rows < low)
                or (high is not None and table.num_rows > high)
            )
            checked = 1
        elif kind == "UNIQUE":
            fields = rule.get("field_ids") or []
            if not fields or len(fields) != len(set(fields)) or not set(fields) <= set(types):
                raise ValueError("UNIQUE fields are invalid")
            keys = []
            for values in zip(*(table[field].to_pylist() for field in fields), strict=True):
                if any(value is None for value in values):
                    continue
                keys.append(
                    tuple(
                        _distinct(value, types[field])
                        for field, value in zip(fields, values, strict=True)
                    )
                )
            counts = Counter(keys)
            failed, checked = sum(count for count in counts.values() if count > 1), len(keys)
        else:
            field = rule.get("field_id")
            if field not in types:
                raise ValueError("Rule references an unknown field")
            values, field_type = table[field].to_pylist(), types[field]
            if kind == "TYPE":
                failed, checked = int(field_type != rule.get("expected_type")), 1
            elif kind == "NOT_NULL":
                failed = sum(value is None for value in values)
            elif kind == "RANGE":
                if field_type not in {"int64", "double"}:
                    raise ValueError("RANGE requires a numeric field")
                parse = _integer if field_type == "int64" else _double
                low = parse(rule["minimum"]) if rule.get("minimum") is not None else None
                high = parse(rule["maximum"]) if rule.get("maximum") is not None else None
                if low is not None and high is not None and low > high:
                    raise ValueError("RANGE minimum exceeds maximum")
                failed = sum(
                    value is not None
                    and (
                        isinstance(value, float)
                        and not math.isfinite(value)
                        or low is not None
                        and value < low
                        or high is not None
                        and value > high
                    )
                    for value in values
                )
            elif kind == "ALLOWED_VALUES":
                allowed = {_typed(value, field_type) for value in rule.get("allowed_values") or []}
                if not allowed:
                    raise ValueError("ALLOWED_VALUES cannot be empty")
                failed = sum(
                    value is not None
                    and (
                        isinstance(value, float)
                        and not math.isfinite(value)
                        or value not in allowed
                    )
                    for value in values
                )
            else:
                raise ValueError("Unknown validation rule")
        rows.append(
            {
                "rule_id": rule["rule_id"],
                "kind": kind,
                "status": "PASS" if failed == 0 else "FAIL",
                "checked_count": checked,
                "failed_count": failed,
            }
        )
    return rows


def _provenance_valid(manifest, operator):
    source = manifest.get("source", {})
    provenance = source.get("provenance", {})
    target = source.get("target", {})
    implementation = target.get("execution_binding", {})
    return bool(
        manifest.get("kind") == "OPERATOR_RESULT"
        and manifest.get("result_kind") == "OPERATOR_RESULT"
        and target.get("kind") == "OPERATOR"
        and target.get("operator_id") == operator
        and target.get("version_id") == "1.0.0"
        and implementation == {"kind": "LOCAL", "implementation_id": IMPLEMENTATIONS[operator]}
        and provenance.get("implementation_id") == IMPLEMENTATIONS[operator]
        and provenance.get("ownership") == "CURATED_PACKAGE"
        and provenance.get("trust_level") == "VERIFIED"
        and all(
            isinstance(provenance.get(key), str) and SHA256.fullmatch(provenance[key])
            for key in ("image_id", "catalog_digest", "operator_digest", "implementation_digest")
        )
    )


def _project_value_equal(left, right, kind):
    if left is None or right is None:
        return left is right
    if kind == "double":
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        return struct.pack(">d", left) == struct.pack(">d", right)
    return type(left) is type(right) and left == right


def reproduce_project(root, entry):
    manifest = entry["manifest"]
    source = manifest.get("source", {})
    source_entry = entry.get("source_table", {})
    selection = source.get("inputs", {}).get("table")
    if not (
        _provenance_valid(manifest, "table-project")
        and selection == source_entry.get("selection")
        and set(entry.get("files", {})) == {"report", "table", "method"}
    ):
        return False
    representations = {item.get("representation_id"): item for item in manifest["representations"]}
    if (
        set(representations) != {"report", "table", "method"}
        or any(
            representations[name].get("schema_id") != schema
            for name, schema in OUTPUT_SCHEMAS["table-project"].items()
        )
        or any(
            entry["files"][name].get("digest") != representations[name].get("digest")
            or entry["files"][name].get("schema_id") != representations[name].get("schema_id")
            or entry["files"][name].get("format") != representations[name].get("format")
            for name in representations
        )
    ):
        return False
    source_table = pq.read_table(pa.BufferReader(_bytes(root, source_entry)))
    source_manifest = source_entry.get("manifest", {})
    descriptor = source_manifest.get("tabular", source_manifest)
    descriptions = descriptor.get("fields", [])
    if _schema(source_table) != [
        (field.get("field_id"), field.get("type")) for field in descriptions
    ] or source_table.num_rows != descriptor.get("row_count"):
        return False
    descriptions_by_id = {item["field_id"]: item for item in descriptions}
    parameters = source.get("parameters", {})
    selected = parameters.get("fields", [])
    source_ids = [item.get("source_field_id") for item in selected]
    if (
        not selected
        or len(selected) != len(set(source_ids))
        or not set(source_ids) <= set(descriptions_by_id)
    ):
        return False
    expected_fields = []
    for ordinal, item in enumerate(selected):
        source_id = item["source_field_id"]
        description = descriptions_by_id[source_id]
        expected_fields.append(
            {
                "ordinal": ordinal,
                "output_field_id": f"f{ordinal}",
                "source_field_id": source_id,
                "source_name": description.get("name", source_id),
                "output_name": item.get("display_name") or description.get("name", source_id),
                "physical_type": description["type"],
            }
        )
    projected = pq.read_table(pa.BufferReader(_bytes(root, entry["files"]["table"])))
    expected_schema = [(item["output_field_id"], item["physical_type"]) for item in expected_fields]
    if _schema(projected) != expected_schema or projected.num_rows != source_table.num_rows:
        return False
    for item in expected_fields:
        source_values = source_table[item["source_field_id"]].to_pylist()
        result_values = projected[item["output_field_id"]].to_pylist()
        if len(source_values) != len(result_values) or not all(
            _project_value_equal(left, right, item["physical_type"])
            for left, right in zip(source_values, result_values, strict=True)
        ):
            return False
    report = json.loads(_bytes(root, entry["files"]["report"]))
    method = json.loads(_bytes(root, entry["files"]["method"]))
    source_digest = source_entry["digest"]
    expected_report = {
        "schema": "table-project-v1",
        "operator_id": "table-project",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": parameters,
        "row_count": source_table.num_rows,
        "field_count": len(expected_fields),
        "fields": expected_fields,
    }
    expected_method = {
        "schema": "table-project-v1",
        "operator_id": "table-project",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
    }
    output_descriptor = manifest.get("tabular", {})
    return bool(
        source.get("provenance", {}).get("input_digests", {}).get("table") == source_digest
        and report == expected_report
        and method == expected_method
        and output_descriptor.get("row_count") == source_table.num_rows
        and output_descriptor.get("fields")
        == [
            {
                "field_id": item["output_field_id"],
                "name": item["output_name"],
                "type": item["physical_type"],
                "source_field_id": item["source_field_id"],
            }
            for item in expected_fields
        ]
    )


def _filter_value(value, kind):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("Filter literal is invalid")
    if kind == "string":
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("Filter string literal must use NFC")
        return value
    if kind == "bool":
        if value not in {"true", "false"}:
            raise ValueError("Filter boolean literal is invalid")
        return value == "true"
    if kind == "int64":
        return _integer(value)
    if kind == "double":
        if not FILTER_NUMBER.fullmatch(value):
            raise ValueError("Filter double literal must be a JSON number")
        return _double(value)
    raise ValueError("Filter literal type is unsupported")


def _filter_expected(table, predicate):
    types = {field.name: str(field.type) for field in table.schema}
    if (
        not isinstance(predicate, dict)
        or predicate.get("semantics") != "kleene-three-valued-v1"
        or not isinstance(predicate.get("nodes"), list)
        or not 1 <= len(predicate["nodes"]) <= 128
    ):
        raise ValueError("Filter predicate is invalid")
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
    available, references, depths, compiled, identities = set(), {}, {}, [], []
    for node in predicate["nodes"]:
        if not isinstance(node, dict):
            raise ValueError("Filter node is invalid")
        identity, kind = node.get("node_id"), node.get("kind")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in available
            or kind not in shapes
        ):
            raise ValueError("Filter node identity or kind is invalid")
        present = {
            name
            for name, exists in {
                "field_id": node.get("field_id") is not None,
                "operator": node.get("operator") is not None,
                "value_type": node.get("value_type") is not None,
                "value": node.get("value") is not None,
                "values": node.get("values") is not None,
                "arguments": bool(node.get("arguments")),
            }.items()
            if exists
        }
        if present != shapes[kind]:
            raise ValueError("Filter node shape differs")
        arguments = node.get("arguments") or []
        if (
            not isinstance(arguments, list)
            or len(arguments) != len(set(arguments))
            or not set(arguments) <= available
        ):
            raise ValueError("Filter arguments are invalid")
        if kind == "NOT" and len(arguments) != 1:
            raise ValueError("Filter NOT arity is invalid")
        if kind in {"AND", "OR"} and not 2 <= len(arguments) <= 16:
            raise ValueError("Filter logical arity is invalid")
        field, value_type, parsed = node.get("field_id"), node.get("value_type"), None
        if field is not None and field not in types:
            raise ValueError("Filter field is unknown")
        if kind == "COMPARE":
            if value_type != types[field] or node.get("operator") not in {
                "EQ",
                "NE",
                "LT",
                "LTE",
                "GT",
                "GTE",
            }:
                raise ValueError("Filter comparison differs from field")
            if value_type == "bool" and node["operator"] not in {"EQ", "NE"}:
                raise ValueError("Boolean filter comparison is unordered")
            parsed = _filter_value(node.get("value"), value_type)
        elif kind == "IN":
            if value_type != types[field] or not isinstance(node.get("values"), list):
                raise ValueError("Filter set differs from field")
            parsed_values = [_filter_value(value, value_type) for value in node["values"]]
            if not 1 <= len(parsed_values) <= 64 or len(set(parsed_values)) != len(parsed_values):
                raise ValueError("Filter set is not unique and bounded")
            parsed = frozenset(parsed_values)
        elif kind in {"IS_NAN", "IS_FINITE"} and types[field] != "double":
            raise ValueError("Filter non-finite predicate requires double")
        for argument in arguments:
            references[argument] += 1
        depth = 1 + max((depths[argument] for argument in arguments), default=0)
        if depth > 8:
            raise ValueError("Filter predicate is too deep")
        references[identity], depths[identity] = 0, depth
        identities.append(identity)
        available.add(identity)
        compiled.append((identity, kind, field, node.get("operator"), parsed, arguments))
    root = predicate.get("root_id")
    if (
        root != identities[-1]
        or references[root]
        or any(count != 1 for identity, count in references.items() if identity != root)
    ):
        raise ValueError("Filter predicate is not one rooted tree")
    columns = {name: table[name].to_pylist() for name in types}
    mask, counts = [], {"true_count": 0, "false_count": 0, "unknown_count": 0}
    for row in range(table.num_rows):
        outcomes = {}
        for identity, kind, field, operator, parsed, arguments in compiled:
            value = columns[field][row] if field is not None else None
            if kind == "COMPARE":
                if value is None or isinstance(value, float) and math.isnan(value):
                    outcome = UNKNOWN
                else:
                    outcome = (
                        TRUE
                        if {
                            "EQ": value == parsed,
                            "NE": value != parsed,
                            "LT": value < parsed,
                            "LTE": value <= parsed,
                            "GT": value > parsed,
                            "GTE": value >= parsed,
                        }[operator]
                        else FALSE
                    )
            elif kind == "IN":
                outcome = (
                    UNKNOWN
                    if value is None or isinstance(value, float) and math.isnan(value)
                    else TRUE
                    if value in parsed
                    else FALSE
                )
            elif kind == "IS_NULL":
                outcome = TRUE if value is None else FALSE
            elif kind == "IS_NAN":
                outcome = (
                    UNKNOWN
                    if value is None
                    else TRUE
                    if isinstance(value, float) and math.isnan(value)
                    else FALSE
                )
            elif kind == "IS_FINITE":
                outcome = UNKNOWN if value is None else TRUE if math.isfinite(value) else FALSE
            elif kind == "NOT":
                child = outcomes[arguments[0]]
                outcome = UNKNOWN if child == UNKNOWN else TRUE if child == FALSE else FALSE
            elif kind == "AND":
                children = [outcomes[argument] for argument in arguments]
                outcome = FALSE if FALSE in children else UNKNOWN if UNKNOWN in children else TRUE
            else:
                children = [outcomes[argument] for argument in arguments]
                outcome = TRUE if TRUE in children else UNKNOWN if UNKNOWN in children else FALSE
            outcomes[identity] = outcome
        outcome = outcomes[root]
        counts[{TRUE: "true_count", FALSE: "false_count", UNKNOWN: "unknown_count"}[outcome]] += 1
        mask.append(outcome == TRUE)
    return table.filter(pa.array(mask, type=pa.bool_())), counts


def reproduce_filter(root, entry):
    manifest = entry["manifest"]
    source = manifest.get("source", {})
    source_entry = entry.get("source_table", {})
    selection = source.get("inputs", {}).get("table")
    if not (
        _provenance_valid(manifest, "table-filter")
        and selection == source_entry.get("selection")
        and set(entry.get("files", {})) == {"report", "table", "method"}
    ):
        return False
    representations = {item.get("representation_id"): item for item in manifest["representations"]}
    if (
        set(representations) != {"report", "table", "method"}
        or any(
            representations[name].get("schema_id") != schema
            for name, schema in OUTPUT_SCHEMAS["table-filter"].items()
        )
        or any(
            entry["files"][name].get("digest") != representations[name].get("digest")
            or entry["files"][name].get("schema_id") != representations[name].get("schema_id")
            or entry["files"][name].get("format") != representations[name].get("format")
            for name in representations
        )
    ):
        return False
    source_table = pq.read_table(pa.BufferReader(_bytes(root, source_entry)))
    source_manifest = source_entry.get("manifest", {})
    descriptor = source_manifest.get("tabular", source_manifest)
    descriptions = descriptor.get("fields", [])
    if _schema(source_table) != [
        (field.get("field_id"), field.get("type")) for field in descriptions
    ] or source_table.num_rows != descriptor.get("row_count"):
        return False
    parameters = source.get("parameters", {})
    predicate = parameters.get("predicate")
    filtered, counts = _filter_expected(source_table, predicate)
    result = pq.read_table(pa.BufferReader(_bytes(root, entry["files"]["table"])))
    if _schema(result) != _schema(source_table) or result.num_rows != filtered.num_rows:
        return False
    for field in descriptions:
        identity, kind = field["field_id"], field["type"]
        expected_values, actual_values = (
            filtered[identity].to_pylist(),
            result[identity].to_pylist(),
        )
        if len(expected_values) != len(actual_values) or not all(
            _project_value_equal(left, right, kind)
            for left, right in zip(expected_values, actual_values, strict=True)
        ):
            return False
    encoded = json.dumps(
        predicate,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    predicate_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    source_digest = source_entry["digest"]
    expected_report = {
        "schema": "table-filter-v1",
        "operator_id": "table-filter",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": parameters,
        "predicate_digest": predicate_digest,
        "input_row_count": source_table.num_rows,
        "output_row_count": filtered.num_rows,
        "dropped_row_count": source_table.num_rows - filtered.num_rows,
        **counts,
    }
    expected_method = {
        "schema": "table-filter-v1",
        "operator_id": "table-filter",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
        "predicate_semantics": "kleene-three-valued-v1",
    }
    report = json.loads(_bytes(root, entry["files"]["report"]))
    method = json.loads(_bytes(root, entry["files"]["method"]))
    output_descriptor = manifest.get("tabular", {})
    return bool(
        source.get("provenance", {}).get("input_digests", {}).get("table") == source_digest
        and report == expected_report
        and method == expected_method
        and output_descriptor.get("row_count") == filtered.num_rows
        and output_descriptor.get("fields") == descriptions
    )


class _ReferenceCastError(ValueError):
    pass


def _cast_reference(value, source_type, target_type):
    """Independent implementation of strict-scalar-cast-v1."""

    if target_type == "string":
        if source_type == "bool":
            return "true" if value else "false"
        if source_type == "int64":
            return str(value)
        if source_type == "double" and math.isfinite(value):
            return json.dumps(value, allow_nan=False, separators=(",", ":"))
        raise _ReferenceCastError
    if target_type == "int64":
        if source_type == "string":
            try:
                return _integer(value)
            except ValueError as exc:
                raise _ReferenceCastError from exc
        if source_type == "double" and math.isfinite(value) and value.is_integer():
            result = int(value)
            if -(2**63) <= result <= 2**63 - 1:
                return result
        if source_type == "bool":
            return int(value)
        raise _ReferenceCastError
    if target_type == "double":
        if source_type == "string" and FILTER_NUMBER.fullmatch(value):
            try:
                return _double(value)
            except ValueError as exc:
                raise _ReferenceCastError from exc
        if source_type == "int64":
            result = float(value)
            if math.isfinite(result) and int(result) == value:
                return result
        if source_type == "bool":
            return 1.0 if value else 0.0
        raise _ReferenceCastError
    if target_type == "bool":
        if source_type == "string" and value in {"true", "false"}:
            return value == "true"
        if source_type == "int64" and value in {0, 1}:
            return bool(value)
        if source_type == "double" and math.isfinite(value) and value in {0.0, 1.0}:
            return bool(value)
        raise _ReferenceCastError
    raise _ReferenceCastError


def _expected_cast(table, parameters):
    if parameters.get("semantics") != "strict-scalar-cast-v1":
        raise ValueError("Cast semantics differ")
    casts = parameters.get("casts")
    if not isinstance(casts, list) or not 1 <= len(casts) <= 128:
        raise ValueError("Cast list is invalid")
    types = {field.name: str(field.type) for field in table.schema}
    if len({item.get("field_id") for item in casts if isinstance(item, dict)}) != len(casts):
        raise ValueError("Cast field identities differ")
    plans = {}
    for item in casts:
        if not isinstance(item, dict) or set(item) != {"field_id", "target_type", "on_error"}:
            raise ValueError("Cast item differs")
        source = types.get(item["field_id"])
        if (
            source not in {"string", "int64", "double", "bool"}
            or item["target_type"] not in {"string", "int64", "double", "bool"}
            or source == item["target_type"]
            or item["on_error"] not in {"ERROR", "SET_NULL"}
        ):
            raise ValueError("Cast conversion differs")
        plans[item["field_id"]] = (source, item["target_type"], item["on_error"])
    arrow_types = {
        "string": pa.string(),
        "int64": pa.int64(),
        "double": pa.float64(),
        "bool": pa.bool_(),
    }
    arrays, summaries = [], {}
    for field in table.schema:
        if field.name not in plans:
            arrays.append(table[field.name])
            continue
        source, target, policy = plans[field.name]
        values, converted, nulls, failures = table[field.name].to_pylist(), [], 0, 0
        for value in values:
            if value is None:
                nulls += 1
                converted.append(None)
                continue
            try:
                converted.append(_cast_reference(value, source, target))
            except _ReferenceCastError:
                failures += 1
                converted.append(None)
        if failures and policy == "ERROR":
            raise ValueError("Published cast contains ERROR conversion failures")
        summaries[field.name] = {
            "field_id": field.name,
            "source_type": source,
            "target_type": target,
            "on_error": policy,
            "input_null_count": nulls,
            "converted_count": len(values) - nulls - failures,
            "failure_count": failures,
            "output_null_count": nulls + failures,
        }
        arrays.append(pa.array(converted, type=arrow_types[target]))
    return (
        pa.Table.from_arrays(arrays, names=table.column_names),
        [summaries[item["field_id"]] for item in casts],
    )


def reproduce_cast(root, entry):
    manifest = entry["manifest"]
    source = manifest.get("source", {})
    source_entry = entry.get("source_table", {})
    selection = source.get("inputs", {}).get("table")
    if not (
        _provenance_valid(manifest, "table-cast")
        and selection == source_entry.get("selection")
        and set(entry.get("files", {})) == {"report", "table", "method"}
    ):
        return False
    representations = {item.get("representation_id"): item for item in manifest["representations"]}
    if (
        set(representations) != {"report", "table", "method"}
        or any(
            representations[name].get("schema_id") != schema
            for name, schema in OUTPUT_SCHEMAS["table-cast"].items()
        )
        or any(
            entry["files"][name].get("digest") != representations[name].get("digest")
            or entry["files"][name].get("schema_id") != representations[name].get("schema_id")
            or entry["files"][name].get("format") != representations[name].get("format")
            for name in representations
        )
    ):
        return False
    source_table = pq.read_table(pa.BufferReader(_bytes(root, source_entry)))
    source_manifest = source_entry.get("manifest", {})
    descriptor = source_manifest.get("tabular", source_manifest)
    descriptions = descriptor.get("fields", [])
    if _schema(source_table) != [
        (field.get("field_id"), field.get("type")) for field in descriptions
    ] or source_table.num_rows != descriptor.get("row_count"):
        return False
    parameters = source.get("parameters", {})
    try:
        expected, summaries = _expected_cast(source_table, parameters)
    except (ValueError, TypeError, KeyError):
        return False
    result = pq.read_table(pa.BufferReader(_bytes(root, entry["files"]["table"])))
    if _schema(result) != _schema(expected) or result.num_rows != source_table.num_rows:
        return False
    for field in result.schema:
        if not all(
            _project_value_equal(left, right, str(field.type))
            for left, right in zip(
                result[field.name].to_pylist(), expected[field.name].to_pylist(), strict=True
            )
        ):
            return False
    source_digest = source_entry["digest"]
    report = json.loads(_bytes(root, entry["files"]["report"]))
    method = json.loads(_bytes(root, entry["files"]["method"]))
    expected_report = {
        "schema": "table-cast-v1",
        "operator_id": "table-cast",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": parameters,
        "row_count": source_table.num_rows,
        "cast_count": len(summaries),
        "casts": summaries,
    }
    expected_method = {
        "schema": "table-cast-v1",
        "operator_id": "table-cast",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
        "cast_semantics": "strict-scalar-cast-v1",
    }
    expected_fields = [dict(item) for item in descriptions]
    by_id = {item["field_id"]: item for item in expected_fields}
    for item in parameters["casts"]:
        by_id[item["field_id"]]["type"] = item["target_type"]
    output_descriptor = manifest.get("tabular", {})
    return bool(
        source.get("provenance", {}).get("input_digests", {}).get("table") == source_digest
        and report == expected_report
        and method == expected_method
        and output_descriptor.get("row_count") == source_table.num_rows
        and output_descriptor.get("fields") == expected_fields
    )


def _reference_aggregate(table, parameters, descriptions):
    if parameters.get("semantics") != "strict-table-aggregate-v1":
        raise ValueError("Aggregate semantics differ")
    group_by, measures = parameters.get("group_by"), parameters.get("measures")
    policy = parameters.get("null_key_policy")
    types = {field.name: str(field.type) for field in table.schema}
    if (
        not isinstance(group_by, list)
        or len(group_by) > 16
        or len(set(group_by)) != len(group_by)
        or any(types.get(identity) not in {"string", "int64", "bool"} for identity in group_by)
        or policy not in {"GROUP", "DROP", "ERROR"}
        or not isinstance(measures, list)
        or not 1 <= len(measures) <= 64
    ):
        raise ValueError("Aggregate parameters differ")
    operations = {"COUNT_ROWS", "COUNT_NON_NULL", "COUNT_DISTINCT", "MIN", "MAX", "SUM", "MEAN"}
    plans, measure_ids = [], []
    for item in measures:
        if not isinstance(item, dict) or not {"measure_id", "operation"} <= set(item):
            raise ValueError("Aggregate measure differs")
        if not set(item) <= {"measure_id", "operation", "field_id", "display_name"}:
            raise ValueError("Aggregate measure fields differ")
        measure_id, operation, field_id = (
            item.get("measure_id"),
            item.get("operation"),
            item.get("field_id"),
        )
        if not isinstance(measure_id, str) or operation not in operations:
            raise ValueError("Aggregate operation differs")
        if operation == "COUNT_ROWS":
            if field_id is not None:
                raise ValueError("COUNT_ROWS field differs")
            source_type, output_type = None, "int64"
        else:
            source_type = types.get(field_id)
            if source_type is None or (
                operation in {"SUM", "MEAN"} and source_type not in {"int64", "double"}
            ):
                raise ValueError("Aggregate source differs")
            output_type = (
                "int64"
                if operation in {"COUNT_NON_NULL", "COUNT_DISTINCT"}
                else "double"
                if operation == "MEAN"
                else source_type
            )
        measure_ids.append(measure_id)
        plans.append((item, source_type, output_type))
    if len(set(measure_ids)) != len(measure_ids):
        raise ValueError("Aggregate measure identities differ")

    columns = {
        name: table[name].to_pylist()
        for name in set(group_by)
        | {item.get("field_id") for item in measures if item.get("field_id")}
    }
    groups, participating, dropped = {}, [], 0
    for row in range(table.num_rows):
        key = tuple(columns[identity][row] for identity in group_by)
        if any(value is None for value in key):
            if policy == "DROP":
                dropped += 1
                continue
            if policy == "ERROR":
                raise ValueError("Successful aggregate contains a null-key error")
        participating.append(row)
        groups.setdefault(key, []).append(row)
    if not group_by and not groups:
        groups[()] = []
    for item, source_type, _ in plans:
        if source_type == "double" and any(
            value is not None and not math.isfinite(value)
            for row in participating
            if (value := columns[item["field_id"]][row]) is not None
        ):
            raise ValueError("Successful aggregate contains non-finite input")

    rows = []
    for key, indices in groups.items():
        output = list(key)
        for item, source_type, _ in plans:
            operation, field_id = item["operation"], item.get("field_id")
            values = (
                [columns[field_id][row] for row in indices if columns[field_id][row] is not None]
                if field_id is not None
                else []
            )
            if operation == "COUNT_ROWS":
                value = len(indices)
            elif operation == "COUNT_NON_NULL":
                value = len(values)
            elif operation == "COUNT_DISTINCT":
                value = len(set(values))
            elif not values:
                value = None
            elif operation == "MIN":
                value = min(values)
            elif operation == "MAX":
                value = max(values)
            elif operation == "SUM" and source_type == "int64":
                value = sum(values)
                if not -(2**63) <= value <= 2**63 - 1:
                    raise ValueError("Successful aggregate contains int64 overflow")
            elif operation == "SUM":
                value = float(sum(Decimal(str(item)) for item in values))
            elif operation == "MEAN":
                value = float(sum(Decimal(str(item)) for item in values) / Decimal(len(values)))
            else:
                raise ValueError("Aggregate operation differs")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Successful aggregate contains numeric overflow")
            output.append(value)
        rows.append(output)

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
        ordinal, field_id = len(group_by) + index, item.get("field_id")
        values = columns[field_id] if field_id is not None else None
        nonnull = (
            len(participating)
            if values is None
            else sum(values[row] is not None for row in participating)
        )
        summaries.append(
            {
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
        )
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
    return (
        rows,
        fields,
        summaries,
        {
            "input_row_count": table.num_rows,
            "participating_row_count": len(participating),
            "dropped_key_row_count": dropped,
            "group_count": len(groups),
        },
    )


def reproduce_aggregate(root, entry):
    manifest = entry["manifest"]
    source = manifest.get("source", {})
    source_entry = entry.get("source_table", {})
    selection = source.get("inputs", {}).get("table")
    if not (
        _provenance_valid(manifest, "table-aggregate")
        and selection == source_entry.get("selection")
        and set(entry.get("files", {})) == {"report", "table", "method"}
    ):
        return False
    representations = {item.get("representation_id"): item for item in manifest["representations"]}
    if (
        set(representations) != {"report", "table", "method"}
        or any(
            representations[name].get("schema_id") != schema
            for name, schema in OUTPUT_SCHEMAS["table-aggregate"].items()
        )
        or any(
            entry["files"][name].get("digest") != representations[name].get("digest")
            or entry["files"][name].get("schema_id") != representations[name].get("schema_id")
            or entry["files"][name].get("format") != representations[name].get("format")
            for name in representations
        )
    ):
        return False
    source_table = pq.read_table(pa.BufferReader(_bytes(root, source_entry)))
    source_manifest = source_entry.get("manifest", {})
    descriptor = source_manifest.get("tabular", source_manifest)
    descriptions = descriptor.get("fields", [])
    if _schema(source_table) != [
        (field.get("field_id"), field.get("type")) for field in descriptions
    ] or source_table.num_rows != descriptor.get("row_count"):
        return False
    parameters = source.get("parameters", {})
    try:
        rows, fields, summaries, counts = _reference_aggregate(
            source_table, parameters, descriptions
        )
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return False
    result = pq.read_table(pa.BufferReader(_bytes(root, entry["files"]["table"])))
    if _schema(result) != [(field["field_id"], field["type"]) for field in fields]:
        return False
    actual_rows = result.to_pylist()
    if len(actual_rows) != len(rows):
        return False
    for actual, expected in zip(actual_rows, rows, strict=True):
        for field, reference in zip(fields, expected, strict=True):
            value = actual[field["field_id"]]
            if field["type"] == "double" and value is not None and reference is not None:
                if not math.isclose(value, reference, rel_tol=1e-8, abs_tol=1e-10):
                    return False
            elif value != reference:
                return False
    source_digest = source_entry["digest"]
    report = json.loads(_bytes(root, entry["files"]["report"]))
    method = json.loads(_bytes(root, entry["files"]["method"]))
    expected_report = {
        "schema": "table-aggregate-v1",
        "operator_id": "table-aggregate",
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": parameters,
        **counts,
        "output_row_count": len(rows),
        "field_count": len(fields),
        "fields": fields,
        "measure_count": len(summaries),
        "measures": summaries,
    }
    expected_method = {
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
    return bool(
        source.get("provenance", {}).get("input_digests", {}).get("table") == source_digest
        and report == expected_report
        and method == expected_method
        and manifest.get("tabular", {}).get("row_count") == len(rows)
        and manifest.get("tabular", {}).get("fields") == fields
    )


def reproduce_role(root, entry, operator):
    """Return whether one frozen tabular result agrees with an independent recomputation."""

    if operator == "table-project":
        return reproduce_project(root, entry)
    if operator == "table-filter":
        return reproduce_filter(root, entry)
    if operator == "table-cast":
        return reproduce_cast(root, entry)
    if operator == "table-aggregate":
        return reproduce_aggregate(root, entry)

    manifest = entry["manifest"]
    source = manifest.get("source", {})
    source_entry = entry.get("source_table", {})
    selection = source.get("inputs", {}).get("table")
    if not (
        _provenance_valid(manifest, operator)
        and selection == source_entry.get("selection")
        and set(entry.get("files", {})) == {"report", "table", "method"}
    ):
        return False
    representations = {item.get("representation_id"): item for item in manifest["representations"]}
    if (
        set(representations) != {"report", "table", "method"}
        or any(
            representations[name].get("schema_id") != schema
            for name, schema in OUTPUT_SCHEMAS[operator].items()
        )
        or any(
            entry["files"][name].get("digest") != representations[name].get("digest")
            or entry["files"][name].get("schema_id") != representations[name].get("schema_id")
            or entry["files"][name].get("format") != representations[name].get("format")
            for name in representations
        )
    ):
        return False

    source_table = pq.read_table(pa.BufferReader(_bytes(root, source_entry)))
    source_manifest = source_entry.get("manifest", {})
    descriptor = source_manifest.get("tabular", source_manifest)
    descriptions = descriptor.get("fields", [])
    if _schema(source_table) != [
        (field.get("field_id"), field.get("type")) for field in descriptions
    ] or source_table.num_rows != descriptor.get("row_count"):
        return False

    parameters = source.get("parameters")
    expected = (
        profile_rows(source_table, parameters.get("fields", []), descriptions)
        if operator == "table-profile"
        else validation_rows(source_table, parameters.get("rules", []))
    )
    report = json.loads(_bytes(root, entry["files"]["report"]))
    result_table = pq.read_table(pa.BufferReader(_bytes(root, entry["files"]["table"])))
    method = json.loads(_bytes(root, entry["files"]["method"]))
    schema = "table-profile-v1" if operator == "table-profile" else "table-validation-v1"
    expected_schema = PROFILE_SCHEMA if operator == "table-profile" else VALIDATION_SCHEMA
    source_digest = source_entry["digest"]
    expected_report = {
        "schema": schema,
        "operator_id": operator,
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "parameters": parameters,
        "row_count": len(expected),
        "rows": expected,
    }
    expected_method = {
        "schema": schema,
        "operator_id": operator,
        "version_id": "1.0.0",
        "source_digest": source_digest,
        "method": "deterministic-pyarrow-v1",
    }
    return bool(
        source.get("provenance", {}).get("input_digests", {}).get("table") == source_digest
        and report == expected_report
        and _schema(result_table) == expected_schema
        and result_table.to_pylist() == expected
        and method == expected_method
        and (operator != "table-validate" or all(row["status"] == "PASS" for row in expected))
    )
