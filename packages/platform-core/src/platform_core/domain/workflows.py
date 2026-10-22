"""Workflow identity and envelope accounting independent of engines and storage."""

import hashlib
import json
from dataclasses import replace

from platform_core.domain.control import Budget, require


def execution_id(action_id: str) -> str:
    """Stable Execution service identity used to freeze workflow node plans."""
    require(
        isinstance(action_id, str) and action_id,
        "INVALID_BINDING",
        "Invalid action identity",
        422,
    )
    return "e" + hashlib.sha256(action_id.encode()).hexdigest()[:32]


def invocation_id(execution_id: str, call_path: list[str], indices: list[int]) -> str:
    require(
        bool(call_path)
        and all(isinstance(p, str) and p for p in call_path)
        and all(type(i) is int and i >= 0 for i in indices),
        "INVALID_BINDING",
        "Invalid workflow invocation identity",
        422,
    )
    value = json.dumps([execution_id, call_path, indices], separators=(",", ":"))
    return "node_" + hashlib.sha256(value.encode()).hexdigest()


def reserve_envelope(budget: Budget, count: int) -> Budget:
    require(
        type(count) is int and 1 <= count <= 1000,
        "MALFORMED_REQUEST",
        "Workflow node limit must be 1 to 1000",
        400,
    )
    require(
        budget.used + budget.reserved + count <= budget.execution_count,
        "BUDGET_BLOCKED",
        "Workflow exceeds available execution allowance",
    )
    return replace(budget, reserved=budget.reserved + count)


def acquire_slot(budget: Budget) -> Budget:
    require(
        budget.in_flight < budget.max_in_flight, "BUDGET_BLOCKED", "No computation slot available"
    )
    return replace(budget, in_flight=budget.in_flight + 1)


def settle_node(
    budget: Budget, *, was_executed: bool, executed: bool, was_stopped: bool, stopped: bool
) -> Budget:
    require(not was_executed or executed, "INVALID_BINDING", "Execution cannot be retracted")
    require(not was_stopped or stopped, "INVALID_BINDING", "Stopped node cannot restart")
    charged = int(executed and not was_executed)
    released = int(stopped and not was_stopped)
    value = replace(
        budget,
        used=budget.used + charged,
        reserved=budget.reserved - charged,
        in_flight=budget.in_flight - released,
    )
    require(
        min(value.reserved, value.in_flight) >= 0, "INVALID_BINDING", "Invalid workflow settlement"
    )
    return value
