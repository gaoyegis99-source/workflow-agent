"""Persistent P6.5 wait dependencies and event-loss reconciliation."""

from __future__ import annotations

from datetime import timedelta

from platform_core.application.ports import Record, Store
from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import require, valid_identifier

_ACTION_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "SEALED", "REJECTED"})
_ACCEPTANCE_TERMINAL = frozenset({"PASS", "FAIL", "ERROR", "INDETERMINATE", "CANCELLED"})
_MODEL_TERMINAL = frozenset({"COMPLETED", "REJECTED", "ERROR", "CANCELLED"})


def dependency_fact(uow, run: Record, dependency: Record) -> Record:
    """Read one dependency from Platform-owned facts without external I/O."""
    owner, object_id = dependency["owner"], dependency["object_id"]
    minimum = dependency.get("minimum_revision")
    if owner == "EXECUTION":
        link = next(
            (
                value
                for value in uow.agents.action_links(run["agent_run_id"])
                if value["operation"] in {"SUBMIT_OPERATOR", "SUBMIT_WORKFLOW"}
                and object_id
                in {
                    value["business_id"],
                    value["action_id"],
                    value["command_id"],
                }
            ),
            None,
        )
        require(link is not None, "INVALID_BINDING", "Execution wait is not Agent-owned", 422)
        action = uow.actions.get(link["action_id"])
        revision = max(1, int(link["observation_revision"]))
        return {
            "owner": owner,
            "object_id": object_id,
            "revision": revision,
            "fact": action["state"],
            "satisfied": action["state"] in _ACTION_TERMINAL
            and (minimum is None or revision >= minimum),
            "unknown": action["state"] == "RECONCILING",
        }
    if owner == "ACCEPTANCE":
        link = next(
            (
                value
                for value in uow.agents.action_links(run["agent_run_id"])
                if value["operation"] == "REQUEST_ACCEPTANCE" and value["business_id"] == object_id
            ),
            None,
        )
        require(link is not None, "INVALID_BINDING", "Acceptance wait is not Agent-owned", 422)
        acceptance = uow.science.check(run["project_id"], object_id)
        revision = max(1, int(link["observation_revision"]))
        return {
            "owner": owner,
            "object_id": object_id,
            "revision": revision,
            "fact": acceptance["status"],
            "satisfied": acceptance["status"] in _ACCEPTANCE_TERMINAL
            and (minimum is None or revision >= minimum),
            "unknown": False,
        }
    if owner == "PLATFORM":
        require(
            object_id == run["task_id"],
            "INVALID_BINDING",
            "Platform wait targets another Task",
            422,
        )
        task = uow.tasks.get(run["project_id"], run["task_id"])
        revision = task.control_version
        changed = task.lifecycle != "OPEN" or task.control != "ACTIVE"
        return {
            "owner": owner,
            "object_id": object_id,
            "revision": revision,
            "fact": f"{task.lifecycle}:{task.control}",
            "satisfied": (minimum is not None and revision >= minimum) or changed,
            "unknown": False,
        }
    if owner == "MODEL":
        call = uow.agents.model_call(object_id, lock=False)
        require(
            call["agent_run_id"] == run["agent_run_id"],
            "INVALID_BINDING",
            "Model wait is not Agent-owned",
            422,
        )
        revision = int(call["attempt"])
        return {
            "owner": owner,
            "object_id": object_id,
            "revision": revision,
            "fact": call["status"],
            "satisfied": call["status"] in _MODEL_TERMINAL
            and (minimum is None or revision >= minimum),
            "unknown": call["status"] in {"UNKNOWN", "RETRYABLE"},
        }
    # DATA and KNOWLEDGE dependencies are satisfied only by a task-linked,
    # monotonically ordered source event. They cannot be invented by a model.
    watermark = next(
        (
            value
            for value in uow.agents.watermarks(run["agent_run_id"])
            if value["source"] == owner and value["stream_id"] == object_id
        ),
        None,
    )
    require(
        watermark is not None,
        "INVALID_BINDING",
        f"{owner} wait has no observed task-linked stream",
        422,
    )
    revision = int(watermark["latest_revision"])
    return {
        "owner": owner,
        "object_id": object_id,
        "revision": revision,
        "fact": "OBSERVED",
        "satisfied": minimum is None or revision >= minimum,
        "unknown": False,
    }


