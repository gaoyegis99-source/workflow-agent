"""Pure validation of frozen tabular operator requests.

These checks deliberately depend on plain JSON-like values only, so the
Science application service can validate a frozen Data schema without gaining
an HTTP-contract or repository dependency.
"""

from __future__ import annotations


def filter_references_match_fields(parameters, fields):
    if not isinstance(parameters, dict) or not isinstance(fields, list):
        return False
    predicate = parameters.get("predicate")
    nodes = predicate.get("nodes") if isinstance(predicate, dict) else None
    if not isinstance(nodes, list):
        return False
    field_types = {
        field.get("field_id"): field.get("type") for field in fields if isinstance(field, dict)
    }
    field_nodes = {"COMPARE", "IN", "IS_NULL", "IS_NAN", "IS_FINITE"}
    for node in nodes:
        if not isinstance(node, dict):
            return False
        kind = node.get("kind")
        if kind not in field_nodes:
            continue
        field_id = node.get("field_id")
        if not isinstance(field_id, str) or field_id not in field_types:
            return False
        physical_type = field_types[field_id]
        if kind in {"COMPARE", "IN"} and node.get("value_type") != physical_type:
            return False
        if kind in {"IS_NAN", "IS_FINITE"} and physical_type != "double":
            return False
    return True


def cast_references_match_fields(parameters, fields):
    if not isinstance(parameters, dict) or not isinstance(fields, list):
        return False
    if parameters.get("semantics") != "strict-scalar-cast-v1":
        return False
    casts = parameters.get("casts")
    if not isinstance(casts, list) or not 1 <= len(casts) <= 128:
        return False
    field_types = {
        field.get("field_id"): field.get("type") for field in fields if isinstance(field, dict)
    }
    if len(field_types) != len(fields):
        return False
    supported = {"string", "int64", "double", "bool"}
    identities = []
    for cast in casts:
        if not isinstance(cast, dict) or set(cast) != {"field_id", "target_type", "on_error"}:
            return False
        field_id = cast.get("field_id")
        source_type = field_types.get(field_id)
        target_type = cast.get("target_type")
        if (
            source_type not in supported
            or target_type not in supported
            or source_type == target_type
            or cast.get("on_error") not in {"ERROR", "SET_NULL"}
        ):
            return False
        identities.append(field_id)
    return len(set(identities)) == len(identities)


def aggregate_references_match_fields(parameters, fields):
    if not isinstance(parameters, dict) or not isinstance(fields, list):
        return False
    if parameters.get("semantics") != "strict-table-aggregate-v1":
        return False
    group_by = parameters.get("group_by")
    measures = parameters.get("measures")
    if (
        not isinstance(group_by, list)
        or len(group_by) > 16
        or len(set(group_by)) != len(group_by)
        or parameters.get("null_key_policy") not in {"GROUP", "DROP", "ERROR"}
        or not isinstance(measures, list)
        or not 1 <= len(measures) <= 64
    ):
        return False
    field_types = {
        field.get("field_id"): field.get("type") for field in fields if isinstance(field, dict)
    }
    if len(field_types) != len(fields) or any(
        value not in {"string", "int64", "double", "bool"} for value in field_types.values()
    ):
        return False
    if any(field_types.get(identity) not in {"string", "int64", "bool"} for identity in group_by):
        return False
    operations = {"COUNT_ROWS", "COUNT_NON_NULL", "COUNT_DISTINCT", "MIN", "MAX", "SUM", "MEAN"}
    identities = []
    for measure in measures:
        if not isinstance(measure, dict) or not {"measure_id", "operation"} <= set(measure):
            return False
        if not set(measure) <= {"measure_id", "operation", "field_id", "display_name"}:
            return False
        identity, operation, field_id = (
            measure.get("measure_id"),
            measure.get("operation"),
            measure.get("field_id"),
        )
        if not isinstance(identity, str) or operation not in operations:
            return False
        if operation == "COUNT_ROWS":
            if field_id is not None:
                return False
        else:
            source_type = field_types.get(field_id)
            if source_type is None or (
                operation in {"SUM", "MEAN"} and source_type not in {"int64", "double"}
            ):
                return False
        identities.append(identity)
    return len(set(identities)) == len(identities)
