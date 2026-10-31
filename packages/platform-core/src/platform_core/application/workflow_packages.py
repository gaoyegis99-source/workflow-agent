"""Workflow package admission and durable execution-plan creation."""

from platform_core.application.catalog import snapshot
from platform_core.application.control import digest
from platform_core.application.ports import Record
from platform_core.application.remote_bindings import freeze_remote_binding
from platform_core.application.research_context import freeze_research_context
from platform_core.application.workflow_engine import build_engine_package
from platform_core.application.workflow_plan import build_execution_plan
from platform_core.application.workflow_support import WorkflowFlow
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import Identity, require
from platform_core.domain.workflows import reserve_envelope


def package_view(record):
    request = record["request"]
    compiled = record["compiled"]
    package_ref = None
    if record["status"] == "READY" and compiled:
        package_ref = {
            "package_id": record["package_id"],
            "version_id": "1",
            "manifest_digest": compiled["manifest_digest"],
        }
    return {
        "package_id": record["package_id"],
        "status": record["status"],
        "status_url": (
            f"/api/v1/projects/{record['project_id']}/workflow-packages/{record['package_id']}"
        ),
        "name": request["name"],
        "profile": request.get("profile", "v7-cwl-p4-v1"),
        "package_ref": package_ref,
        "error": record["error"],
        "source": request,
        "compiled": compiled,
    }


