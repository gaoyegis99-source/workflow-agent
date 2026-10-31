"""Advanced workflow-preview projection for subworkflows and scatter."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from platform_core.domain.authoring_graph_structure import _issue, _port_structure, _topological


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
