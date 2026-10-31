"""Pure validation for immutable flat workflow engine packages.

The producer and consumer deliberately share this standard-library contract so
package acceptance cannot drift between Platform and Execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 1005
MEMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class EnginePackageViolation(ValueError):
    """A stable, transport-neutral engine package contract violation."""

    def __init__(self, message: str, code: str = "INVALID_BINDING", status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EnginePackageViolation(message)


def validate_engine_package(package: object, compiled: object, plan: object) -> dict:
    """Validate one immutable, fully flattened workflow engine package."""

    _require(isinstance(compiled, dict) and isinstance(plan, dict), "Workflow binding is invalid")
    _require(
        isinstance(package, dict)
        and set(package)
        == {
            "schema_version",
            "strategy",
            "source_package_digest",
            "plan_digest",
            "entrypoint",
            "files",
            "node_map",
            "package_digest",
        }
        and package.get("schema_version") == 1
        and package.get("strategy") == "flat_v1"
        and package.get("source_package_digest") == compiled.get("manifest_digest")
        and package.get("plan_digest") == plan.get("plan_digest")
        and isinstance(package.get("files"), dict)
        and 0 < len(package["files"]) <= MAX_FILES
        and package.get("entrypoint") in package["files"]
        and all(
            isinstance(name, str) and MEMBER.fullmatch(name) and isinstance(document, dict)
            for name, document in package["files"].items()
        )
        and isinstance(package.get("node_map"), dict),
        "Invalid immutable workflow engine package",
    )
    frozen = {key: value for key, value in package.items() if key != "package_digest"}
    _require(
        package["package_digest"] == fingerprint(frozen) and len(canonical(package)) <= MAX_BYTES,
        "Workflow engine package digest or size is invalid",
    )
    _require(
        not plan.get("deferred_templates"),
        "Deferred workflow templates are not executable by the flat engine",
    )
    nodes = plan.get("nodes")
    _require(
        isinstance(nodes, list)
        and all(isinstance(node, dict) and isinstance(node.get("node_id"), str) for node in nodes),
        "Workflow execution plan nodes are invalid",
    )
    expected = {node["node_id"]: node for node in nodes if node.get("condition") is not False}
    _require(set(package["node_map"]) == set(expected), "Engine node map differs from plan")
    for node_id, item in package["node_map"].items():
        node = expected[node_id]
        _require(
            isinstance(item, dict)
            and set(item)
            == {
                "node_id",
                "call_path",
                "scatter_indices",
                "operator_id",
                "implementation_digest",
                "output_ports",
                "step_id",
            }
            and item["node_id"] == node_id
            and item["call_path"] == node["call_path"]
            and item["scatter_indices"] == node["scatter_indices"]
            and item["operator_id"] == node["operator_id"]
            and item["implementation_digest"] == node["implementation_digest"]
            and item["output_ports"] == node["output_ports"]
            and isinstance(item["step_id"], str)
            and MEMBER.fullmatch(item["step_id"]),
            "Engine node map entry differs from plan",
        )
    root = package["files"][package["entrypoint"]]
    _require(
        root.get("class") == "Workflow"
        and isinstance(root.get("steps"), dict)
        and "v7_gather" in root.get("steps", {})
        and all(item["step_id"] in root["steps"] for item in package["node_map"].values())
        and not any("scatter" in step for step in root["steps"].values()),
        "Flat workflow graph is inconsistent",
    )
    return package
