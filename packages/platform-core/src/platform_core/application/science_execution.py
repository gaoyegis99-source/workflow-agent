"""Execution admission, publication permits, and cancellation for Science."""

from platform_core.application.catalog import resolve, snapshot
from platform_core.application.remote_bindings import freeze_remote_binding
from platform_core.application.research_context import freeze_research_context
from platform_core.application.science_support import ScienceFlow
from platform_core.application.science_validation import (
    aggregate_references_match_fields,
    cast_references_match_fields,
    filter_references_match_fields,
)
from platform_core.domain.control import Identity, RuleViolation, require


class ScienceExecutionFlow(ScienceFlow):
    def submit(self, actor, project, key, request, *, agent_context=None):
        replay = self.control.replay_action(actor, project, key, request)
        if replay is not None:
            return replay
        self.authorize(actor, project, "EDITOR")
        with self.store.transaction() as uow:
            task = uow.tasks.get(project, request["task_id"])
            task.admissible(request["expected_spec_revision"])
            research = freeze_research_context(uow, project, task, request.get("research_context"))
        target = request["target"]
        remote = target.get("execution_binding", {}).get("kind") == "REMOTE"
        remote_binding = None
        if remote:
            with self.store.transaction() as uow:
                remote_binding = freeze_remote_binding(uow, project, target)
        else:
            resolve(target, self.operator_catalog)
        operator_definition = resolve(target, self.operator_catalog)
        input_ports = list(operator_definition["inputs"])
        require(
            len(input_ports) == 1 and set(request.get("inputs", {})) == set(input_ports),
            "INVALID_BINDING",
            "Execution inputs differ from the frozen operator contract",
            422,
        )
        input_name = input_ports[0]
        binding = self.data_call(
            actor, project, "POST", "/resolve", body=request["inputs"][input_name]
        )
        if target.get("operator_id") == "table-filter":
            require(
                filter_references_match_fields(
                    request.get("parameters"), binding.get("table", {}).get("fields")
                ),
                "INVALID_BINDING",
                "Filter predicate differs from the selected table schema",
                422,
            )
        if target.get("operator_id") == "table-cast":
            require(
                cast_references_match_fields(
                    request.get("parameters"), binding.get("table", {}).get("fields")
                ),
                "INVALID_BINDING",
                "Cast request differs from the selected table schema",
                422,
            )
        if target.get("operator_id") == "table-aggregate":
            require(
                aggregate_references_match_fields(
                    request.get("parameters"), binding.get("table", {}).get("fields")
                ),
                "INVALID_BINDING",
                "Aggregate plan differs from the selected table schema",
                422,
            )
        additions = {
            "project_id": project,
            "kind": "OPERATOR",
            "bindings": {input_name: binding},
            "resources": [binding["selection"]["asset"]],
            "_operator_snapshot": snapshot(target, self.operator_catalog),
        }
        if research is not None:
            additions["_research_policy"] = research
        if remote_binding is not None:
            additions["_remote_binding"] = remote_binding
        # Frozen legacy shape remains available to existing 0.3.x workers and checkers.
        if input_name == "observations":
            additions["binding"] = binding
        frozen = request | additions
        return self.control.submit_action(actor, project, key, frozen, agent_context=agent_context)

    def permit(self, action_id, execution_id, attempt_id):
        with self.store.transaction() as uow:
            action = uow.actions.get(action_id)
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
            require(
                action["execution_id"] in (None, execution_id),
                "INVALID_BINDING",
                "Execution identity changed",
            )
            request = action["request"]
            if request.get("kind") in {"CHECK", "WORKFLOW"}:
                # Acceptance owns checker admission through its separately
                # frozen Checker Catalog. Workflow parents are coordination
                # actions; each leaf is checked by permit_node. Neither is a
                # public compute operator target.
                operator_available = True
            else:
                try:
                    current_operator = resolve(
                        request.get("target", {}), self.operator_catalog, admission=False
                    )
                    operator_available = current_operator["lifecycle"] != "REVOKED"
                except RuleViolation:
                    # A pre-P7 frozen action is only recoverable while its exact
                    # compatibility definition remains present.
                    operator_available = False
            binding = request.get("target", {}).get("execution_binding", {})
            remote_enabled = True
            if binding.get("kind") == "REMOTE":
                try:
                    service = uow.service_bindings.get(
                        action["project_id"],
                        binding["service_binding_id"],
                        binding["binding_revision"],
                    )
                    remote_enabled = service["enabled"]
                except RuleViolation:
                    remote_enabled = False
            if (
                task.lifecycle != "OPEN"
                or action["cancel_requested"]
                or action["stopped"]
                or task.spec_revision != action["spec_revision"]
                or task.grant_epoch != action["grant_epoch"]
                or not remote_enabled
                or not operator_available
                or not uow.tasks.permits(
                    task.task_id, request["target"], request["resources"], uow.now()
                )
            ):
                return {"decision": "DENY"}
            if task.control == "PAUSED":
                return {"decision": "WAIT"}
            uow.science.permit(action_id, execution_id, attempt_id)
            return {
                "decision": "ALLOW",
                "spec_revision": task.spec_revision,
                "grant_epoch": task.grant_epoch,
            }

    def execution_view(self, actor, project, execution_id):
        self.authorize(actor, project)
        with self.store.transaction() as uow:
            uow.actions.by_execution(project, execution_id)
        return self.execution.json("GET", f"/internal/projects/{project}/executions/{execution_id}")

    def permit_publication(self, action_id):
        """Recheck mutable lifecycle just before immutable assets become visible."""
        with self.store.transaction() as uow:
            action = uow.actions.get(action_id)
            request = action["request"]
            if request.get("kind") == "CHECK":
                return {"decision": "ALLOW"}
            release_permit = None
            frozen_release = request.get("_workflow_release")
            if frozen_release is not None:
                release = uow.authoring.release_by_id(
                    action["project_id"], frozen_release["release_id"], lock=True
                )
                identity_matches = all(
                    release.get(key) == frozen_release.get(key)
                    for key in (
                        "release_id",
                        "definition_id",
                        "version_label",
                        "candidate_id",
                        "revision",
                        "source_digest",
                        "package_ref",
                        "context_digest",
                    )
                )
                if not identity_matches:
                    return {"decision": "DENY", "reason": "RELEASE_IDENTITY_CHANGED"}
                release_permit = uow.authoring.release_publication_permit(action_id)
                if release_permit is None:
                    decision = "DENY" if release["lifecycle"] == "REVOKED" else "ALLOW"
                    release_permit = {
                        "action_id": action_id,
                        "release_id": release["release_id"],
                        "lifecycle_revision": release["lifecycle_revision"],
                        "decision": decision,
                        "reason": ("RELEASE_REVOKED" if decision == "DENY" else "PERMIT_GRANTED"),
                        "created_at": uow.now(),
                    }
                    uow.authoring.add_release_publication_permit(release_permit)
                if release_permit["decision"] != "ALLOW":
                    return {
                        "decision": "DENY",
                        "reason": release_permit["reason"],
                        "release_lifecycle_revision": release_permit["lifecycle_revision"],
                    }
            targets = (
                list(request.get("leaf_targets", []))
                if request.get("kind") == "WORKFLOW"
                else [request.get("target", {})]
            )
            try:
                available = all(
                    resolve(target, self.operator_catalog, admission=False)["lifecycle"]
                    != "REVOKED"
                    for target in targets
                )
            except RuleViolation:
                available = False
            if not available:
                return (
                    {"decision": "DENY", "reason": "OPERATOR_REVOKED"}
                    if release_permit is not None
                    else {"decision": "DENY"}
                )
            response = {"decision": "ALLOW"}
            if release_permit is not None:
                response |= {
                    "release_lifecycle_revision": release_permit["lifecycle_revision"],
                }
            return response

    def activities(self, actor, project, task_id):
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            uow.tasks.get(project, task_id)
            rows = uow.actions.list(task_id)
            for row in rows:
                request = dict(row["request"])
                request.pop("_remote_binding", None)
                request.pop("_remote_bindings", None)
                request.pop("_operator_snapshot", None)
                row["request"] = request
            return rows

    def cancel(self, actor, project, execution_id, key, request):
        def operation(uow):
            preliminary = uow.actions.by_execution(project, execution_id)
            task = uow.tasks.get(project, preliminary["task_id"], lock=True)
            require(
                task.spec_revision == request["expected_spec_revision"],
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            action = uow.actions.get(preliminary["action_id"])
            if not action["stopped"]:
                uow.actions.update(action["action_id"], cancel_requested=True)
            return uow.actions.command(project, action["command_id"])

        return self.control._write(
            actor,
            project,
            "cancel_execution",
            key,
            request | {"execution_id": execution_id},
            "EDITOR",
            operation,
        )
