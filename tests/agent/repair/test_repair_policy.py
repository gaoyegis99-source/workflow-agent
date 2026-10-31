import pytest
from platform_core.application.action_tools import ActionToolGateway
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.tool_catalog import build_tool_catalog
from platform_core.domain.agent_runtime import DecisionProposal
from platform_core.domain.control import RuleViolation
from platform_infrastructure import database as database_module


class FailingBackend:
    def __init__(self, code):
        self.code = code
        self.calls = 0

    def execute(self, *args):
        self.calls += 1
        status = 503 if self.code == "DEPENDENCY_UNAVAILABLE" else 422
        raise RuleViolation(self.code, "bounded failure", status)


def proposal(frame):
    return DecisionProposal(
        "ACTION_REQUESTS",
        frame.frame_id,
        frame.task_spec_revision,
        {
            "requests": [
                {
                    "call_id": "repair-completeness",
                    "tool_id": "execution.submit_operator",
                    "tool_version": "1.0.0",
                    "purpose": "Exercise one bounded repair branch",
                    "arguments": {"operator_id": "timeseries-completeness"},
                    "expected_observation": "A typed failure",
                }
            ]
        },
    )


def attempt(setup, gateway, owner):
    coordinator = AgentCoordinator(
        setup["database"],
        context_compiler=ContextCompiler(build_tool_catalog("science"), max_context_tokens=20_000),
    )
    claim = coordinator.claim(owner, 30)
    frame = coordinator.prepare_frame(claim)
    accepted = coordinator.accept_proposal(claim, frame, proposal(frame))
    return gateway.execute_proposal(accepted["proposal_id"])


def test_same_deterministic_failure_stops_after_two_attempts(model_agent_setup):
    backend = FailingBackend("MALFORMED_REQUEST")
    gateway = ActionToolGateway(
        model_agent_setup["database"], build_tool_catalog("science"), backend
    )
    attempt(model_agent_setup, gateway, "p67-repair-one")
    attempt(model_agent_setup, gateway, "p67-repair-two")
    with model_agent_setup["database"].transaction() as uow:
        run = uow.agent_runs.run_by_id(model_agent_setup["agent_run_id"])
        attempts = uow.agent_tools.delivery_attempts(run["agent_run_id"])
    assert backend.calls == 2
    assert [value["outcome"] for value in attempts] == ["FAILED", "STOPPED"]
    assert run["status"] == "BLOCKED"
    assert run["blocked_reason"] == "REPEATED_FAILURE_LIMIT"


def test_unknown_external_outcome_cannot_be_rekeyed(model_agent_setup):
    backend = FailingBackend("DEPENDENCY_UNAVAILABLE")
    gateway = ActionToolGateway(
        model_agent_setup["database"], build_tool_catalog("science"), backend
    )
    attempt(model_agent_setup, gateway, "p67-unknown-one")
    with pytest.raises(RuleViolation) as error:
        attempt(model_agent_setup, gateway, "p67-unknown-two")
    assert error.value.code == "PRECONDITION_UNRESOLVED"
    assert backend.calls == 1
    with model_agent_setup["database"].transaction() as uow:
        attempts = uow.agent_tools.delivery_attempts(model_agent_setup["agent_run_id"])
    assert len(attempts) == 1 and attempts[0]["outcome"] == "UNKNOWN"


def test_p67_migration_is_current(database):
    assert database_module.SCHEMA_VERSION == "0026_worker_lane_leases"
    with database.engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0026_worker_lane_leases"
        )
