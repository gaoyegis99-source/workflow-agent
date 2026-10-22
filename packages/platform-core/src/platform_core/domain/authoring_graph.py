"""Pure validation and structure propagation for the bounded A2 graph profile."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from platform_core.domain.control import require, valid_identifier

MAX_SOURCE_BYTES = 1024 * 1024
FORBIDDEN_KEYS = {
    "$include",
    "$import",
    "baseCommand",
    "expression",
    "hints",
    "location",
    "path",
    "valueFrom",
}
ALLOWED_REQUIREMENTS = {
    "SubworkflowFeatureRequirement",
    "ScatterFeatureRequirement",
    "MultipleInputFeatureRequirement",
}
ALLOWED_STEP_FIELDS = {
    "run",
    "in",
    "out",
    "scatter",
    "scatterMethod",
    "when",
    "label",
    "doc",
}
ALLOWED_DOCUMENT_FIELDS = {
    "cwlVersion",
    "class",
    "inputs",
    "outputs",
    "steps",
    "label",
    "doc",
    "requirements",
}
ALLOWED_BINDING_FIELDS = {"source", "default", "linkMerge", "pickValue"}
ALLOWED_SCATTER_METHODS = {"dotproduct", "flat_crossproduct", "nested_crossproduct"}
ALLOWED_LINK_MERGE = {"merge_nested", "merge_flattened"}
ALLOWED_PICK_VALUE = {"first_non_null", "the_only_non_null", "all_non_null"}
CONDITION = re.compile(r"\$\(inputs\.[A-Za-z][A-Za-z0-9_]*\)")


def validate_source_envelope(source: dict) -> None:
    """Validate the inert A5 source envelope while allowing incomplete drafts.

    The editor accepts only the same bounded workflow features as the trusted
    compiler.  It never accepts a user command, expression, image, path or
    external reference.  Complete graph/type validation remains the job of the
    asynchronous validator and cwltool container.
    """

    require(isinstance(source, dict), "MALFORMED_REQUEST", "Workflow source must be an object", 422)
    try:
        encoded = json.dumps(
            source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError):
        require(False, "MALFORMED_REQUEST", "Workflow source must be finite JSON", 422)
        return
    require(
        len(encoded) <= MAX_SOURCE_BYTES,
        "MALFORMED_REQUEST",
        "Workflow source exceeds 1 MiB",
        422,
    )
    require(
        set(source) == {"profile", "name", "entrypoint", "documents", "implementations"},
        "UNSUPPORTED_TARGET",
        "Workflow source fields differ from the bounded editor profile",
        422,
    )
    require(
        source.get("profile") == "v7-cwl-p4-v1",
        "UNSUPPORTED_TARGET",
        "Unsupported workflow profile",
        422,
    )
    _safe_json(source, 0, [0])
    documents = source.get("documents", {})
    implementations = source.get("implementations", {})
    require(isinstance(documents, dict), "MALFORMED_REQUEST", "Documents must be an object", 422)
    require(
        isinstance(implementations, dict),
        "MALFORMED_REQUEST",
        "Implementations must be an object",
        422,
    )
    for document_id, document in documents.items():
        _validate_document(document_id, document, documents, implementations)


def _safe_json(value: Any, depth: int, count: list[int]) -> None:
    require(depth <= 16, "MALFORMED_REQUEST", "Workflow source is too deeply nested", 422)
    count[0] += 1
    require(count[0] <= 50_000, "MALFORMED_REQUEST", "Workflow source has too many values", 422)
    if isinstance(value, dict):
        require(
            not (set(value) & FORBIDDEN_KEYS),
            "UNSUPPORTED_TARGET",
            "Executable workflow fields are forbidden in the bounded editor",
            422,
        )
        for key, item in value.items():
            require(isinstance(key, str), "MALFORMED_REQUEST", "Workflow keys must be text", 422)
            _safe_json(item, depth + 1, count)
    elif isinstance(value, list):
        require(len(value) <= 4096, "MALFORMED_REQUEST", "Workflow array is too large", 422)
        for item in value:
            _safe_json(item, depth + 1, count)
    elif isinstance(value, str):
        require(
            not value.startswith(("http://", "https://", "file://")),
            "UNSUPPORTED_TARGET",
            "External workflow references are forbidden",
            422,
        )


def _validate_requirements(value: Any) -> None:
    require(
        isinstance(value, dict)
        and set(value) <= ALLOWED_REQUIREMENTS
        and all(item == {} for item in value.values()),
        "UNSUPPORTED_TARGET",
        "Only bounded workflow feature requirements are accepted",
        422,
    )


def _validate_binding(value: Any) -> None:
    if isinstance(value, str):
        require(value != "", "INVALID_BINDING", "Empty source reference", 422)
        return
    require(isinstance(value, dict), "INVALID_BINDING", "Invalid step input binding", 422)
    require(
        set(value) <= ALLOWED_BINDING_FIELDS,
        "UNSUPPORTED_TARGET",
        "Step binding contains an unsupported expression or field",
        422,
    )
    if "source" in value:
        sources = value["source"]
        require(
            isinstance(sources, str)
            or isinstance(sources, list)
            and sources
            and all(isinstance(item, str) and item for item in sources),
            "INVALID_BINDING",
            "Step source must be a name or a non-empty list of names",
            422,
        )
    if "default" in value:
        _safe_json(value["default"], 0, [0])
    if "linkMerge" in value:
        require(
            value["linkMerge"] in ALLOWED_LINK_MERGE,
            "UNSUPPORTED_TARGET",
            "Unsupported linkMerge mode",
            422,
        )
    if "pickValue" in value:
        require(
            value["pickValue"] in ALLOWED_PICK_VALUE,
            "UNSUPPORTED_TARGET",
            "Unsupported pickValue mode",
            422,
        )
    require(
        not ({"linkMerge", "pickValue"} <= set(value)),
        "UNSUPPORTED_TARGET",
        "linkMerge and pickValue cannot be combined",
        422,
    )


def _validate_document(
    document_id: str,
    document: Any,
    documents: dict[str, Any],
    implementations: dict[str, Any],
) -> None:
    require(
        isinstance(document, dict), "MALFORMED_REQUEST", "Workflow document must be an object", 422
    )
    require(
        set(document) <= ALLOWED_DOCUMENT_FIELDS,
        "UNSUPPORTED_TARGET",
        "Workflow document contains unsupported fields",
        422,
    )
    require(
        document.get("class") == "Workflow" and document.get("cwlVersion", "v1.2") == "v1.2",
        "UNSUPPORTED_TARGET",
        "Only CWL 1.2 Workflow documents are editable",
        422,
    )
    if "requirements" in document:
        _validate_requirements(document["requirements"])
    for key in ("inputs", "outputs", "steps"):
        require(
            isinstance(document.get(key, {}), dict),
            "MALFORMED_REQUEST",
            f"Workflow {key} must be an object",
            422,
        )
    require(
        len(document.get("inputs", {})) <= 128
        and len(document.get("outputs", {})) <= 128
        and len(document.get("steps", {})) <= 1000,
        "MALFORMED_REQUEST",
        "Workflow document exceeds the bounded graph limits",
        422,
    )
    for step_key, step in document.get("steps", {}).items():
        require(
            valid_identifier(step_key) and isinstance(step, dict),
            "INVALID_BINDING",
            "Invalid workflow step",
            422,
        )
        require(
            set(step) <= ALLOWED_STEP_FIELDS,
            "UNSUPPORTED_TARGET",
            "Workflow step contains unsupported fields",
            422,
        )
        run = step.get("run")
        if run is not None:
            require(
                isinstance(run, str) and valid_identifier(run),
                "INVALID_BINDING",
                "Workflow step run must be a local identifier",
                422,
            )
        bindings = step.get("in", {})
        require(isinstance(bindings, dict), "INVALID_BINDING", "Step inputs must be an object", 422)
        for binding in bindings.values():
            _validate_binding(binding)
        outputs = step.get("out", [])
        require(
            isinstance(outputs, list)
            and len(outputs) == len(set(outputs))
            and all(valid_identifier(item) for item in outputs),
            "INVALID_BINDING",
            "Step outputs must be unique identifiers",
            422,
        )
        scatter = step.get("scatter", [])
        scatter = [scatter] if isinstance(scatter, str) else scatter
        require(
            isinstance(scatter, list)
            and len(scatter) == len(set(scatter))
            and all(valid_identifier(item) for item in scatter)
            and set(scatter) <= set(bindings),
            "INVALID_BINDING",
            "Scatter inputs must be unique bound ports",
            422,
        )
        if "scatterMethod" in step:
            require(
                bool(scatter) and step["scatterMethod"] in ALLOWED_SCATTER_METHODS,
                "UNSUPPORTED_TARGET",
                "Unsupported or unbound scatter method",
                422,
            )
        when = step.get("when")
        require(
            when is None or isinstance(when, str) and CONDITION.fullmatch(when),
            "UNSUPPORTED_TARGET",
            "Condition must be an exact boolean input reference",
            422,
        )


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


def preview_graph(
    source: dict,
    descriptor: dict,
    resolved_bindings: dict[str, dict],
    parameter_validator: Callable[[str, Any], list[dict]],
) -> dict:
    """Validate one bounded graph and propagate only trusted structural facts."""

    validate_source_envelope(source)
    if _advanced_source(source):
        return _preview_advanced_graph(source, descriptor, resolved_bindings, parameter_validator)
    issues: list[dict] = []
    documents = source["documents"]
    document = documents.get(source["entrypoint"])
    if not isinstance(document, dict):
        issues.append(_issue("ENTRYPOINT_NOT_FOUND", "Entrypoint document is missing"))
        return {"status": "INCOMPATIBLE", "nodes": [], "issues": issues}
    allowed_document = {"cwlVersion", "class", "inputs", "outputs", "steps", "label", "doc"}
    if (
        set(document) - allowed_document
        or document.get("class") != "Workflow"
        or document.get("cwlVersion", "v1.2") != "v1.2"
    ):
        issues.append(_issue("UNSUPPORTED_SOURCE", "Only the bounded root Workflow is editable"))
    inputs = document.get("inputs", {})
    outputs = document.get("outputs", {})
    steps = document.get("steps", {})
    if not isinstance(inputs, dict) or not isinstance(outputs, dict) or not isinstance(steps, dict):
        issues.append(
            _issue("INVALID_SOURCE", "Workflow inputs, outputs and steps must be objects")
        )
        return {"status": "INCOMPATIBLE", "nodes": [], "issues": issues}
    if not inputs:
        issues.append(_issue("WORKFLOW_INPUT_REQUIRED", "Workflow needs at least one data input"))
    if not outputs:
        issues.append(
            _issue("WORKFLOW_OUTPUT_REQUIRED", "Workflow needs at least one final output")
        )
    if not steps:
        issues.append(_issue("WORKFLOW_STEP_REQUIRED", "Workflow needs at least one operator step"))

    descriptors = {
        (item["operator_id"], item["version_id"]): item for item in descriptor["operators"]
    }
    implementations = source["implementations"]
    root_structures = {
        key: structure_from_binding(value, key)
        for key, value in resolved_bindings.items()
        if key in inputs
    }
    for key in inputs:
        root_structures.setdefault(key, _unknown(key))

    ordered, cyclic = _topological(steps)
    for key in sorted(cyclic):
        issues.append(
            _issue("WORKFLOW_CYCLE", "Workflow data dependencies contain a cycle", step=key)
        )
    node_outputs: dict[str, dict[str, dict]] = {}
    node_views = []
    for step_key in ordered:
        step = steps[step_key]
        node_issues_start = len(issues)
        needs_binding = False
        if not valid_identifier(step_key) or not isinstance(step, dict):
            issues.append(_issue("INVALID_STEP", "Invalid step identity or value", step=step_key))
            continue
        if set(step) - {"run", "in", "out", "label", "doc"}:
            issues.append(
                _issue(
                    "UNSUPPORTED_STEP", "Advanced step features are read-only in A2", step=step_key
                )
            )
        run = step.get("run")
        target = implementations.get(run) if isinstance(run, str) else None
        if target is None:
            issues.append(
                _issue(
                    "UNKNOWN_IMPLEMENTATION", "Step implementation is not declared", step=step_key
                )
            )
            continue
        operator = descriptors.get((target.get("operator_id"), target.get("version_id")))
        if operator is None:
            issues.append(
                _issue(
                    "UNKNOWN_OPERATOR", "Step operator is not in the trusted catalog", step=step_key
                )
            )
            continue
        allowed_implementations = {
            item["implementation_id"] for item in operator.get("implementations", [])
        }
        binding = target.get("execution_binding", {})
        if (
            binding.get("kind") != "LOCAL"
            or binding.get("implementation_id") not in allowed_implementations
        ):
            issues.append(
                _issue(
                    "UNKNOWN_IMPLEMENTATION", "Step implementation is not trusted", step=step_key
                )
            )

        bindings = step.get("in", {})
        if not isinstance(bindings, dict):
            issues.append(_issue("INVALID_BINDING", "Step inputs must be an object", step=step_key))
            bindings = {}
        parameters_binding = bindings.get("parameters")
        parameters = (
            parameters_binding.get("default")
            if isinstance(parameters_binding, dict) and set(parameters_binding) == {"default"}
            else None
        )
        if parameters is None:
            issues.append(
                _issue(
                    "PARAMETERS_REQUIRED",
                    "A2 operator parameters must be fixed",
                    step=step_key,
                    port="parameters",
                )
            )
            parameters = {}
        else:
            for error in parameter_validator(operator["operator_id"], parameters):
                issues.append(
                    _issue(
                        error["code"],
                        error["message"],
                        step=step_key,
                        port="parameters",
                        pointer=error["pointer"],
                    )
                )

        input_structures = {}
        for port, expected in operator["input_ports"].items():
            raw = bindings.get(port)
            source_ref = (
                raw
                if isinstance(raw, str)
                else raw.get("source")
                if isinstance(raw, dict) and set(raw) == {"source"}
                else None
            )
            if not isinstance(source_ref, str):
                issues.append(
                    _issue(
                        "INPUT_REQUIRED",
                        "Required data input is not connected",
                        step=step_key,
                        port=port,
                    )
                )
                continue
            if "/" in source_ref:
                parent, output_port = source_ref.split("/", 1)
                structure = node_outputs.get(parent, {}).get(output_port)
            else:
                structure = root_structures.get(source_ref)
            if structure is None:
                issues.append(
                    _issue(
                        "SOURCE_NOT_FOUND",
                        "Connected source port does not exist",
                        step=step_key,
                        port=port,
                    )
                )
                continue
            input_structures[port] = structure
            actual_kind = structure["representation_kind"]
            expected_kind = expected["representation_kind"]
            if actual_kind == "UNKNOWN":
                needs_binding = True
                issues.append(
                    _issue(
                        "REQUIRES_BINDING",
                        "Input structure requires an exact sample binding",
                        step=step_key,
                        port=port,
                        severity="INFO",
                    )
                )
            elif actual_kind != expected_kind:
                issues.append(
                    _issue(
                        "PORT_TYPE_MISMATCH",
                        f"{actual_kind} output cannot connect to {expected_kind} input",
                        step=step_key,
                        port=port,
                    )
                )

        table_input = next(
            (
                value
                for value in input_structures.values()
                if value["representation_kind"] == "TABLE"
            ),
            None,
        )
        fields = _field_map(table_input) if table_input else None
        if operator["operator_id"].startswith("table-") and table_input and fields is None:
            needs_binding = True
        if fields is not None:
            for field_id, pointer in _referenced_fields(operator["operator_id"], parameters):
                if field_id not in fields:
                    issues.append(
                        _issue(
                            "INPUT_FIELD_NOT_FOUND",
                            "Parameter references a field absent from the upstream table",
                            step=step_key,
                            port="parameters",
                            pointer=pointer,
                        )
                    )
            for pointer, message in _field_type_issues(operator["operator_id"], parameters, fields):
                issues.append(
                    _issue(
                        "INPUT_FIELD_TYPE_MISMATCH",
                        message,
                        step=step_key,
                        port="parameters",
                        pointer=pointer,
                    )
                )

        declared_out = step.get("out", [])
        if not isinstance(declared_out, list) or len(declared_out) != len(set(declared_out)):
            issues.append(
                _issue(
                    "INVALID_OUTPUT", "Step output list must contain unique ports", step=step_key
                )
            )
            declared_out = []
        unknown_out = set(declared_out) - set(operator["output_ports"])
        if unknown_out:
            issues.append(
                _issue("UNKNOWN_OUTPUT", "Step exposes an unknown output port", step=step_key)
            )
        produced = {}
        for port in declared_out:
            if port not in operator["output_ports"]:
                continue
            structure = _port_structure(operator["output_ports"][port], f"{step_key}/{port}")
            if port == "table" and table_input is not None:
                transformed = _transform_table(operator["operator_id"], parameters, table_input)
                if transformed is not None:
                    structure = transformed | {"source": f"{step_key}/{port}"}
            produced[port] = structure
        node_outputs[step_key] = produced
        node_errors = [
            value for value in issues[node_issues_start:] if value["severity"] == "ERROR"
        ]
        node_views.append(
            {
                "step_key": step_key,
                "status": "INCOMPATIBLE"
                if node_errors
                else "REQUIRES_BINDING"
                if needs_binding
                else "COMPATIBLE",
                "outputs": produced,
            }
        )

    for name, output in outputs.items():
        source_ref = output.get("outputSource") if isinstance(output, dict) else None
        if not isinstance(source_ref, str) or "/" not in source_ref:
            issues.append(
                _issue(
                    "FINAL_OUTPUT_SOURCE_REQUIRED",
                    "Final output must reference a step output",
                    port=name,
                )
            )
            continue
        step_key, port = source_ref.split("/", 1)
        if port not in node_outputs.get(step_key, {}):
            issues.append(
                _issue(
                    "FINAL_OUTPUT_SOURCE_NOT_FOUND", "Final output source does not exist", port=name
                )
            )

    errors = any(value["severity"] == "ERROR" for value in issues)
    requires = any(value["code"] == "REQUIRES_BINDING" for value in issues)
    return {
        "status": "INCOMPATIBLE" if errors else "REQUIRES_BINDING" if requires else "COMPATIBLE",
        "nodes": node_views,
        "issues": issues,
    }


def _advanced_source(source: dict) -> bool:
    documents = source.get("documents", {})
    if len(documents) != 1:
        return True
    for document in documents.values():
        if document.get("requirements"):
            return True
        for step in document.get("steps", {}).values():
            if isinstance(step, dict) and (
                {"scatter", "scatterMethod", "when"} & set(step) or step.get("run") in documents
            ):
                return True
    return False


def _contains_file_type(value: Any) -> bool:
    if isinstance(value, str):
        return value == "File"
    if isinstance(value, list):
        return any(_contains_file_type(item) for item in value)
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "array":
            return _contains_file_type(value.get("items"))
        if kind == "record":
            return any(_contains_file_type(item.get("type")) for item in value.get("fields", []))
        return _contains_file_type(kind)
    return False


def _advanced_output_structure(port: dict, source: str, *, scatter: bool, nullable: bool) -> dict:
    value = _port_structure(port, source)
    value["shape"] = "ARRAY" if scatter else "SCALAR"
    value["nullable"] = nullable
    return value


def _preview_advanced_graph(
    source: dict,
    descriptor: dict,
    resolved_bindings: dict[str, dict],
    parameter_validator: Callable[[str, Any], list[dict]],
) -> dict:
    """Locate A5 issues without pretending to execute or expand the workflow.

    Formal CWL type/shape validation still runs in the fixed validation
    container.  This projection supplies deterministic editor locations and
    only propagates structures that come from the trusted operator descriptor.
    """

    documents = source["documents"]
    implementations = source["implementations"]
    operators = {
        (item["operator_id"], item["version_id"]): item for item in descriptor["operators"]
    }
    issues: list[dict] = []
    nodes: list[dict] = []
    visited: set[tuple[str, tuple[str, ...]]] = set()

    root = documents.get(source["entrypoint"], {})
    for name, declaration in root.get("inputs", {}).items():
        declared_type = declaration.get("type") if isinstance(declaration, dict) else declaration
        if _contains_file_type(declared_type) and name not in resolved_bindings:
            issues.append(
                _issue(
                    "REQUIRES_BINDING",
                    "Root input structure requires an exact sample binding",
                    port=name,
                    severity="INFO",
                    document=source["entrypoint"],
                )
            )

    def walk(reference: str, path: list[str]) -> dict[str, dict]:
        identity = (reference, tuple(path))
        if identity in visited:
            return {}
        visited.add(identity)
        document = documents[reference]
        ordered, cyclic = _topological(document.get("steps", {}))
        for name in sorted(cyclic):
            issues.append(
                _issue(
                    "WORKFLOW_CYCLE",
                    "Workflow data dependencies contain a cycle",
                    step=name,
                    document=reference,
                )
            )
        produced_by_step: dict[str, dict[str, dict]] = {}
        for step_key in ordered:
            step = document["steps"][step_key]
            run = step.get("run")
            call_path = [*path, step_key]
            scatter = bool(step.get("scatter"))
            nullable = bool(step.get("when"))
            if run in documents:
                produced = walk(run, call_path)
                produced = {
                    key: value
                    | {
                        "source": f"{step_key}/{key}",
                        "shape": "ARRAY" if scatter else value.get("shape", "SCALAR"),
                        "nullable": nullable or bool(value.get("nullable")),
                    }
                    for key, value in produced.items()
                }
                produced_by_step[step_key] = produced
                nodes.append(
                    {
                        "step_key": step_key,
                        "document_id": reference,
                        "call_path": call_path,
                        "node_kind": "SUBWORKFLOW",
                        "status": "COMPATIBLE",
                        "outputs": produced,
                    }
                )
                continue
            target = implementations.get(run)
            operator = (
                operators.get((target.get("operator_id"), target.get("version_id")))
                if isinstance(target, dict)
                else None
            )
            node_start = len(issues)
            if operator is None:
                issues.append(
                    _issue(
                        "UNKNOWN_OPERATOR",
                        "Step operator is not in the trusted catalog",
                        step=step_key,
                        document=reference,
                    )
                )
                produced_by_step[step_key] = {}
                continue
            allowed = {item["implementation_id"] for item in operator.get("implementations", [])}
            binding = target.get("execution_binding", {})
            if binding.get("kind") != "LOCAL" or binding.get("implementation_id") not in allowed:
                issues.append(
                    _issue(
                        "UNKNOWN_IMPLEMENTATION",
                        "Step implementation is not trusted",
                        step=step_key,
                        document=reference,
                    )
                )
            bindings = step.get("in", {})
            parameter_binding = bindings.get("parameters") if isinstance(bindings, dict) else None
            parameters = (
                parameter_binding.get("default")
                if isinstance(parameter_binding, dict) and set(parameter_binding) == {"default"}
                else None
            )
            if parameters is None:
                issues.append(
                    _issue(
                        "PARAMETERS_REQUIRED",
                        "Operator parameters must be fixed data",
                        step=step_key,
                        port="parameters",
                        document=reference,
                    )
                )
            else:
                for error in parameter_validator(operator["operator_id"], parameters):
                    issues.append(
                        _issue(
                            error["code"],
                            error["message"],
                            step=step_key,
                            port="parameters",
                            pointer=error["pointer"],
                            document=reference,
                        )
                    )
            for port in operator["input_ports"]:
                if port not in bindings:
                    issues.append(
                        _issue(
                            "INPUT_REQUIRED",
                            "Required data input is not connected",
                            step=step_key,
                            port=port,
                            document=reference,
                        )
                    )
            declared = step.get("out", [])
            unknown = set(declared) - set(operator["output_ports"])
            if unknown:
                issues.append(
                    _issue(
                        "UNKNOWN_OUTPUT",
                        "Step exposes an unknown output port",
                        step=step_key,
                        document=reference,
                    )
                )
            produced = {
                port: _advanced_output_structure(
                    operator["output_ports"][port],
                    f"{step_key}/{port}",
                    scatter=scatter,
                    nullable=nullable,
                )
                for port in declared
                if port in operator["output_ports"]
            }
            produced_by_step[step_key] = produced
            errors = any(item["severity"] == "ERROR" for item in issues[node_start:])
            nodes.append(
                {
                    "step_key": step_key,
                    "document_id": reference,
                    "call_path": call_path,
                    "node_kind": "OPERATOR",
                    "status": "INCOMPATIBLE" if errors else "COMPATIBLE",
                    "outputs": produced,
                }
            )
        workflow_outputs: dict[str, dict] = {}
        for name, declaration in document.get("outputs", {}).items():
            source_ref = declaration.get("outputSource") if isinstance(declaration, dict) else None
            if isinstance(source_ref, str) and "/" in source_ref:
                parent, port = source_ref.split("/", 1)
                structure = produced_by_step.get(parent, {}).get(port)
                if structure is not None:
                    workflow_outputs[name] = deepcopy(structure)
                    continue
            issues.append(
                _issue(
                    "FINAL_OUTPUT_SOURCE_NOT_FOUND",
                    "Final output source does not exist",
                    port=name,
                    document=reference,
                )
            )
        return workflow_outputs

    if source["entrypoint"] in documents:
        walk(source["entrypoint"], [])
    errors = any(item["severity"] == "ERROR" for item in issues)
    requires = any(item["code"] == "REQUIRES_BINDING" for item in issues)
    return {
        "status": "INCOMPATIBLE" if errors else "REQUIRES_BINDING" if requires else "COMPATIBLE",
        "nodes": sorted(nodes, key=lambda item: (item.get("call_path", []), item["step_key"])),
        "issues": issues,
    }


def environment_stale_reasons(frozen: dict, current: dict) -> list[str]:
    names = (
        "catalog_digest",
        "semantic_contract_digest",
        "compiler_digest",
        "validator_image_digest",
    )
    return [name.upper() + "_CHANGED" for name in names if frozen.get(name) != current.get(name)]