class WorkflowPackageAdmissionFlow(WorkflowFlow):
    def register(self, actor: Identity, project: str, key: str, request: Record) -> Record:
        def operation(uow):
            package_id = uow.new_id()
            record = {
                "package_id": package_id,
                "project_id": project,
                "request": request,
                "digest": digest(request),
                "status": "QUEUED",
                "compiled": None,
                "error": None,
                "owner": None,
                "generation": 0,
                "lease_until": None,
            }
            uow.workflows.add_package(record)
            return package_view(record)

        return self.control._write(
            actor, project, "register_workflow_package", key, request, "EDITOR", operation
        )

    def get(self, actor: Identity, project: str, package_id: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            return package_view(uow.workflows.package(project, package_id))

    def list(self, actor: Identity, project: str, after: str = "", limit: int = 51) -> list[Record]:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            return [package_view(row) for row in uow.workflows.packages(project, after, limit)]

    def admit(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        *,
        idempotency_payload: Record | None = None,
        agent_context: Record | None = None,
        operation: str = "submit_workflow",
        authoring_trial: Record | None = None,
        authoring_release: Record | None = None,
    ) -> Record:
        """Admit an already resolved workflow request without external I/O."""

        # Planning and graph generation can expand to 1,000 nodes.  Allocate
        # candidate identities and perform that pure work before taking the
        # task row lock.  The final transaction revalidates every frozen fact;
        # an idempotency winner may safely discard this candidate.
        with self.store.transaction() as preparation:
            if actor.is_service_continuation and agent_context is not None:
                preparation.access.authorize_principal(actor.principal_id, project, "EDITOR")
            else:
                preparation.access.authorize(actor, project, "EDITOR")
            prepared_task = preparation.tasks.get(project, request["task_id"])
            prepared_task.admissible(request["expected_spec_revision"])
            prepared_research = freeze_research_context(
                preparation, project, prepared_task, request.get("research_context")
            )
            prepared_package = preparation.workflows.package(
                project, request["target"]["package_ref"]["package_id"]
            )
            require(
                self._package_matches(prepared_package, request["target"]["package_ref"]),
                "INVALID_BINDING",
                "Workflow package reference is not ready or exact",
                422,
            )
            action_id, command_id = preparation.new_id(), preparation.new_id()
            frame_id = (
                agent_context["tool_request_id"]
                if agent_context is not None
                else preparation.new_id()
            )
            prepared_remote_bindings = {
                path: freeze_remote_binding(preparation, project, invocation["target"])
                for path, invocation in prepared_package["compiled"]["invocations"].items()
                if invocation["target"].get("execution_binding", {}).get("kind") == "REMOTE"
            }
            prepared_operator_snapshots = {
                path: snapshot(invocation["target"], self.operator_catalog)
                for path, invocation in prepared_package["compiled"]["invocations"].items()
            }
        amount = request["max_node_executions"]
        execution_plan = build_execution_plan(
            prepared_package["request"],
            prepared_package["compiled"],
            request["inputs"],
            action_id=action_id,
            max_node_executions=amount,
        )
        engine_package = build_engine_package(
            prepared_package["request"],
            prepared_package["compiled"],
            execution_plan,
            request["inputs"],
        )
        trial_case = None
        if authoring_trial is not None:
            require(
                prepared_package["compiled"].get("invocations"),
                "INVALID_BINDING",
                "The trial candidate has no trusted compute path",
                422,
            )
            trial_case = {
                "profile": "workflow-trial-case-v2",
                "candidate": {
                    "candidate_id": authoring_trial["candidate_id"],
                    "package_ref": request["target"]["package_ref"],
                    "source_digest": authoring_trial["source_digest"],
                    "context_digest": authoring_trial["context_digest"],
                },
                "input_digest": digest(request["inputs"]),
                "expected_execution": "SUCCEEDED",
                "required_steps": [
                    {
                        "node_id": node["node_id"],
                        "call_path": node["call_path"],
                        "scatter_indices": node["scatter_indices"],
                        "operator_id": node["operator_id"],
                    }
                    for node in execution_plan["nodes"]
                    if node.get("condition") is True
                ],
                "planned_steps": [
                    {
                        "node_id": node["node_id"],
                        "call_path": node["call_path"],
                        "scatter_indices": node["scatter_indices"],
                        "operator_id": node["operator_id"],
                        "expected": (
                            "EXECUTED"
                            if node.get("condition") is True
                            else "SKIPPED"
                            if node.get("condition") is False
                            else "DYNAMIC"
                        ),
                    }
                    for node in execution_plan["nodes"]
                ],
                "deferred_templates": [
                    {
                        "template_id": item["template_id"],
                        "call_path": item["call_path"],
                        "index_depth": item["index_depth"],
                        "operator_id": item["operator_id"],
                    }
                    for item in execution_plan.get("deferred_templates", [])
                ],
                "required_outputs": [
                    {"output": name, "type": value}
                    for name, value in sorted(prepared_package["compiled"]["outputs"].items())
                ],
                "assertions": authoring_trial["expectations"],
                "assertion_version": "trusted-output-assertions-v2",
                "execution_plan_digest": execution_plan["plan_digest"],
            }
            trial_case["trial_case_digest"] = canonical_digest(trial_case)

        def commit(uow):
            task = uow.tasks.get(project, request["task_id"], lock=True)
            task.admissible(request["expected_spec_revision"])
            research = freeze_research_context(uow, project, task, request.get("research_context"))
            require(
                research == prepared_research,
                "REVISION_CONFLICT",
                "Research policy changed while preparing workflow execution",
            )
            package = uow.workflows.package(project, request["target"]["package_ref"]["package_id"])
            require(
                self._package_matches(package, request["target"]["package_ref"]),
                "INVALID_BINDING",
                "Workflow package reference is not ready or exact",
                422,
            )
            require(
                package["digest"] == prepared_package["digest"]
                and package["compiled"] == prepared_package["compiled"],
                "VERSION_CONFLICT",
                "Workflow package changed while preparing execution",
                409,
            )
            if authoring_trial is not None:
                candidate = uow.authoring.candidate(
                    project,
                    authoring_trial["definition_id"],
                    authoring_trial["candidate_id"],
                )
                require(
                    candidate["package_id"] == package["package_id"]
                    and candidate["source_digest"] == authoring_trial["source_digest"]
                    and candidate["context_digest"] == authoring_trial["context_digest"]
                    and candidate["context"] == authoring_trial["context"],
                    "REVISION_CONFLICT",
                    "Workflow candidate changed while preparing the trial",
                    409,
                )
            if authoring_release is not None:
                release = uow.authoring.release(
                    project,
                    authoring_release["definition_id"],
                    authoring_release["version_label"],
                    lock=True,
                )
                require(
                    release["release_id"] == authoring_release["release_id"]
                    and release["candidate_id"] == authoring_release["candidate_id"]
                    and release["revision"] == authoring_release["revision"]
                    and release["source_digest"] == authoring_release["source_digest"]
                    and release["package_ref"] == authoring_release["package_ref"]
                    and release["context_digest"] == authoring_release["context_digest"],
                    "REVISION_CONFLICT",
                    "Workflow release identity changed while preparing execution",
                    409,
                )
                require(
                    release["lifecycle"] == "ACTIVE"
                    and release["lifecycle_revision"] == authoring_release["lifecycle_revision"],
                    "PRECONDITION_UNRESOLVED",
                    "Workflow release is not active",
                    409,
                )
            targets = [item["target"] for item in package["compiled"]["invocations"].values()]
            operator_snapshots = {
                path: snapshot(invocation["target"], self.operator_catalog)
                for path, invocation in package["compiled"]["invocations"].items()
            }
            require(
                operator_snapshots == prepared_operator_snapshots,
                "VERSION_CONFLICT",
                "Operator catalogue changed while preparing workflow execution",
                409,
            )
            remote_bindings = {
                path: freeze_remote_binding(uow, project, invocation["target"])
                for path, invocation in package["compiled"]["invocations"].items()
                if invocation["target"].get("execution_binding", {}).get("kind") == "REMOTE"
            }
            require(
                remote_bindings == prepared_remote_bindings,
                "VERSION_CONFLICT",
                "Remote service binding changed while preparing execution",
                409,
            )
            now = uow.now()
            require(
                uow.tasks.permits(task.task_id, request["target"], request["resources"], now)
                and all(
                    uow.tasks.permits(task.task_id, target, request["resources"], now)
                    for target in targets
                ),
                "SCOPE_DENIED",
                "Workflow package, leaf operators or inputs are outside the current grant",
                403,
            )
            budget = reserve_envelope(uow.budgets.get(task.task_id), amount)
            command = {
                "command_id": command_id,
                "project_id": project,
                "task_id": task.task_id,
                "spec_revision": task.spec_revision,
                "status": "RECORDED",
                "action_id": action_id,
                "execution_id": None,
                "error_code": None,
                "error_message": None,
            }
            uow.actions.add_command(command)
            uow.actions.add(
                {
                    "action_id": action_id,
                    "command_id": command_id,
                    "task_id": task.task_id,
                    "project_id": project,
                    "spec_revision": task.spec_revision,
                    "grant_epoch": task.grant_epoch,
                    "principal_id": actor.principal_id,
                    "token_id": actor.token_id,
                    "frame_id": frame_id,
                    "decision_source": "AGENT" if agent_context is not None else "CLIENT",
                    "request": request
                    | {
                        "project_id": project,
                        "kind": "WORKFLOW",
                        "compiled": package["compiled"],
                        "execution_plan": execution_plan,
                        "engine_package": engine_package,
                        "leaf_targets": targets,
                        "_operator_catalog_digest": self.operator_catalog["catalog_digest"],
                        "_operator_snapshots": operator_snapshots,
                        **({"_research_policy": research} if research is not None else {}),
                        **(
                            {"_workflow_release": authoring_release}
                            if authoring_release is not None
                            else {}
                        ),
                        **({"_remote_bindings": remote_bindings} if remote_bindings else {}),
                        **(
                            {"client_request": idempotency_payload}
                            if idempotency_payload is not None
                            else {}
                        ),
                    },
                    "send_started": False,
                    "state": "RECORDED",
                    "external_revision": 0,
                    "execution_id": None,
                    "stopped": False,
                    "cancel_requested": False,
                    "failure_count": 0,
                    "last_external_observation": None,
                }
            )
            uow.budgets.save(budget)
            uow.workflows.add_envelope(action_id, amount)
            uow.delivery.enqueue(
                uow.new_id(),
                task.task_id,
                "ACTION",
                {"action_id": action_id, "project_id": project},
            )
            if authoring_trial is None:
                return command
            now = uow.now()
            uow.authoring.add_trial(
                {
                    "trial_id": authoring_trial["trial_id"],
                    "project_id": project,
                    "definition_id": authoring_trial["definition_id"],
                    "candidate_id": authoring_trial["candidate_id"],
                    "task_id": task.task_id,
                    "action_id": action_id,
                    "inputs": request["inputs"],
                    "inputs_digest": canonical_digest(request["inputs"]),
                    "trial_case": trial_case,
                    "trial_case_digest": trial_case["trial_case_digest"],
                    "context": authoring_trial["context"],
                    "context_digest": authoring_trial["context_digest"],
                    "case_verdict": "PENDING",
                    "case_result": None,
                    "case_result_digest": None,
                    "error_category": None,
                    "attempt": 0,
                    "next_retry_at": now,
                    "owner": None,
                    "generation": 0,
                    "lease_until": None,
                    "created_by": actor.principal_id,
                    "created_at": now,
                    "finished_at": None,
                }
            )
            return {
                "trial_id": authoring_trial["trial_id"],
                "trial_case_digest": trial_case["trial_case_digest"],
                "command": command,
            }

        return self.control._write(
            actor,
            project,
            operation,
            key,
            idempotency_payload or request,
            "EDITOR",
            commit,
        )
