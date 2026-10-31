"""Classify bounded native-container failure diagnostics.

This module owns the parsing and structural validation of operator-produced
``output/error.json`` files.  The local driver remains responsible for the
container lifecycle; it only asks this module for the durable error and the
allowlisted diagnostic that may be persisted with it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContainerFailure:
    """A lifecycle error plus an optional, validated diagnostic payload."""

    error: str
    details: dict[str, object]


def classify_container_failure(row: dict, root: Path, state: dict) -> ContainerFailure:
    """Return the only diagnostic payload safe to attach to a failed attempt.

    Native implementations may report a small, schema-constrained diagnostic.
    Anything malformed, unexpected, oversized, linked, or accompanied by other
    output files remains a generic container failure.  This deliberately keeps
    arbitrary process output out of the execution ledger.
    """

    error = row["runtime"].get("error") or "CONTAINER_EXIT"
    if state.get("OOMKilled"):
        error = "MEMORY_LIMIT"
    elif state["ExitCode"] == 142:
        error = "TIME_LIMIT"
    elif state["ExitCode"] == 153:
        error = "OUTPUT_LIMIT"

    error_file = root / "output/error.json"
    implementation_id = row["runtime"].get("implementation_profile", {}).get("implementation_id")
    diagnostic = _native_diagnostic(error_file, root, state["ExitCode"], implementation_id)
    if diagnostic is not None:
        return diagnostic

    if error_file.is_file() and not error_file.is_symlink() and error_file.stat().st_size < 4096:
        try:
            if json.loads(error_file.read_text()).get("code") == "INPUT_ERROR":
                error = "INPUT_ERROR"
        except ValueError:
            pass
    return ContainerFailure(error, {})


def _native_diagnostic(
    error_file: Path,
    root: Path,
    exit_code: int,
    implementation_id: object,
) -> ContainerFailure | None:
    if not (
        error_file.is_file() and not error_file.is_symlink() and error_file.stat().st_size < 16384
    ):
        return None
    try:
        diagnostic = json.loads(error_file.read_text())
    except ValueError:
        return None
    if (
        exit_code == 10
        and implementation_id == "table-validate-native-v1"
        and _is_validation_failure(diagnostic, root)
    ):
        return ContainerFailure("VALIDATION_FAILED", {"validation": diagnostic})
    if (
        exit_code == 11
        and implementation_id == "table-cast-native-v1"
        and _is_cast_failure(diagnostic, root)
    ):
        return ContainerFailure("CAST_FAILED", {"cast": diagnostic})
    if (
        exit_code == 12
        and implementation_id == "table-aggregate-native-v1"
        and _is_aggregate_failure(diagnostic, root)
    ):
        return ContainerFailure("AGGREGATION_FAILED", {"aggregation": diagnostic})
    return None


def _only_error_file(root: Path) -> bool:
    return {path.name for path in (root / "output").iterdir() if path.name != "started.marker"} == {
        "error.json"
    }


def _is_validation_failure(diagnostic: object, root: Path) -> bool:
    try:
        failures = diagnostic.get("failures", [])
        return bool(
            set(diagnostic)
            == {
                "code",
                "status",
                "rule_count",
                "failed_rule_count",
                "violation_count",
                "failures",
            }
            and diagnostic["code"] == "VALIDATION_FAILED"
            and diagnostic["status"] == "INVALID"
            and type(diagnostic["rule_count"]) is int
            and 1 <= diagnostic["rule_count"] <= 64
            and type(diagnostic["failed_rule_count"]) is int
            and diagnostic["failed_rule_count"] == len(failures)
            and 1 <= len(failures) <= diagnostic["rule_count"]
            and type(diagnostic["violation_count"]) is int
            and diagnostic["violation_count"]
            == sum(item.get("failed_count", -1) for item in failures)
            and all(
                set(item) == {"rule_id", "kind", "failed_count"}
                and isinstance(item["rule_id"], str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item["rule_id"])
                and item["kind"]
                in {"ROW_COUNT", "TYPE", "NOT_NULL", "UNIQUE", "RANGE", "ALLOWED_VALUES"}
                and type(item["failed_count"]) is int
                and item["failed_count"] > 0
                for item in failures
            )
            and _only_error_file(root)
        )
    except (ValueError, TypeError, AttributeError):
        return False


def _is_cast_failure(diagnostic: object, root: Path) -> bool:
    try:
        failures = diagnostic.get("failures", [])
        return bool(
            set(diagnostic)
            == {
                "code",
                "status",
                "cast_count",
                "failed_cast_count",
                "violation_count",
                "failures",
            }
            and diagnostic["code"] == "CAST_FAILED"
            and diagnostic["status"] == "INVALID"
            and type(diagnostic["cast_count"]) is int
            and 1 <= diagnostic["cast_count"] <= 128
            and type(diagnostic["failed_cast_count"]) is int
            and diagnostic["failed_cast_count"] == len(failures)
            and 1 <= len(failures) <= diagnostic["cast_count"]
            and type(diagnostic["violation_count"]) is int
            and diagnostic["violation_count"]
            == sum(item.get("failure_count", -1) for item in failures)
            and all(
                set(item) == {"field_id", "source_type", "target_type", "failure_count"}
                and isinstance(item["field_id"], str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item["field_id"])
                and item["source_type"] in {"string", "int64", "double", "bool"}
                and item["target_type"] in {"string", "int64", "double", "bool"}
                and item["source_type"] != item["target_type"]
                and type(item["failure_count"]) is int
                and item["failure_count"] > 0
                for item in failures
            )
            and _only_error_file(root)
        )
    except (ValueError, TypeError, AttributeError):
        return False


def _is_aggregate_failure(diagnostic: object, root: Path) -> bool:
    try:
        failures = diagnostic.get("failures", [])
        return bool(
            set(diagnostic)
            == {
                "code",
                "status",
                "failure_count",
                "violation_count",
                "failures",
            }
            and diagnostic["code"] == "AGGREGATION_FAILED"
            and diagnostic["status"] == "INVALID"
            and type(diagnostic["failure_count"]) is int
            and diagnostic["failure_count"] == len(failures)
            and 1 <= len(failures) <= 64
            and type(diagnostic["violation_count"]) is int
            and diagnostic["violation_count"] == sum(item.get("count", -1) for item in failures)
            and all(
                set(item) == {"scope", "measure_id", "field_id", "category", "count"}
                and item["scope"] in {"GROUP_KEY", "MEASURE"}
                and (
                    item["measure_id"] is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item["measure_id"])
                )
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", item["field_id"])
                and item["category"] in {"NULL_GROUP_KEY", "NONFINITE_VALUE", "NUMERIC_OVERFLOW"}
                and type(item["count"]) is int
                and item["count"] > 0
                for item in failures
            )
            and _only_error_file(root)
        )
    except (ValueError, TypeError, AttributeError):
        return False


__all__ = ("ContainerFailure", "classify_container_failure")
