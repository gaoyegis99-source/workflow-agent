"""Read-only Agent ledger projections.

The coordinator owns claims and progression.  These projections deliberately
avoid model context/checkpoints and expose only user-visible ledger facts.
"""

from __future__ import annotations

from platform_core.application.ports import Record, Store
from platform_core.domain.control import Identity


class AgentViews:
    def __init__(self, store: Store) -> None:
        self.store = store

    def view(self, actor: Identity, project_id: str, agent_run_id: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project_id)
            run = uow.agent_runs.run(project_id, agent_run_id)
            return {
                **run,
                "plan": uow.agent_runs.plan(agent_run_id),
                "frames": uow.agent_conversations.frames(agent_run_id),
                "proposals": uow.agent_conversations.proposals(agent_run_id),
                "questions": uow.agent_conversations.questions(agent_run_id),
                "wakeups": uow.agent_conversations.wakeups(agent_run_id),
                "events": uow.agent_conversations.events(agent_run_id),
                "wait_dependencies": uow.agent_recovery.wait_dependencies(agent_run_id),
                "runtime_recoveries": uow.agent_recovery.runtime_recoveries(agent_run_id),
            }

    def task_view(self, actor: Identity, project_id: str, task_id: str) -> Record:
        """Return a user-visible projection without model context or checkpoints."""
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project_id)
            task = uow.tasks.get(project_id, task_id)
            run = uow.agent_runs.run_by_task(task_id, required=True)
            plan = uow.agent_runs.plan(run["agent_run_id"])
            questions = uow.agent_conversations.questions(run["agent_run_id"])
            proposals = uow.agent_conversations.proposals(run["agent_run_id"])[-20:]
            observations = uow.agent_tools.tool_observations(run["agent_run_id"])[-50:]
            action_links = uow.agent_tools.action_links(run["agent_run_id"])[-50:]
            wait_dependencies = uow.agent_recovery.wait_dependencies(run["agent_run_id"])[-64:]
            recoveries = uow.agent_recovery.runtime_recoveries(run["agent_run_id"])[-20:]
            delivery_attempts = uow.agent_tools.delivery_attempts(run["agent_run_id"])[-50:]
            usage = (
                uow.agent_budgets.model_usage(run["agent_run_id"])
                if uow.agent_budgets.model_budget(run["agent_run_id"], required=False)
                else None
            )
            return {
                "agent_run_id": run["agent_run_id"],
                "task_ref": {"task_id": task.task_id, "spec_revision": task.spec_revision},
                "status": run["status"],
                "epoch": run["epoch"],
                "generation": run["generation"],
                "wake_revision": run["wake_revision"],
                "runtime_profile_id": run["runtime_profile"]["profile_id"],
                "runtime_profile_digest": run["runtime_profile_digest"],
                "plan_revision": run["plan_revision"],
                "plan": plan["plan"] if plan else None,
                "blocked_reason": run["blocked_reason"],
                "questions": [
                    {
                        "question_id": value["question_id"],
                        "gap_id": value["gap_id"],
                        "question": value["question"],
                        "status": value["status"],
                        "created_at": value["created_at"].isoformat(),
                    }
                    for value in questions
                ],
                "recent_decisions": [
                    {
                        "proposal_id": value["proposal_id"],
                        "frame_id": value["frame_id"],
                        "kind": value["kind"],
                        "message": value["proposal"].get("body", {}).get("message"),
                        "accepted_at": value["accepted_at"].isoformat(),
                    }
                    for value in proposals
                ],
                "read_observations": [
                    {
                        "observation_id": value["observation_id"],
                        "tool_request_id": value["tool_request_id"],
                        "tool_id": value["tool_id"],
                        "status": value["status"],
                        "result_digest": value["result_digest"],
                        "refs": value["refs"],
                        "error_category": value["error_category"],
                        "observed_at": value["observed_at"].isoformat(),
                    }
                    for value in observations
                ],
                "action_links": [
                    {
                        "link_id": value["link_id"],
                        "tool_request_id": value["tool_request_id"],
                        "operation": value["operation"],
                        "business_id": value["business_id"],
                        "action_id": value["action_id"],
                        "command_id": value["command_id"],
                        "created_at": value["created_at"].isoformat(),
                    }
                    for value in action_links
                ],
                "wait_dependencies": [
                    {
                        "dependency_id": value["dependency_id"],
                        "owner": value["owner"],
                        "object_id": value["object_id"],
                        "minimum_revision": value["minimum_revision"],
                        "observed_revision": value["observed_revision"],
                        "state": value["state"],
                    }
                    for value in wait_dependencies
                ],
                "runtime_recoveries": [
                    {
                        "recovery_id": value["recovery_id"],
                        "from_epoch": value["from_epoch"],
                        "to_epoch": value["to_epoch"],
                        "reason": value["reason"],
                        "ledger_digest": value["ledger_digest"],
                        "created_at": value["created_at"].isoformat(),
                    }
                    for value in recoveries
                ],
                "delivery_attempts": [
                    {
                        "attempt_id": value["attempt_id"],
                        "tool_id": value["tool_id"],
                        "ordinal": value["ordinal"],
                        "outcome": value["outcome"],
                        "failure_category": value["failure_category"],
                        "retry_of_attempt_id": value["retry_of_attempt_id"],
                        "stop_reason": value["stop_reason"],
                    }
                    for value in delivery_attempts
                ],
                "model_usage": usage,
            }


__all__ = ("AgentViews",)
