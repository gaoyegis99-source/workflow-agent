"""Pure binding and structural inference used by authoring previews."""

from __future__ import annotations

from collections import deque
from copy import deepcopy

from platform_core.domain.control import valid_identifier


def _location(
    step: str | None = None,
    port: str | None = None,
    pointer: str = "",
    document: str = "main",
) -> dict:
    return {"document_id": document, "step_key": step, "port": port, "pointer": pointer}


def _issue(
    code: str,
    message: str,
    *,
    step: str | None = None,
    port: str | None = None,
    pointer: str = "",
    scope: str = "SOURCE",
    severity: str = "ERROR",
    document: str = "main",
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "scope": scope,
        "location": _location(step, port, pointer, document),
        "message": message,
    }


def _unknown(source: str | None = None) -> dict:
    return {
        "representation_kind": "UNKNOWN",
        "content_role": "UNBOUND_INPUT",
        "fields": None,
        "spatial": None,
        "source": source,
    }


def structure_from_binding(binding: dict, source: str) -> dict:
    manifest = binding.get("manifest", {})
    table = binding.get("table")
    if not isinstance(table, dict) and isinstance(manifest.get("fields"), list):
        table = {"fields": manifest["fields"], "row_count": manifest.get("row_count")}
    if isinstance(table, dict):
        return {
            "representation_kind": "TABLE",
            "content_role": "SOURCE_TABLE",
            "fields": deepcopy(table.get("fields", [])),
            "spatial": None,
            "source": source,
        }
    spatial = manifest.get("spatial")
    if isinstance(spatial, dict):
        return {
            "representation_kind": "VECTOR" if spatial.get("layers") is not None else "IMAGE",
            "content_role": "SOURCE_VECTOR"
            if spatial.get("layers") is not None
            else "SOURCE_RASTER",
            "fields": None,
            "spatial": deepcopy(spatial),
            "source": source,
        }
    return _unknown(source)


def _fixed_table(role: str, names: list[tuple[str, str]]) -> dict:
    return {
        "representation_kind": "TABLE",
        "content_role": role,
        "fields": [
            {"field_id": f"f{index}", "name": name, "type": kind}
            for index, (name, kind) in enumerate(names)
        ],
        "spatial": None,
        "source": None,
    }


