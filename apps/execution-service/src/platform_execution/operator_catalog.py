"""Independent validation of the deployment Operator Catalog lock."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from platform_execution.implementations import COMPUTE_IMPLEMENTATIONS

SHA256 = "sha256:"
LIFECYCLES = {"ACTIVE", "DISABLED", "REVOKED"}
OWNERSHIPS = {"PLATFORM", "CURATED_PACKAGE", "DOMAIN_PACKAGE"}
TRUST_LEVELS = {"BUILTIN", "VERIFIED"}
MAX_CATALOG_BYTES = 1024 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def digest(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return SHA256 + hashlib.sha256(payload).hexdigest()


def valid_digest(value):
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith(SHA256)
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def bounded(value, depth=0, count=None):
    count = count if count is not None else [0]
    count[0] += 1
    if depth > 16 or count[0] > 4096:
        return False
    if value is None or type(value) in {bool, int, float}:
        return True
    if isinstance(value, str):
        return len(value) <= 4096
    if isinstance(value, list):
        return len(value) <= 1024 and all(bounded(item, depth + 1, count) for item in value)
    if isinstance(value, dict):
        return len(value) <= 256 and all(
            isinstance(key, str) and len(key) <= 128 and bounded(item, depth + 1, count)
            for key, item in value.items()
        )
    return False


def ports(value):
    return (
        isinstance(value, dict)
        and 0 < len(value) <= 64
        and all(IDENTIFIER.fullmatch(key) and item == "File" for key, item in value.items())
    )


def validate(value):
    if not (
        isinstance(value, dict)
        and set(value) == {"schema_version", "operators", "catalog_digest"}
        and value.get("schema_version") == 1
        and isinstance(value.get("operators"), list)
        and 0 < len(value["operators"]) <= 256
        and value.get("catalog_digest")
        == digest({key: item for key, item in value.items() if key != "catalog_digest"})
    ):
        raise ValueError("Invalid Operator Catalog lock")
    seen_operators, seen_implementations = set(), set()
    for operator in value["operators"]:
        required = {
            "operator_id",
            "version_id",
            "operator_digest",
            "ownership",
            "trust_level",
            "lifecycle",
            "inputs",
            "parameters",
            "outputs",
            "effects",
            "supports_workflow",
            "implementations",
            "legacy",
        }
        key = (operator.get("operator_id"), operator.get("version_id"))
        if not (
            isinstance(operator, dict)
            and set(operator) == required
            and key not in seen_operators
            and isinstance(key[0], str)
            and IDENTIFIER.fullmatch(key[0])
            and isinstance(key[1], str)
            and VERSION.fullmatch(key[1])
            and key[1].lower() != "latest"
            and operator.get("ownership") in OWNERSHIPS
            and operator.get("trust_level") in TRUST_LEVELS
            and operator.get("lifecycle") in LIFECYCLES
            and operator.get("effects") == ["CONSUMES_EXECUTION_BUDGET", "CREATES_ASSETS"]
            and type(operator.get("supports_workflow")) is bool
            and ports(operator.get("inputs"))
            and ports(operator.get("outputs"))
            and isinstance(operator.get("parameters"), dict)
            and bounded(operator["parameters"])
            and isinstance(operator.get("implementations"), list)
            and 0 < len(operator["implementations"]) <= 8
        ):
            raise ValueError("Duplicate or invalid operator definition")
        seen_operators.add(key)
        semantic = {
            name: operator.get(name)
            for name in (
                "operator_id",
                "version_id",
                "ownership",
                "trust_level",
                "inputs",
                "parameters",
                "outputs",
                "effects",
                "supports_workflow",
                "legacy",
            )
        }
        if operator.get("operator_digest") != digest(semantic):
            raise ValueError("Operator semantic digest differs")
        for implementation in operator.get("implementations", []):
            identity = implementation.get("implementation_id")
            local = COMPUTE_IMPLEMENTATIONS.get(identity)
            executable = {
                name: implementation.get(name)
                for name in (
                    "implementation_id",
                    "binding_kind",
                    "runtime",
                    "image_digest",
                    "protocol",
                    "resource_profile",
                )
            }
            if not (
                identity not in seen_implementations
                and isinstance(identity, str)
                and IDENTIFIER.fullmatch(identity)
                and local is not None
                and executable["binding_kind"] == "LOCAL"
                and valid_digest(executable["image_digest"])
                and executable["protocol"] == local["protocol"]
                and executable["resource_profile"] == local["resource_profile"]
                and implementation.get("implementation_digest") == digest(executable)
            ):
                raise ValueError("Operator implementation differs from executable registry")
            seen_implementations.add(identity)
            if len(seen_implementations) > 1024:
                raise ValueError("Too many operator implementations")
    return value


def load(path):
    try:
        source = Path(path)
        if source.stat().st_size > MAX_CATALOG_BYTES:
            raise ValueError("Operator Catalog is too large")
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Operator Catalog cannot be read") from error
    return validate(value)


def exact_snapshot(catalog, frozen):
    if not isinstance(frozen, dict) or frozen.get("catalog_digest") != catalog["catalog_digest"]:
        raise ValueError("Platform and Execution Operator Catalogs differ")
    operator = next(
        (
            item
            for item in catalog["operators"]
            if item["operator_id"] == frozen.get("operator_id")
            and item["version_id"] == frozen.get("version_id")
        ),
        None,
    )
    if operator is None:
        raise ValueError("Frozen operator is absent from the catalogue")
    implementation = frozen.get("implementation") or {}
    expected = (
        next(
            (
                item
                for item in operator["implementations"]
                if item["implementation_id"] == implementation.get("implementation_id")
            ),
            None,
        )
        if implementation
        else None
    )
    if not (
        frozen.get("operator_digest") == operator["operator_digest"]
        and frozen.get("ownership") == operator["ownership"]
        and frozen.get("trust_level") == operator["trust_level"]
        and frozen.get("lifecycle") == operator["lifecycle"]
        and frozen.get("effects") == operator["effects"]
        and frozen.get("supports_workflow") == operator["supports_workflow"]
        and (not implementation or implementation == expected)
    ):
        raise ValueError("Frozen operator definition differs from the catalogue")
    return operator