def persist_wait_dependencies(
    uow, run: Record, proposal: Record, dependencies: list[Record]
) -> bool:
    require(1 <= len(dependencies) <= 64, "INVALID_BINDING", "WAIT requires dependencies", 422)
    now = uow.now()
    for index, value in enumerate(dependencies):
        require(
            isinstance(value, dict)
            and value.get("owner")
            in {"PLATFORM", "DATA", "KNOWLEDGE", "EXECUTION", "ACCEPTANCE", "MODEL"}
            and valid_identifier(value.get("object_id")),
            "INVALID_BINDING",
            "Malformed wait dependency",
            422,
        )
        minimum = value.get("minimum_revision")
        require(
            minimum is None or (type(minimum) is int and minimum > 0),
            "INVALID_BINDING",
            "Invalid wait revision",
            422,
        )
        fact = dependency_fact(uow, run, value)
        state = "SATISFIED" if fact["satisfied"] else ("UNKNOWN" if fact["unknown"] else "WAITING")
        uow.agents.add_wait_dependency(
            {
                "dependency_id": uow.new_id(),
                "agent_run_id": run["agent_run_id"],
                "proposal_id": proposal["proposal_id"],
                "dependency_index": index,
                "owner": value["owner"],
                "object_id": value["object_id"],
                "minimum_revision": minimum,
                "observed_revision": fact["revision"],
                "state": state,
                "state_digest": canonical_digest(fact),
                "created_at": now,
                "updated_at": now,
            }
        )
    active = uow.agents.wait_dependencies(run["agent_run_id"], active_only=True)
    if active:
        uow.agents.schedule_reconciliation(run["agent_run_id"], now + timedelta(seconds=5))
        return False
    return True


def wake_run(uow, run: Record, event_ids: list[str], reason: str = "DEPENDENCY") -> None:
    if run["status"] in {"STOPPED", "BLOCKED", "QUESTION"}:
        return
    now = uow.now()
    revision = run["wake_revision"] + 1
    uow.agents.add_wakeup(
        {
            "wake_id": uow.new_id(),
            "agent_run_id": run["agent_run_id"],
            "revision": revision,
            "reason": reason,
            "event_ids": event_ids,
            "state": "PENDING",
            "created_at": now,
            "consumed_at": None,
        }
    )
    uow.agents.stale_pending_frames(run["agent_run_id"])
    uow.agents.revoke_lease(run["agent_run_id"])
    uow.agents.clear_reconciliation(run["agent_run_id"])
    uow.agents.update_run(
        run["agent_run_id"],
        status="READY",
        generation=run["generation"] + 1,
        wake_revision=revision,
        task_spec_revision=run["task_spec_revision"],
        control_version=run["control_version"],
        blocked_reason=None,
        updated_at=now,
    )


class AgentWaitReconciler:
    """Repair lost wakes by reconciling due waits against committed facts."""

    def __init__(self, store: Store):
        self.store = store

    def process(self) -> bool:
        with self.store.transaction() as uow:
            due = uow.agents.due_reconciliations()
        changed = False
        for timer in due:
            with self.store.transaction() as uow:
                run = uow.agents.run_by_id(timer["agent_run_id"], lock=True)
                if run["status"] != "WAITING":
                    uow.agents.clear_reconciliation(run["agent_run_id"])
                    continue
                dependencies = uow.agents.wait_dependencies(
                    run["agent_run_id"], active_only=True, lock=True
                )
                event_ids = []
                for dependency in dependencies:
                    fact = dependency_fact(uow, run, dependency)
                    state = (
                        "SATISFIED"
                        if fact["satisfied"]
                        else ("UNKNOWN" if fact["unknown"] else "WAITING")
                    )
                    digest = canonical_digest(fact)
                    if state != dependency["state"] or digest != dependency["state_digest"]:
                        uow.agents.update_wait_dependency(
                            dependency["dependency_id"],
                            observed_revision=fact["revision"],
                            state=state,
                            state_digest=digest,
                            updated_at=uow.now(),
                        )
                        event_ids.append(dependency["dependency_id"])
                if not uow.agents.wait_dependencies(run["agent_run_id"], active_only=True):
                    wake_run(uow, run, event_ids or [run["agent_run_id"]])
                    changed = True
                else:
                    uow.agents.schedule_reconciliation(
                        run["agent_run_id"], uow.now() + timedelta(seconds=5)
                    )
        return changed
