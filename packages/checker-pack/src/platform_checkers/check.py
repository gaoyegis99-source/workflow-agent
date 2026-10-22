"""Fixed P3 acceptance profile; tolerances and supported clauses are not caller options."""

import hashlib
import json
import math

from platform_checkers.reference import references

ABSOLUTE_TOLERANCE = 1e-10
RELATIVE_TOLERANCE = 1e-8
EXPECTED_METHODS = {
    "timeseries-completeness": (
        "Distinct valid calendar periods; duplicate periods count all rows, including nulls."
    ),
    "statistics-linear-trend": (
        "Intercept OLS on elapsed UTC days; year=365.2425 days; descriptive estimate only."
    ),
}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def same(actual, expected):
    if isinstance(expected, float):
        return (
            type(actual) in (float, int)
            and math.isfinite(actual)
            and math.isclose(
                actual, expected, rel_tol=RELATIVE_TOLERANCE, abs_tol=ABSOLUTE_TOLERANCE
            )
        )
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and set(actual) == set(expected)
            and all(same(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(same(a, b) for a, b in zip(actual, expected, strict=True))
        )
    return type(actual) is type(expected) and actual == expected


def check(request, observations, outputs):
    spec = request["acceptance_spec"]
    failures = []
    references_by_operator = references(observations, request)
    required = spec["required_outputs"]
    artifacts = request["artifacts"]
    if len(artifacts) != len(required) or len(outputs) != len(required):
        failures.append("OUTPUT_COVERAGE")
    seen = set()
    for artifact, output in zip(artifacts, outputs, strict=False):
        manifest, ref = artifact["manifest"], artifact["ref"]
        source = manifest["source"]
        operator = source["target"]["operator_id"]
        if (
            operator not in required
            or operator in seen
            or manifest["kind"] != "OPERATOR_RESULT"
            or ref["version_manifest_digest"]
            != "sha256:" + hashlib.sha256(canonical(manifest)).hexdigest()
            or source["task_ref"]
            != {"task_id": request["task_id"], "spec_revision": request["expected_spec_revision"]}
            or source["input"] != spec["input"]
            or source["parameters"] != spec["parameters"]
            or source["semantics"] != request["binding"]["semantics"]
        ):
            failures.append("PROVENANCE")
        seen.add(operator)
        result, table, method = output["result"], output["table"], output["method"]
        if (
            set(result)
            != {
                "schema",
                "operator_id",
                "version_id",
                "input",
                "parameters",
                "semantics",
                "method",
                "rows",
            }
            or result["schema"] != "p3-result-v1"
            or result["operator_id"] != operator
            or result["version_id"] != "1.0.0"
            or result["input"] != spec["input"]
            or result["parameters"] != spec["parameters"]
            or result["semantics"] != request["binding"]["semantics"]
            or result["method"] != EXPECTED_METHODS.get(operator)
            or method != {key: value for key, value in result.items() if key != "rows"}
            or not same(table, result["rows"])
        ):
            failures.append("SCHEMA_OR_REPRESENTATION")
        if operator not in references_by_operator or not same(
            result["rows"], references_by_operator[operator]
        ):
            failures.append("NUMERICAL_OR_REASON_MISMATCH")
    if seen != set(required):
        failures.append("OUTPUT_COVERAGE")
    if failures:
        status = "FAIL"
    else:
        status = "PASS"
        if "statistics-linear-trend" in required:
            trends = references_by_operator["statistics-linear-trend"]
            evaluable = sum(row.get("status") == "EVALUABLE" for row in trends)
            if evaluable < spec["minimum_evaluable"] or (
                not spec["allow_not_evaluable"] and evaluable != len(trends)
            ):
                status = "NOT_EVALUABLE"
    return {
        "profile": "p3-timeseries-v1",
        "status": status,
        "failures": sorted(set(failures)),
        "scope": "Frozen structured acceptance clauses only; free-text goals are not verified.",
        "absolute_tolerance": ABSOLUTE_TOLERANCE,
        "relative_tolerance": RELATIVE_TOLERANCE,
        "input": spec["input"],
        "parameters": spec["parameters"],
        "artifacts": [artifact["ref"] for artifact in artifacts],
    }
