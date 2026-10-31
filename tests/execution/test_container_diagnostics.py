from __future__ import annotations

import json

from platform_execution.application.components.container_diagnostics import (
    classify_container_failure,
)


def _row(implementation_id: str) -> dict:
    return {
        "runtime": {
            "implementation_profile": {"implementation_id": implementation_id},
        }
    }


def _write_error(tmp_path, value: dict) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "error.json").write_text(json.dumps(value), encoding="utf-8")


def test_validated_native_diagnostic_is_allowlisted(tmp_path) -> None:
    diagnostic = {
        "code": "VALIDATION_FAILED",
        "status": "INVALID",
        "rule_count": 1,
        "failed_rule_count": 1,
        "violation_count": 3,
        "failures": [{"rule_id": "no-null", "kind": "NOT_NULL", "failed_count": 3}],
    }
    _write_error(tmp_path, diagnostic)

    failure = classify_container_failure(
        _row("table-validate-native-v1"), tmp_path, {"ExitCode": 10}
    )

    assert failure.error == "VALIDATION_FAILED"
    assert failure.details == {"validation": diagnostic}


def test_extra_or_invalid_output_never_enters_the_execution_ledger(tmp_path) -> None:
    _write_error(
        tmp_path,
        {
            "code": "CAST_FAILED",
            "status": "INVALID",
            "cast_count": 1,
            "failed_cast_count": 1,
            "violation_count": 1,
            "failures": [
                {
                    "field_id": "value",
                    "source_type": "string",
                    "target_type": "int64",
                    "failure_count": 1,
                }
            ],
        },
    )
    (tmp_path / "output" / "untrusted.log").write_text("body", encoding="utf-8")

    failure = classify_container_failure(_row("table-cast-native-v1"), tmp_path, {"ExitCode": 11})

    assert failure.error == "CONTAINER_EXIT"
    assert failure.details == {}


def test_runtime_limits_take_precedence_over_native_diagnostics(tmp_path) -> None:
    _write_error(
        tmp_path,
        {
            "code": "VALIDATION_FAILED",
            "status": "INVALID",
            "rule_count": 1,
            "failed_rule_count": 1,
            "violation_count": 1,
            "failures": [{"rule_id": "rule", "kind": "TYPE", "failed_count": 1}],
        },
    )

    failure = classify_container_failure(
        _row("table-validate-native-v1"),
        tmp_path,
        {"ExitCode": 142, "OOMKilled": False},
    )

    assert failure.error == "TIME_LIMIT"
    assert failure.details == {}
