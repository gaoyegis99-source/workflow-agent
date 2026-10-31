"""Pure request-contract validation for the Execution HTTP API."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated

from fastapi import Path
from platform_contracts.service import digest, ensure

from platform_execution.adapters.repository import Repository

Id = Annotated[
    str,
    Path(pattern=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$(?![\s\S])")),
]


def plan_fingerprint(value: object) -> str:
    return "sha256:" + digest(value)


def validate_execution_plan(action: str, body: dict) -> None:
    """Defend the immutable Execution payload required by every new workflow."""

    plan = body.get("execution_plan")
    ensure(
        isinstance(plan, dict)
        and set(plan)
        == {
            "schema_version",
            "package_digest",
            "input_digest",
            "nodes",
            "deferred_templates",
            "plan_digest",
        }
        and plan.get("schema_version") == 1
        and plan.get("package_digest") == body.get("compiled", {}).get("manifest_digest")
        and plan.get("input_digest") == plan_fingerprint(body.get("inputs", {}))
        and plan.get("plan_digest")
        == plan_fingerprint({key: value for key, value in plan.items() if key != "plan_digest"})
        and isinstance(plan.get("nodes"), list)
        and isinstance(plan.get("deferred_templates"), list)
        and len(plan["nodes"]) <= 1000
        and len(plan["deferred_templates"]) <= 1000,
        "INVALID_BINDING",
        "Invalid immutable workflow execution plan",
        422,
    )
    execution = Repository.identity(action)
    invocations = body["compiled"]["invocations"]
    node_ids = set()
    executable = 0
    for node in plan["nodes"]:
        ensure(
            isinstance(node, dict)
            and set(node)
            == {
                "node_id",
                "call_path",
                "scatter_indices",
                "dependencies",
                "operator_id",
                "implementation_digest",
                "input_binding_digest",
                "condition",
                "output_ports",
            }
            and isinstance(node.get("call_path"), list)
            and all(isinstance(part, str) and part for part in node["call_path"])
            and isinstance(node.get("scatter_indices"), list)
            and all(type(index) is int and index >= 0 for index in node["scatter_indices"])
            and type(node.get("condition")) in {type(None), bool}
            and isinstance(node.get("dependencies"), list)
            and all(isinstance(item, str) for item in node["dependencies"])
            and isinstance(node.get("output_ports"), list)
            and all(isinstance(item, str) for item in node["output_ports"]),
            "INVALID_BINDING",
            "Invalid planned workflow node",
            422,
        )
        invocation = invocations.get("/".join(node["call_path"]))
        target = invocation.get("target") if invocation else None
        ensure(
            invocation is not None
            and node["node_id"]
            == "node_"
            + hashlib.sha256(
                json.dumps(
                    [execution, node["call_path"], node["scatter_indices"]],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            and node["node_id"] not in node_ids
            and node["operator_id"] == target.get("operator_id")
            and node["implementation_digest"] == plan_fingerprint(target)
            and node["output_ports"] == sorted(invocation["outputs"]),
            "INVALID_BINDING",
            "Planned workflow node does not match the frozen package",
            422,
        )
        node_ids.add(node["node_id"])
        executable += node["condition"] is not False
    ensure(
        executable <= body.get("max_node_executions", 0)
        and all(set(node["dependencies"]) <= node_ids for node in plan["nodes"]),
        "INVALID_BINDING",
        "Workflow plan dependencies or node limit are invalid",
        422,
    )
    template_ids = set()
    for template in plan["deferred_templates"]:
        ensure(
            isinstance(template, dict)
            and set(template)
            == {
                "template_id",
                "call_path",
                "index_depth",
                "dependencies",
                "operator_id",
                "implementation_digest",
                "input_binding_digest",
                "condition",
                "output_ports",
            }
            and isinstance(template.get("call_path"), list)
            and all(isinstance(part, str) and part for part in template["call_path"])
            and type(template.get("index_depth")) is int
            and 0 <= template["index_depth"] <= 32
            and template.get("condition") is None
            and isinstance(template.get("dependencies"), list)
            and all(isinstance(item, str) for item in template["dependencies"])
            and set(template["dependencies"]) <= node_ids
            and isinstance(template.get("output_ports"), list)
            and all(isinstance(item, str) for item in template["output_ports"]),
            "INVALID_BINDING",
            "Invalid deferred workflow template",
            422,
        )
        invocation = invocations.get("/".join(template["call_path"]))
        target = invocation.get("target") if invocation else None
        ensure(
            invocation is not None
            and isinstance(template.get("template_id"), str)
            and template["template_id"].startswith("template_")
            and template["template_id"] not in template_ids
            and template["operator_id"] == target.get("operator_id")
            and template["implementation_digest"] == plan_fingerprint(target)
            and template["output_ports"] == sorted(invocation["outputs"]),
            "INVALID_BINDING",
            "Deferred workflow template does not match the frozen package",
            422,
        )
        template_ids.add(template["template_id"])


__all__ = ("Id", "plan_fingerprint", "validate_execution_plan")
