"""Workflow node permit, observation, and read-model lifecycle."""

from platform_core.application.catalog import resolve, snapshot
from platform_core.application.control import digest
from platform_core.application.ports import Record
from platform_core.application.remote_bindings import freeze_remote_binding
from platform_core.application.workflow_plan import fingerprint, plan_invocation
from platform_core.application.workflow_support import WorkflowFlow
from platform_core.domain.control import Identity, RuleViolation, require
from platform_core.domain.workflows import acquire_slot, invocation_id, settle_node


class WorkflowNodeFlow(WorkflowFlow):
    def permit_node(
        self, action_id: str, execution_id: str, node_id: str, request: Record
    ) -> Record:
        require(
            type(request.get("uses_derived", False)) is bool,
            "INVALID_BINDING",
            "Invalid derived input fact",
            422,
        )
        stable = {
            "call_path": request["call_path"],
            "scatter_indices": request["scatter_indices"],
            "inputs": request["inputs"],
            "uses_derived": request.get("uses_derived", False),
        }
        with self.store.transaction() as uow:
            action = uow.actions.get(action_id)
            require(
                action["execution_id"] == execution_id
                and invocation_id(execution_id, request["call_path"], request["scatter_indices"])
                == node_id,
                "INVALID_BINDING",
                "Workflow invocation identity changed",
                422,
            )
            try:
                if action["token_id"] is None:
                    uow.access.authorize_principal(
                        action["principal_id"], action["project_id"], "EDITOR"
                    )
                else:
                    uow.access.authorize(
                        Identity(action["principal_id"], action["token_id"]),
                        action["project_id"],
                        "EDITOR",
                    )
            except RuleViolation:
                return {"decision": "DENY"}
            task = uow.tasks.get(action["project_id"], action["task_id"], lock=True)
            path = "/".join(request["call_path"])
            invocation = action["request"]["compiled"]["invocations"].get(path)
            require(invocation is not None, "INVALID_BINDING", "Unknown workflow call path", 422)
            target = invocation["target"]
            try:
                current_operator = resolve(target, self.operator_catalog, admission=False)
                operator_available = current_operator["lifecycle"] != "REVOKED"
            except RuleViolation:
                operator_available = False
            frozen_snapshot = action["request"].get("_operator_snapshots", {}).get(path)
            if frozen_snapshot is not None:
                current_snapshot = snapshot(target, self.operator_catalog, admission=False)
                operator_available = (
                    operator_available
                    and all(
                        frozen_snapshot.get(key) == current_snapshot.get(key)
                        for key in ("operator_id", "version_id", "operator_digest")
                    )
                    and (
                        (frozen_snapshot.get("implementation") or {}).get("implementation_id")
                        == (current_snapshot.get("implementation") or {}).get("implementation_id")
                    )
                )
            remote_enabled = True
            if target.get("execution_binding", {}).get("kind") == "REMOTE":
                try:
                    frozen = freeze_remote_binding(uow, action["project_id"], target)
                    remote_enabled = (
                        action["request"].get("_remote_bindings", {}).get(path) == frozen
                    )
                except RuleViolation:
                    remote_enabled = False
            release_identity_valid = True
            release_revoked = False
            frozen_release = action["request"].get("_workflow_release")
            if frozen_release is not None:
                try:
                    current_release = uow.authoring.release_by_id(
                        action["project_id"], frozen_release["release_id"], lock=True
                    )
                    release_identity_valid = (
                        current_release["definition_id"] == frozen_release["definition_id"]
                        and current_release["version_label"] == frozen_release["version_label"]
                        and current_release["candidate_id"] == frozen_release["candidate_id"]
                        and current_release["source_digest"] == frozen_release["source_digest"]
                        and current_release["package_ref"] == frozen_release["package_ref"]
                    )
                    release_revoked = current_release["lifecycle"] == "REVOKED"
                except RuleViolation:
                    release_identity_valid = False
            plan = action["request"].get("execution_plan")
            if plan is not None:
                planned = plan_invocation(
                    plan,
                    node_id,
                    request["call_path"],
                    request["scatter_indices"],
                )
                require(
                    planned is not None
                    and planned.get("condition") is not False
                    and planned.get("operator_id") == target.get("operator_id")
                    and planned.get("implementation_digest") == fingerprint(target),
                    "INVALID_BINDING",
                    "Workflow invocation is outside the frozen execution plan",
                    422,
                )
            envelope = uow.workflows.envelope(action_id)
            node = uow.workflows.node(node_id, lock=True)
            if node is None:
                if len(uow.workflows.nodes(action_id)) >= envelope["amount"]:
                    return {
                        "decision": "DENY",
                        "reportable": False,
                        "reason": "NODE_LIMIT",
                    }
                node = {
                    "node_id": node_id,
                    "action_id": action_id,
                    "request": request,
                    "digest": digest(stable),
                    "slot_acquired": False,
                    "executed": False,
                    "stopped": False,
                    "revision": 0,
                    "observation": None,
                }
                uow.workflows.add_node(node)
            else:
                require(
                    node["action_id"] == action_id and node["digest"] == digest(stable),
                    "INVALID_BINDING",
                    "Workflow invocation was replayed with different inputs",
                    422,
                )
            now = uow.now()
            release_denied = not release_identity_valid or (
                release_revoked and not node["slot_acquired"]
            )
            invalid = (
                task.lifecycle != "OPEN"
                or action["cancel_requested"]
                or action["stopped"]
                or task.spec_revision != action["spec_revision"]
                or task.grant_epoch != action["grant_epoch"]
                or not remote_enabled
                or not operator_available
                or release_denied
                or not uow.tasks.permits(
                    task.task_id,
                    action["request"]["target"],
                    action["request"]["resources"],
                    now,
                )
                or not uow.tasks.permits(
                    task.task_id,
                    target,
                    action["request"]["resources"],
                    now,
                    derived=request.get("uses_derived", False),
                )
            )
            if invalid or node["stopped"]:
                if not node["slot_acquired"] and not node["stopped"]:
                    fact = {
                        "state": "REJECTED",
                        "executed": False,
                        "stopped": True,
                        "execution_id": request.get("execution_id"),
                        "attempt_id": request.get("attempt_id"),
                    }
                    uow.workflows.update_node(
                        node_id,
                        stopped=True,
                        revision=1,
                        observation=fact,
                    )
                    node = node | {"stopped": True, "revision": 1, "observation": fact}
                return {
                    "decision": "DENY",
                    "reportable": bool(node["slot_acquired"]),
                    "reason": "RELEASE_REVOKED" if release_denied else "PERMIT_DENIED",
                }
            if task.control == "PAUSED":
                return {"decision": "WAIT", "reportable": False}
            if not node["slot_acquired"]:
                try:
                    budget = acquire_slot(uow.budgets.get(task.task_id))
                except RuleViolation as problem:
                    if problem.code == "BUDGET_BLOCKED":
                        return {"decision": "WAIT"}
                    raise
                uow.budgets.save(budget)
                uow.workflows.update_node(node_id, slot_acquired=True)
            return {
                "decision": "ALLOW",
                "reportable": True,
                "target": target,
                "task_id": task.task_id,
                "spec_revision": task.spec_revision,
            }

    def observe_node(
        self, action_id: str, execution_id: str, node_id: str, observation: Record
    ) -> bool:
        with self.store.transaction() as uow:
            action = uow.actions.get(action_id)
            require(
                action["execution_id"] == execution_id,
                "INVALID_BINDING",
                "Workflow execution identity changed",
                422,
            )
            task = uow.tasks.get(action["project_id"], action["task_id"], lock=True)
            node = uow.workflows.node(node_id, lock=True)
            require(
                node is not None and node["action_id"] == action_id,
                "INVALID_BINDING",
                "Unknown workflow invocation",
                422,
            )
            require(node["slot_acquired"], "INVALID_BINDING", "Node has no execution slot", 422)
            revision = observation["revision"]
            require(
                type(revision) is int and revision >= 1,
                "INVALID_BINDING",
                "Invalid node revision",
            )
            if revision < node["revision"]:
                return False
            fact = {
                "state": observation["state"],
                "executed": observation["executed"],
                "stopped": observation["stopped"],
                "execution_id": observation.get("execution_id"),
                "attempt_id": observation.get("attempt_id"),
            }
            if revision == node["revision"]:
                require(
                    node["observation"] == fact,
                    "INVALID_BINDING",
                    "Conflicting node fact",
                    422,
                )
                return True
            budget = settle_node(
                uow.budgets.get(task.task_id),
                was_executed=node["executed"],
                executed=bool(observation["executed"]),
                was_stopped=node["stopped"],
                stopped=bool(observation["stopped"]),
            )
            uow.budgets.save(budget)
            uow.workflows.update_node(
                node_id,
                executed=bool(observation["executed"]),
                stopped=bool(observation["stopped"]),
                revision=revision,
                observation=fact,
            )
            return True

    def steps(
        self,
        actor: Identity,
        project: str,
        execution_id: str,
        after: str = "",
        limit: int = 101,
    ) -> list[Record]:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            action = uow.actions.by_execution(project, execution_id)
            require(
                uow.workflows.envelope(action["action_id"], required=False) is not None,
                "RESOURCE_NOT_FOUND",
                "Workflow execution not found",
                404,
            )
            values = []
            for node in uow.workflows.nodes(action["action_id"], after, limit):
                request, fact = node["request"], node["observation"] or {}
                values.append(
                    {
                        "node_id": node["node_id"],
                        "execution_id": fact.get("execution_id"),
                        "call_path": request["call_path"],
                        "scatter_indices": request["scatter_indices"],
                        "state": fact.get("state", "QUEUED"),
                        "executed": node["executed"],
                        "attempt_id": fact.get("attempt_id"),
                        "engine_job_id": request.get("engine_job_id"),
                    }
                )
            return values
