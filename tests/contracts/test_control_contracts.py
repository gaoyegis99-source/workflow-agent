from datetime import UTC, datetime, timedelta

import pytest
from platform_contracts.control import BudgetLimits, ControlRequest, GrantCreate, TaskCreate
from platform_contracts.schema import CONTRACTS
from pydantic import ValidationError


@pytest.mark.parametrize("value", [True, "1", -1, 1_000_001, 1.2])
def test_execution_limit_rejects_coercions_and_out_of_range(value):
    with pytest.raises(ValidationError):
        BudgetLimits(execution_count=value, max_in_flight=1)


@pytest.mark.parametrize("value", [0, False, "1", 1001])
def test_in_flight_limit_is_a_positive_bounded_integer(value):
    with pytest.raises(ValidationError):
        BudgetLimits(execution_count=1, max_in_flight=value)


def test_control_version_and_task_state_cannot_be_forged():
    with pytest.raises(ValidationError):
        ControlRequest(expected_control_version=True)
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(
            {
                "execution_mode": "DIRECT",
                "spec": {"goal": "Test"},
                "budget": {"execution_count": 1, "max_in_flight": 1},
                "lifecycle": "SUCCEEDED",
            }
        )


def test_grants_require_exact_targets_and_timezone():
    target = {
        "kind": "OPERATOR",
        "operator_id": "test",
        "version_id": "1",
        "execution_binding": {"kind": "LOCAL", "implementation_id": "fixture"},
    }
    payload = {
        "task_id": "task",
        "targets": [target],
        "resources": [],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    grant = GrantCreate.model_validate(payload)
    assert grant.model_dump()["expires_at"].endswith("+00:00")
    for change in [
        {"targets": []},
        {"expires_at": "2026-10-12T12:00:00"},
        {"resources": ["*"]},
        {"project_wide": True},
    ]:
        with pytest.raises(ValidationError):
            GrantCreate.model_validate(payload | change)


def test_public_schema_export_contains_implemented_control_contracts():
    assert {
        "TaskCreate",
        "TaskView",
        "GrantCreate",
        "ControlRequest",
        "BudgetView",
    } <= CONTRACTS.keys()
    assert "ActionRequest" not in CONTRACTS  # Internal test adapter only; no public execution DTO.
