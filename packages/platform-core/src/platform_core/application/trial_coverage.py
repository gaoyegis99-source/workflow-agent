"""Pure Workflow trial output and coverage projection.

No database, HTTP client, or release service is needed to determine whether
the immutable facts of a trial cover its frozen case.  Keeping this algorithm
separate lets the worker own only lease/retry orchestration.
"""

from __future__ import annotations

from typing import Any

from platform_core.application.ports import Record
from platform_core.domain.control import require


def at_path(value: Any, path: list[Any]) -> Any:
    for component in path:
        if isinstance(component, int):
            require(
                isinstance(value, list) and 0 <= component < len(value),
                "INVALID_BINDING",
                "Trial output array path does not exist",
            )
            value = value[component]
        else:
            require(
                isinstance(value, dict) and component in value,
                "INVALID_BINDING",
                "Trial output record path does not exist",
            )
            value = value[component]
    return value


def matches_type(value: Any, schema: Any) -> bool:
    if isinstance(schema, list):
        return (value is None and "null" in schema) or any(
            item != "null" and matches_type(value, item) for item in schema
        )
    if isinstance(schema, dict):
        kind = schema.get("type")
        if kind == "array":
            return isinstance(value, list) and all(
                matches_type(item, schema["items"]) for item in value
            )
        if kind == "record":
            fields = schema.get("fields", [])
            return (
                isinstance(value, dict)
                and set(value) == {field["name"] for field in fields}
                and all(matches_type(value[field["name"]], field["type"]) for field in fields)
            )
        return matches_type(value, kind)
    if schema == "File":
        return isinstance(value, dict) and set(value) == {"asset", "representation_id"}
    matches = {
        "boolean": type(value) is bool,
        "int": type(value) is int,
        "long": type(value) is int,
        "float": type(value) in {int, float},
        "double": type(value) in {int, float},
        "string": isinstance(value, str),
    }
    return matches.get(schema, False)


def shape(value: Any, depth: int = 0) -> Any:
    require(depth <= 16, "INVALID_BINDING", "Trial output shape is too deeply nested")
    if isinstance(value, list):
        children = [shape(item, depth + 1) for item in value]
        unique = []
        for child in children:
            if child not in unique:
                unique.append(child)
        return {"kind": "ARRAY", "length": len(value), "item_shapes": unique}
    if isinstance(value, dict):
        if set(value) == {"asset", "representation_id"}:
            return {"kind": "FILE"}
        return {
            "kind": "RECORD",
            "fields": {key: shape(value[key], depth + 1) for key in sorted(value)},
        }
    if value is None:
        return {"kind": "NULL"}
    return {"kind": "SCALAR", "type": type(value).__name__}


def coverage(trial_case: Record, nodes: list[Record], outputs: Any) -> tuple[Record, list[str]]:
    planned_values = trial_case.get("planned_steps")
    if planned_values is None:
        # A3 trial cases predate explicit coverage facts.  Their required
        # steps were the complete static plan, so project those immutable
        # facts into the A5 model instead of treating every historical node as
        # an unexpected dynamic instance.
        planned_values = [
            {**item, "expected": "EXECUTED"} for item in trial_case.get("required_steps", [])
        ]
    planned = {item["node_id"]: item for item in planned_values}
    templates = trial_case.get("deferred_templates", [])
    actual = {item["node_id"]: item for item in nodes}
    violations = []
    instances: dict[str, Record] = {}

    def bucket(path):
        key = "/".join(path)
        return instances.setdefault(
            key,
            {
                "call_path": path,
                "planned": 0,
                "observed": 0,
                "started": 0,
                "succeeded": 0,
                "skipped": 0,
            },
        )

    for item in planned.values():
        value = bucket(item["call_path"])
        value["planned"] += 1
        if item["expected"] == "SKIPPED":
            value["skipped"] += 1
            observed = actual.get(item["node_id"])
            if observed is not None and observed.get("executed"):
                violations.append(item["node_id"])
    for node_id, item in actual.items():
        request = item.get("request", {})
        path = request.get("call_path", [])
        indices = request.get("scatter_indices", [])
        static = planned.get(node_id)
        dynamic = next(
            (
                template
                for template in templates
                if template.get("call_path") == path and template.get("index_depth") == len(indices)
            ),
            None,
        )
        if static is None and dynamic is None:
            violations.append(node_id)
        value = bucket(path)
        value["observed"] += 1
        if item.get("executed"):
            value["started"] += 1
        if (item.get("observation") or {}).get("state") == "SUCCEEDED":
            value["succeeded"] += 1
    untested = [
        {
            "call_path": item["call_path"],
            "scatter_indices": item["scatter_indices"],
            "reason": "CONDITION_FALSE",
        }
        for item in planned.values()
        if item["expected"] == "SKIPPED"
    ]
    for template in templates:
        if not any(
            item.get("request", {}).get("call_path") == template["call_path"]
            and len(item.get("request", {}).get("scatter_indices", [])) == template["index_depth"]
            for item in nodes
        ):
            untested.append(
                {
                    "call_path": template["call_path"],
                    "scatter_indices": None,
                    "reason": "DYNAMIC_TEMPLATE_NOT_OBSERVED",
                }
            )
    result = {
        "profile": "workflow-trial-coverage-v1",
        "instances": sorted(instances.values(), key=lambda item: item["call_path"]),
        "executed_call_paths": [
            list(path)
            for path in sorted(
                {
                    tuple(item.get("request", {}).get("call_path", []))
                    for item in nodes
                    if item.get("executed")
                }
            )
        ],
        "untested_branches": untested,
        "final_output_shapes": {key: shape(value) for key, value in sorted(outputs.items())}
        if isinstance(outputs, dict)
        else {},
        "positive_compute": any(item.get("executed") for item in nodes),
    }
    return result, sorted(set(violations))
