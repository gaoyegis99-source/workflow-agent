"""Ordinary bounded-workflow preview projection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from platform_core.domain.authoring_graph_advanced import (
    _advanced_source,
    _preview_advanced_graph,
)
from platform_core.domain.authoring_graph_envelope import validate_source_envelope
from platform_core.domain.authoring_graph_structure import (
    _field_map,
    _field_type_issues,
    _issue,
    _port_structure,
    _referenced_fields,
    _topological,
    _transform_table,
    _unknown,
    structure_from_binding,
)
from platform_core.domain.control import valid_identifier


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
