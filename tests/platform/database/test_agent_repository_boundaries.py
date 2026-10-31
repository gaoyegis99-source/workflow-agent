from platform_infrastructure.database import UnitOfWork
from platform_infrastructure.repositories import (
    AgentBudgetRepository,
    AgentConversationRepository,
    AgentRecoveryRepository,
    AgentRunLeaseRepository,
    AgentToolRepository,
)

RUN_METHODS = {
    "active_lease",
    "add_plan",
    "add_run",
    "claim",
    "lease_owned",
    "plan",
    "plans",
    "release_lease",
    "renew_lease",
    "revoke_lease",
    "run",
    "run_by_id",
    "run_by_task",
    "update_run",
}
CONVERSATION_METHODS = {
    "add_answer",
    "add_context",
    "add_frame",
    "add_outbox",
    "add_proposal",
    "add_question",
    "add_wakeup",
    "events",
    "frame",
    "proposals",
    "questions",
    "register_event",
    "wakeups",
}
TOOL_METHODS = {
    "action_links",
    "add_action_link",
    "add_delivery_attempt",
    "add_tool_observation",
    "add_tool_request",
    "delivery_attempts",
    "tool_observations",
    "tool_requests",
}
BUDGET_METHODS = {
    "add_model_budget",
    "add_model_observation",
    "model_budget",
    "model_call",
    "model_calls",
    "model_usage",
    "reserve_model_call",
    "settle_model_reservation",
}
RECOVERY_METHODS = {
    "add_runtime_recovery",
    "add_wait_dependency",
    "due_reconciliations",
    "runtime_recoveries",
    "schedule_reconciliation",
    "update_watermark",
    "wait_dependencies",
}


def methods(repository):
    return {
        name
        for name, value in repository.__dict__.items()
        if callable(value) and not name.startswith("__")
    }


def test_agent_repository_methods_have_one_domain_owner():
    repositories = {
        AgentRunLeaseRepository: RUN_METHODS,
        AgentConversationRepository: CONVERSATION_METHODS,
        AgentToolRepository: TOOL_METHODS,
        AgentBudgetRepository: BUDGET_METHODS,
        AgentRecoveryRepository: RECOVERY_METHODS,
    }
    for repository, expected in repositories.items():
        assert expected <= methods(repository)
        assert all(
            expected.isdisjoint(methods(other)) for other in repositories if other is not repository
        )


class Transaction:
    def commit(self):
        pass

    def rollback(self):
        pass


class Connection:
    def begin(self):
        return Transaction()

    def close(self):
        pass


class Engine:
    def connect(self):
        return Connection()


def test_unit_of_work_exposes_only_explicit_agent_domains():
    with UnitOfWork(Engine()) as uow:
        assert isinstance(uow.agent_runs, AgentRunLeaseRepository)
        assert isinstance(uow.agent_conversations, AgentConversationRepository)
        assert isinstance(uow.agent_tools, AgentToolRepository)
        assert isinstance(uow.agent_budgets, AgentBudgetRepository)
        assert isinstance(uow.agent_recovery, AgentRecoveryRepository)
        assert not hasattr(uow, "agents")
