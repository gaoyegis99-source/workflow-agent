"""Agent-run creation and Task-fencing commands.

These commands own the transaction that touches the Agent run, budget,
conversation outbox, and recovery records.  They intentionally do not depend
on ``AgentCoordinator`` so the coordinator remains a progression service.
"""

from __future__ import annotations

from platform_core.application.ports import Record, UnitOfWork
from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import Identity, require, valid_identifier


def create_agent_run(
    uow: UnitOfWork,
    task,
    actor: Identity,
    runtime_profile: Record,
    *,
    model_profile: Record | None = None,
    model_budget: Record | None = None,
    tool_catalog: Record | None = None,
    operator_catalog: Record | None = None,
) -> Record:
    """Create the business ledger entry in the same transaction as the Task."""
    profile_digest = runtime_profile.get("profile_digest")
    require(
        valid_identifier(runtime_profile.get("profile_id"))
        and isinstance(profile_digest, str)
        and len(profile_digest) == 71
        and profile_digest.startswith("sha256:"),
        "INVALID_BINDING",
        "The Agent runtime profile is not frozen",
        422,
    )
    canonical_digest(runtime_profile)
    if tool_catalog is not None or operator_catalog is not None:
        require(
            isinstance(tool_catalog, dict)
            and tool_catalog.get("catalog_id") in {"p7-action-tools-v2", "p7-science-tools-v2"}
            and isinstance(tool_catalog.get("catalog_digest"), str)
            and isinstance(operator_catalog, dict)
            and isinstance(operator_catalog.get("catalog_digest"), str),
            "INVALID_BINDING",
            "New Agent runs require the P7 tool and operator catalogues",
            422,
        )
        unsigned = dict(runtime_profile) | {
            "tool_catalog_id": tool_catalog["catalog_id"],
            "tool_catalog_digest": tool_catalog["catalog_digest"],
            "operator_catalog_digest": operator_catalog["catalog_digest"],
        }
        runtime_profile = unsigned | {"deployment_profile_digest": canonical_digest(unsigned)}
        profile_digest = runtime_profile["deployment_profile_digest"]
    now = uow.now()
    agent_run_id = uow.new_id()
    record = {
        "agent_run_id": agent_run_id,
        "project_id": task.project_id,
        "task_id": task.task_id,
        "status": "READY",
        "epoch": 1,
        "generation": 1,
        "runtime_ref": f"{agent_run_id}-e1",
        "runtime_profile": runtime_profile,
        "runtime_profile_digest": profile_digest,
        "task_spec_revision": task.spec_revision,
        "control_version": task.control_version,
        "plan_revision": 0,
        "frame_sequence": 0,
        "accepted_proposals": 0,
        "wake_revision": 0,
        "blocked_reason": None,
        "created_by": actor.principal_id,
        "created_at": now,
        "updated_at": now,
    }
    uow.agent_runs.add_run(record)
    require(
        (model_profile is None) == (model_budget is None),
        "INVALID_BINDING",
        "Model profile and budget must be configured together",
        422,
    )
    if model_profile is not None:
        from platform_core.domain.model_gateway import ModelBudgetLimits

        limits = ModelBudgetLimits.from_record(model_budget)
        uow.agent_budgets.add_model_budget(
            {
                "agent_run_id": agent_run_id,
                "task_id": task.task_id,
                "profile_id": model_profile["profile_id"],
                "profile_digest": model_profile["profile_digest"],
                **limits.as_record(),
                "used_calls": 0,
                "reserved_calls": 0,
                "used_input_tokens": 0,
                "reserved_input_tokens": 0,
                "used_output_tokens": 0,
                "reserved_output_tokens": 0,
                "used_reasoning_tokens": 0,
                "reserved_reasoning_tokens": 0,
                "used_cost_microunits": 0,
                "reserved_cost_microunits": 0,
                "created_at": now,
            }
        )
    uow.agent_conversations.add_outbox(
        {
            "event_id": uow.new_id(),
            "agent_run_id": agent_run_id,
            "kind": "AGENT_RUN_CREATED",
            "payload": {
                "agent_run_id": agent_run_id,
                "task_id": task.task_id,
                "task_spec_revision": task.spec_revision,
            },
            "done": False,
            "available_at": now,
            "owner": None,
            "lease_until": None,
            "generation": 0,
        }
    )
    return record


def task_execution_mode(uow: UnitOfWork, task_id: str) -> str:
    return "AGENT" if uow.agent_runs.run_by_task(task_id) is not None else "DIRECT"


def apply_task_change(uow: UnitOfWork, task, kind: str) -> None:
    """Fence an in-flight Agent decision when authoritative Task state changes."""
    run = uow.agent_runs.run_by_task(task.task_id, lock=True)
    if run is None:
        return
    now = uow.now()
    uow.agent_conversations.stale_pending_frames(run["agent_run_id"])
    uow.agent_runs.revoke_lease(run["agent_run_id"])
    generation = run["generation"] + 1
    changes = {
        "generation": generation,
        "task_spec_revision": task.spec_revision,
        "control_version": task.control_version,
        "updated_at": now,
        "blocked_reason": None,
    }
    if task.lifecycle == "CANCELLED":
        changes["status"] = "STOPPED"
        changes["blocked_reason"] = "TASK_CANCELLED"
        uow.agent_conversations.cancel_questions(run["agent_run_id"])
        uow.agent_conversations.cancel_wakeups(run["agent_run_id"])
        uow.agent_recovery.cancel_wait_dependencies(run["agent_run_id"], now)
        uow.agent_recovery.clear_reconciliation(run["agent_run_id"])
    elif task.control == "PAUSED":
        changes["status"] = "WAITING"
        changes["blocked_reason"] = "TASK_PAUSED"
    else:
        revision = run["wake_revision"] + 1
        event_id = uow.new_id()
        uow.agent_conversations.add_wakeup(
            {
                "wake_id": uow.new_id(),
                "agent_run_id": run["agent_run_id"],
                "revision": revision,
                "reason": "TASK",
                "event_ids": [event_id],
                "state": "PENDING",
                "created_at": now,
                "consumed_at": None,
            }
        )
        changes.update(status="READY", wake_revision=revision)
        uow.agent_conversations.add_outbox(
            {
                "event_id": event_id,
                "agent_run_id": run["agent_run_id"],
                "kind": kind,
                "payload": {
                    "task_id": task.task_id,
                    "spec_revision": task.spec_revision,
                    "control_version": task.control_version,
                },
                "done": False,
                "available_at": now,
                "owner": None,
                "lease_until": None,
                "generation": 0,
            }
        )
    uow.agent_runs.update_run(run["agent_run_id"], **changes)


__all__ = ("apply_task_change", "create_agent_run", "task_execution_mode")
