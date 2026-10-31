"""Bounded authoring-source envelope validation."""

from __future__ import annotations

import json
import re
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
