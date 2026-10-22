from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config
from platform_contracts.workflows import WorkflowExecutionCreate, WorkflowPackageCreate
from platform_core.application.catalog import OPERATORS, target
from platform_core.domain.control import Budget, RuleViolation
from platform_core.domain.workflows import (
    acquire_slot,
    invocation_id,
    reserve_envelope,
    settle_node,
)
from pydantic import ValidationError


def test_parent_envelope_does_not_take_single_computation_slot():
    budget = reserve_envelope(Budget("task", 3, 1), 2)
    assert (budget.reserved, budget.in_flight) == (2, 0)
    budget = acquire_slot(budget)
    with pytest.raises(RuleViolation):
        acquire_slot(budget)
    budget = settle_node(budget, was_executed=False, executed=True, was_stopped=False, stopped=True)
    budget = acquire_slot(budget)
    budget = settle_node(budget, was_executed=False, executed=True, was_stopped=False, stopped=True)
    assert (budget.used, budget.reserved, budget.in_flight) == (2, 0, 0)
    assert budget.reserve().reserved == 1  # Independent P3 acceptance still fits.


def test_envelope_checks_remaining_allowance_and_monotonic_settlement():
    with pytest.raises(RuleViolation):
        reserve_envelope(Budget("task", 1, 5), 2)
    with pytest.raises(RuleViolation):
        settle_node(
            Budget("task", 3, 1, used=1),
            was_executed=True,
            executed=False,
            was_stopped=False,
            stopped=True,
        )
    value = Budget("task", 3, 1, used=1)
    assert (
        settle_node(value, was_executed=True, executed=True, was_stopped=True, stopped=True)
        == value
    )
    with pytest.raises(RuleViolation):
        acquire_slot(replace(value, in_flight=1))


def test_identity_distinguishes_duplicate_values_and_nested_calls():
    one = invocation_id("root", ["sub", "step"], [0, 1])
    assert one == invocation_id("root", ["sub", "step"], [0, 1])
    assert one != invocation_id("root", ["sub", "step"], [1, 0])
    assert one != invocation_id("root", ["other", "step"], [0, 1])
    with pytest.raises(RuleViolation):
        invocation_id("root", ["step"], [True])


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "2"])
def test_workflow_limit_contract_rejects_invalid(limit):
    with pytest.raises(ValidationError):
        WorkflowExecutionCreate(
            task_id="task",
            expected_spec_revision=1,
            target={
                "kind": "WORKFLOW",
                "package_ref": {
                    "package_id": "package",
                    "version_id": "1",
                    "manifest_digest": "sha256:" + "a" * 64,
                },
            },
            inputs={},
            max_node_executions=limit,
        )


def test_workflow_package_requires_self_contained_entrypoint():
    with pytest.raises(ValidationError):
        WorkflowPackageCreate(
            name="flow",
            entrypoint="missing",
            documents={"main": {}},
            implementations={
                "tool": {
                    "kind": "OPERATOR",
                    "operator_id": "tool",
                    "version_id": "1",
                    "execution_binding": {"kind": "LOCAL", "implementation_id": "impl"},
                }
            },
        )


def test_additive_migrations_repeat_and_keep_existing_data(database, owner, service_databases):
    from platform_core.application.control import ControlService

    config = Config()
    config.set_main_option(
        "script_location",
        str(
            Path(__file__).parents[2]
            / "packages/platform-infrastructure/src/platform_infrastructure/migrations"
        ),
    )
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        alembic_command.downgrade(config, "0003_p3_delivery")

    app = ControlService(database)
    actor, _ = owner
    project = app.create_project(actor, "p4-upgrade", "Preserved")
    task = app.create_task(
        actor,
        project["project_id"],
        "legacy-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "preserve pending P3 action", "requirements": []},
            "budget": {"execution_count": 3, "max_in_flight": 1},
        },
    )
    leaf = target(OPERATORS[0])
    app.grant(
        actor,
        project["project_id"],
        "legacy-grant",
        {
            "task_id": task["task_id"],
            "targets": [leaf],
            "resources": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    pending = app.submit_action(
        actor,
        project["project_id"],
        "legacy-action",
        {
            "task_id": task["task_id"],
            "expected_spec_revision": 1,
            "target": leaf,
            "resources": [],
            "inputs": {},
            "parameters": {},
        },
    )
    database.migrate()
    database.migrate()
    database.check_schema()
    assert app.project(actor, project["project_id"])["name"] == "Preserved"
    assert app.command(actor, project["project_id"], pending["command_id"])["status"] == "RECORDED"
    assert app.budget(actor, project["project_id"], task["task_id"])["reserved"] == 1
    with database.engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT to_regclass('workflow_envelopes')"))
        assert connection.scalar(sa.text("SELECT to_regclass('workflow_nodes')"))
    for db in service_databases:
        db.migrate()
        db.check_schema()
