"""Project Agent-linked business state back into deterministic wake events."""

from __future__ import annotations

import hashlib

from platform_core.application.agent_waiting import dependency_fact, wake_run
from platform_core.application.ports import Store
from platform_core.domain.agent_runtime import canonical_digest


class AgentActionReconciler:
    """Observe Platform-owned business identities without polling external services.

    Execution and checker workers remain responsible for external reconciliation.
    This projector only notices their committed Platform facts and wakes the
    owning Agent when those facts change.
    """

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _payload(uow, link):
        if link["operation"] in {"SUBMIT_OPERATOR", "SUBMIT_WORKFLOW"}:
            action = uow.actions.get(link["business_id"])
            command = uow.actions.command(action["project_id"], action["command_id"])
            return {
                "kind": "ACTION_STATUS",
                "action_id": action["action_id"],
                "command_id": action["command_id"],
                "command_status": command["status"],
                "execution_id": command["execution_id"],
                "action_state": action["state"],
                "executed": bool(
                    action["last_external_observation"]
                    and action["last_external_observation"].get("executed")
                ),
                "stopped": action["stopped"],
                "error_code": command["error_code"],
            }
        if link["operation"] == "REGISTER_DELIVERABLE":
            candidate = uow.science.candidate(
                uow.agents.run_by_id(link["agent_run_id"])["project_id"],
                link["business_id"],
            )
            return {
                "kind": "DELIVERABLE_STATUS",
                "deliverable_id": candidate["deliverable_id"],
                "status": "REGISTERED",
                "digest": candidate["digest"],
            }
        if link["operation"] in {"CREATE_ANALYSIS", "REGISTER_ANALYSIS", "CREATE_CLAIM"}:
            return {
                "kind": "RESEARCH_STATUS",
                "operation": link["operation"],
                "business_id": link["business_id"],
                "status": "RECORDED",
            }
        acceptance = uow.science.check(
            uow.agents.run_by_id(link["agent_run_id"])["project_id"],
            link["business_id"],
        )
        task = uow.tasks.get(acceptance["project_id"], acceptance["task_id"])
        return {
            "kind": "ACCEPTANCE_STATUS",
            "acceptance_id": acceptance["acceptance_id"],
            "status": acceptance["status"],
            "report_digest": acceptance["report_digest"],
            "task_lifecycle": task.lifecycle,
        }

    def process(self) -> bool:
        with self.store.transaction() as uow:
            runs = [
                value
                for value in uow.agents.runs_with_action_links()
                if value["status"] not in {"STOPPED", "BLOCKED"}
            ]
        changed = False
        for run in runs:
            for link in run["links"]:
                with self.store.transaction() as uow:
                    locked = uow.agents.action_link(link["tool_request_id"], lock=True)
                    current_run = uow.agents.run_by_id(locked["agent_run_id"], lock=True)
                    if current_run["status"] in {"STOPPED", "BLOCKED"}:
                        continue
                    payload = self._payload(uow, locked)
                    task = uow.tasks.get(
                        current_run["project_id"], current_run["task_id"], lock=True
                    )
                    state_digest = canonical_digest(payload)
                    if state_digest == locked["last_observation_digest"]:
                        continue
                    revision = locked["observation_revision"] + 1
                    event_id = (
                        "ae"
                        + hashlib.sha256(
                            f"{locked['tool_request_id']}:{revision}".encode()
                        ).hexdigest()[:62]
                    )
                    event = {
                        "source": "ACTION",
                        "event_id": event_id,
                        "stream_id": locked["tool_request_id"],
                        "source_revision": revision,
                        "kind": payload["kind"],
                        "payload": payload,
                    }
                    if not uow.agents.register_event(
                        event
                        | {
                            "agent_run_id": current_run["agent_run_id"],
                            "event_digest": canonical_digest(event),
                            "observed_at": uow.now(),
                        }
                    ):
                        continue
                    uow.agents.update_watermark(
                        {
                            "agent_run_id": current_run["agent_run_id"],
                            "source": "ACTION",
                            "stream_id": locked["tool_request_id"],
                            "latest_revision": revision,
                            "latest_event_digest": canonical_digest(event),
                            "updated_at": uow.now(),
                        }
                    )
                    uow.agents.update_action_link(
                        locked["tool_request_id"],
                        observation_revision=revision,
                        last_observation_digest=state_digest,
                    )
                    dependencies = uow.agents.wait_dependencies(
                        current_run["agent_run_id"], active_only=True, lock=True
                    )
                    had_dependencies = bool(dependencies)
                    for dependency in dependencies:
                        fact = dependency_fact(uow, current_run, dependency)
                        next_state = (
                            "SATISFIED"
                            if fact["satisfied"]
                            else ("UNKNOWN" if fact["unknown"] else "WAITING")
                        )
                        uow.agents.update_wait_dependency(
                            dependency["dependency_id"],
                            observed_revision=fact["revision"],
                            state=next_state,
                            state_digest=canonical_digest(fact),
                            updated_at=uow.now(),
                        )
                    remaining = uow.agents.wait_dependencies(
                        current_run["agent_run_id"], active_only=True
                    )
                    # Intermediate QUEUED/RUNNING observations remain visible
                    # in the ledger but do not spend another model call.
                    should_wake = current_run["status"] == "WAITING" and (
                        (had_dependencies and not remaining) or not had_dependencies
                    )
                    if should_wake:
                        current_run = current_run | {
                            "task_spec_revision": task.spec_revision,
                            "control_version": task.control_version,
                        }
                        wake_run(uow, current_run, [event_id], "ACTION")
                    elif (
                        current_run["task_spec_revision"] != task.spec_revision
                        or current_run["control_version"] != task.control_version
                    ):
                        uow.agents.update_run(
                            current_run["agent_run_id"],
                            task_spec_revision=task.spec_revision,
                            control_version=task.control_version,
                            updated_at=uow.now(),
                        )
                    changed = True
        return changed