PROFILE_FIELDS = [
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
VALIDATION_FIELDS = [
    ("rule_id", "string"),
    ("kind", "string"),
    ("status", "string"),
    ("checked_count", "int64"),
    ("failed_count", "int64"),
]


def _port_structure(port: dict, source: str) -> dict:
    return {
        "representation_kind": port["representation_kind"],
        "content_role": port["content_role"],
        "fields": None,
        "spatial": None,
        "source": source,
    }


def _field_map(structure: dict) -> dict[str, dict] | None:
    fields = structure.get("fields")
    if not isinstance(fields, list):
        return None
    result = {}
    for field in fields:
        if not isinstance(field, dict) or not valid_identifier(field.get("field_id")):
            return None
        result[field["field_id"]] = field
    return result if len(result) == len(fields) else None


def _referenced_fields(operator: str, parameters: dict) -> list[tuple[str, str]]:
    if operator == "table-profile":
        return [
            (str(value), f"/fields/{index}")
            for index, value in enumerate(parameters.get("fields", []))
        ]
    if operator == "table-project":
        return [
            (str(value.get("source_field_id")), f"/fields/{index}/source_field_id")
            for index, value in enumerate(parameters.get("fields", []))
            if isinstance(value, dict)
        ]
    if operator == "table-cast":
        return [
            (str(value.get("field_id")), f"/casts/{index}/field_id")
            for index, value in enumerate(parameters.get("casts", []))
            if isinstance(value, dict)
        ]
    if operator == "table-aggregate":
        result = [
            (str(value), f"/group_by/{index}")
            for index, value in enumerate(parameters.get("group_by", []))
        ]
        result.extend(
            (str(value.get("field_id")), f"/measures/{index}/field_id")
            for index, value in enumerate(parameters.get("measures", []))
            if isinstance(value, dict) and value.get("field_id") is not None
        )
        return result
    if operator == "table-filter":
        nodes = parameters.get("predicate", {}).get("nodes", [])
        return [
            (str(value.get("field_id")), f"/predicate/nodes/{index}/field_id")
            for index, value in enumerate(nodes)
            if isinstance(value, dict) and value.get("field_id") is not None
        ]
    if operator == "table-validate":
        result = []
        for index, value in enumerate(parameters.get("rules", [])):
            if not isinstance(value, dict):
                continue
            if value.get("field_id") is not None:
                result.append((str(value["field_id"]), f"/rules/{index}/field_id"))
            result.extend(
                (str(field), f"/rules/{index}/field_ids/{field_index}")
                for field_index, field in enumerate(value.get("field_ids") or [])
            )
        return result
    return []


def _transform_table(operator: str, parameters: dict, input_structure: dict) -> dict | None:
    fields = _field_map(input_structure)
    if fields is None:
        return None
    if operator == "table-filter":
        return deepcopy(input_structure)
    if operator == "table-project":
        projected = []
        for index, value in enumerate(parameters.get("fields", [])):
            source = fields.get(value.get("source_field_id")) if isinstance(value, dict) else None
            if source is None:
                return None
            projected.append(
                {
                    "field_id": f"f{index}",
                    "name": value.get("display_name") or source.get("name", source["field_id"]),
                    "type": source.get("type"),
                    "source_field_id": source["field_id"],
                }
            )
        return {**deepcopy(input_structure), "fields": projected, "content_role": "DERIVED_TABLE"}
    if operator == "table-cast":
        changed = deepcopy(input_structure)
        by_id = {item["field_id"]: item for item in changed["fields"]}
        for value in parameters.get("casts", []):
            if isinstance(value, dict) and value.get("field_id") in by_id:
                by_id[value["field_id"]]["type"] = value.get("target_type")
        changed["content_role"] = "DERIVED_TABLE"
        return changed
    if operator == "table-aggregate":
        output = []
        for source_id in parameters.get("group_by", []):
            source = fields.get(source_id)
            if source is None:
                return None
            output.append((source.get("name", source_id), source.get("type"), source_id))
        for measure in parameters.get("measures", []):
            if not isinstance(measure, dict):
                return None
            operation = measure.get("operation")
            source_id = measure.get("field_id")
            source_type = fields.get(source_id, {}).get("type")
            kind = (
                "int64"
                if operation in {"COUNT_ROWS", "COUNT_NON_NULL", "COUNT_DISTINCT"}
                else "double"
                if operation == "MEAN"
                else source_type
            )
            output.append(
                (measure.get("display_name") or measure.get("measure_id"), kind, source_id)
            )
        return {
            "representation_kind": "TABLE",
            "content_role": "DERIVED_TABLE",
            "fields": [
                {"field_id": f"f{index}", "name": name, "type": kind, "source_field_id": source}
                for index, (name, kind, source) in enumerate(output)
            ],
            "spatial": None,
            "source": input_structure.get("source"),
        }
    if operator == "table-profile":
        return _fixed_table("PROFILE_TABLE", PROFILE_FIELDS)
    if operator == "table-validate":
        return _fixed_table("VALIDATION_REPORT", VALIDATION_FIELDS)
    return None


def _field_type_issues(
    operator: str, parameters: dict, fields: dict[str, dict]
) -> list[tuple[str, str]]:
    """Return value-free pointers for statically impossible field operations."""

    issues = []
    if operator == "table-aggregate":
        for index, measure in enumerate(parameters.get("measures", [])):
            if not isinstance(measure, dict):
                continue
            field_id = measure.get("field_id")
            if (
                measure.get("operation") in {"MIN", "MAX", "SUM", "MEAN"}
                and field_id in fields
                and fields[field_id].get("type") not in {"int64", "double"}
            ):
                issues.append((f"/measures/{index}/field_id", "Aggregate requires a numeric field"))
    if operator == "table-validate":
        for index, rule in enumerate(parameters.get("rules", [])):
            if not isinstance(rule, dict):
                continue
            field_id = rule.get("field_id")
            if (
                rule.get("kind") == "RANGE"
                and field_id in fields
                and fields[field_id].get("type") not in {"int64", "double"}
            ):
                issues.append(
                    (f"/rules/{index}/field_id", "Range validation requires a numeric field")
                )
    if operator == "table-filter":
        for index, node in enumerate(parameters.get("predicate", {}).get("nodes", [])):
            if not isinstance(node, dict) or node.get("field_id") not in fields:
                continue
            physical = fields[node["field_id"]].get("type")
            if node.get("value_type") is not None and node["value_type"] != physical:
                issues.append(
                    (
                        f"/predicate/nodes/{index}/value_type",
                        "Filter literal type differs from the field type",
                    )
                )
            if node.get("kind") in {"IS_NAN", "IS_FINITE"} and physical not in {"int64", "double"}:
                issues.append(
                    (
                        f"/predicate/nodes/{index}/field_id",
                        "Numeric predicate requires a numeric field",
                    )
                )
    return issues


def _topological(steps: dict) -> tuple[list[str], set[str]]:
    dependencies = {key: set() for key in steps}
    followers = {key: set() for key in steps}
    for key, step in steps.items():
        for binding in step.get("in", {}).values() if isinstance(step, dict) else []:
            source = (
                binding
                if isinstance(binding, str)
                else binding.get("source")
                if isinstance(binding, dict)
                else None
            )
            if isinstance(source, str) and "/" in source:
                parent = source.split("/", 1)[0]
                if parent in steps:
                    dependencies[key].add(parent)
                    followers[parent].add(key)
    queue = deque(sorted(key for key, values in dependencies.items() if not values))
    ordered = []
    while queue:
        key = queue.popleft()
        ordered.append(key)
        for follower in sorted(followers[key]):
            dependencies[follower].discard(key)
            if not dependencies[follower]:
                queue.append(follower)
    return ordered, set(steps) - set(ordered)
