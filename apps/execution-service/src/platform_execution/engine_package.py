"""Validation for immutable workflow engine packages received from Platform."""

import json
import re

from platform_contracts.service import digest, ensure

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 1005
MEMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def fingerprint(value):
    return "sha256:" + digest(value)


def validate(body):
    """Validate package, plan binding, node map and bounded file names."""
    package = body.get("engine_package")
    if package is None:
        return None
    compiled, plan = body.get("compiled", {}), body.get("execution_plan", {})
    ensure(
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
        and package.get("strategy") in {"flat_v1", "legacy_v1"}
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
        "INVALID_BINDING",
        "Invalid immutable workflow engine package",
        422,
    )
    frozen = {key: value for key, value in package.items() if key != "package_digest"}
    ensure(
        package["package_digest"] == fingerprint(frozen)
        and len(
            json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        <= MAX_BYTES,
        "INVALID_BINDING",
        "Workflow engine package digest or size is invalid",
        422,
    )
    expected = {
        node["node_id"]: node
        for node in plan.get("nodes", [])
        if node.get("condition") is not False
    }
    ensure(
        set(package["node_map"]) == set(expected),
        "INVALID_BINDING",
        "Engine node map differs from execution plan",
        422,
    )
    for node_id, item in package["node_map"].items():
        node = expected[node_id]
        ensure(
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
            and (
                item["step_id"] is None
                or isinstance(item["step_id"], str)
                and MEMBER.fullmatch(item["step_id"])
            ),
            "INVALID_BINDING",
            "Engine node map entry differs from execution plan",
            422,
        )
    deferred = bool(plan.get("deferred_templates"))
    ensure(
        package["strategy"] == ("legacy_v1" if deferred else "flat_v1"),
        "INVALID_BINDING",
        "Workflow engine strategy differs from execution plan",
        422,
    )
    if package["strategy"] == "flat_v1":
        root = package["files"][package["entrypoint"]]
        ensure(
            root.get("class") == "Workflow"
            and "v7_gather" in root.get("steps", {})
            and all(item["step_id"] in root["steps"] for item in package["node_map"].values())
            and not any("scatter" in step for step in root["steps"].values()),
            "INVALID_BINDING",
            "Flat workflow graph is inconsistent",
            422,
        )
    return package
