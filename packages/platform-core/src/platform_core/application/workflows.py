"""Workflow package registration and validation orchestration."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from platform_core.application.catalog import configured_catalog, resolve, snapshot
from platform_core.application.control import ControlService, digest
from platform_core.application.ports import Record, Store
from platform_core.application.remote_bindings import freeze_remote_binding
from platform_core.application.research import freeze_research_context
from platform_core.application.workflow_compiler import compile_package
from platform_core.application.workflow_engine import build_engine_package
from platform_core.application.workflow_plan import (
    build_execution_plan,
    fingerprint,
    plan_invocation,
)
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import Identity, RuleViolation, require
from platform_core.domain.workflows import (
    acquire_slot,
    invocation_id,
    reserve_envelope,
    settle_node,
)


class PackageValidator(Protocol):
    image_id: str

    def validate(self, compiled: Record) -> None: ...


def package_view(record: Record) -> Record:
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


class Workflows:
    def __init__(self, store: Store, data=None, execution=None, *, operator_catalog=None):
        self.store = store
        self.data = data
        self.execution = execution
        self.operator_catalog = operator_catalog or configured_catalog()
        self.control = ControlService(store)

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

    @staticmethod
    def _package_matches(record, ref):
        return (
            record["status"] == "READY"
            and ref["package_id"] == record["package_id"]
            and ref["version_id"] == "1"
            and ref["manifest_digest"] == record["compiled"]["manifest_digest"]
        )

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
            if actor.token_id is None and agent_context is not None:
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

    def _replay(
        self, actor, project, key, request, *, agent_context=None, operation="submit_workflow"
    ):
        self.control.validate_key(key)
        with self.store.transaction() as uow:
            if actor.token_id is None and agent_context is not None:
                uow.access.authorize_principal(actor.principal_id, project, "EDITOR")
            else:
                uow.access.authorize(actor, project, "EDITOR")
            result = uow.delivery.replay(actor, project, operation, key)
            if result is None:
                return None
            action_id = result.get("action_id") or result.get("command", {}).get("action_id")
            require(action_id is not None, "INVALID_BINDING", "Idempotent action is missing", 409)
            action = uow.actions.get(action_id)
            require(
                digest(action["request"].get("client_request")) == digest(request),
                "IDEMPOTENCY_CONFLICT",
                "Request key has different content",
            )
            return result

    def _resolve_inputs(self, actor, project, schemas, values):
        require(
            isinstance(values, dict) and set(values) <= set(schemas),
            "INVALID_BINDING",
            "Workflow inputs do not match the frozen package",
            422,
        )
        resources = []

        def resolve(schema, value, path):
            compound = (
                isinstance(schema, dict)
                and isinstance(schema.get("type"), str)
                and schema.get("type") in {"array", "record"}
            )
            wrapper = schema if isinstance(schema, dict) and not compound else {}
            if value is None and "default" in wrapper:
                value = wrapper["default"]
            schema = schema if compound else wrapper.get("type", schema)
            if isinstance(schema, list):
                if value is None and "null" in schema:
                    return None
                choices = [item for item in schema if item != "null"]
                require(len(choices) == 1, "INVALID_BINDING", "Invalid nullable input", 422)
                return resolve(choices[0], value, path)
            if schema == "File":
                require(
                    isinstance(value, dict) and "asset" in value,
                    "INVALID_BINDING",
                    "Workflow File inputs require an exact asset selection",
                    422,
                )
                require(
                    self.data is not None, "DEPENDENCY_UNAVAILABLE", "Data service unavailable", 503
                )
                binding = self.data.json(
                    "POST",
                    f"/internal/projects/{project}/resolve",
                    headers={"X-Actor-ID": actor.principal_id},
                    json=value,
                )
                resources.append(binding["selection"]["asset"])
                return {"class": "File", "binding": binding}
            if isinstance(schema, dict) and schema.get("type") == "array":
                require(isinstance(value, list), "INVALID_BINDING", "Array input required", 422)
                return [
                    resolve(schema["items"], item, path + [index])
                    for index, item in enumerate(value)
                ]
            if isinstance(schema, dict) and schema.get("type") == "record":
                require(isinstance(value, dict), "INVALID_BINDING", "Record input required", 422)
                fields = {field["name"]: field for field in schema["fields"]}
                require(set(value) <= set(fields), "INVALID_BINDING", "Unknown record field", 422)
                output = {}
                for name, field in fields.items():
                    if (
                        name in value
                        or "default" in field
                        or (isinstance(field["type"], list) and "null" in field["type"])
                    ):
                        output[name] = resolve(field, value.get(name), path + [name])
                    else:
                        require(False, "INVALID_BINDING", "Required record field is missing", 422)
                return output
            valid = {
                "boolean": type(value) is bool,
                "int": type(value) is int,
                "long": type(value) is int,
                "float": type(value) in {int, float},
                "double": type(value) in {int, float},
                "string": isinstance(value, str),
            }
            require(
                valid.get(schema, False), "INVALID_BINDING", "Workflow scalar type mismatch", 422
            )
            return value

        frozen = {}
        for name, schema in schemas.items():
            kind = schema.get("type") if isinstance(schema, dict) else None
            wrapper = (
                schema
                if isinstance(schema, dict)
                and not (isinstance(kind, str) and kind in {"array", "record"})
                else {}
            )
            optional = isinstance(wrapper.get("type", schema), list) and "null" in wrapper.get(
                "type", schema
            )
            if name in values or "default" in wrapper or optional:
                frozen[name] = resolve(schema, values.get(name), [name])
            else:
                require(False, "INVALID_BINDING", "Required workflow input is missing", 422)
        unique = {digest(resource): resource for resource in resources}
        return frozen, list(unique.values())

    def submit(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        *,
        agent_context: Record | None = None,
    ) -> Record:
        replay = self._replay(actor, project, key, request, agent_context=agent_context)
        if replay is not None:
            return replay
        with self.store.transaction() as uow:
            if actor.token_id is None and agent_context is not None:
                uow.access.authorize_principal(actor.principal_id, project, "EDITOR")
            else:
                uow.access.authorize(actor, project, "EDITOR")
            package = uow.workflows.package(project, request["target"]["package_ref"]["package_id"])
            require(
                self._package_matches(package, request["target"]["package_ref"]),
                "INVALID_BINDING",
                "Workflow package reference is not ready or exact",
                422,
            )
            schemas = package["compiled"]["inputs"]
        inputs, resources = self._resolve_inputs(actor, project, schemas, request["inputs"])
        return self.admit(
            actor,
            project,
            key,
            request | {"inputs": inputs, "resources": resources},
            idempotency_payload=request,
            agent_context=agent_context,
        )

    def submit_authoring_trial(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        authoring_trial: Record,
        *,
        client_request: Record | None = None,
    ) -> Record:
        idempotency_payload = client_request or request
        replay = self._replay(
            actor,
            project,
            key,
            idempotency_payload,
            operation="submit_workflow_trial",
        )
        if replay is not None:
            return replay
        with self.store.transaction() as uow:
            self._authorize_actor(uow, actor, project)
            package = uow.workflows.package(project, request["target"]["package_ref"]["package_id"])
            require(
                self._package_matches(package, request["target"]["package_ref"]),
                "INVALID_BINDING",
                "Workflow package reference is not ready or exact",
                422,
            )
            schemas = package["compiled"]["inputs"]
        inputs, resources = self._resolve_inputs(actor, project, schemas, request["inputs"])
        return self.admit(
            actor,
            project,
            key,
            request | {"inputs": inputs, "resources": resources},
            idempotency_payload=idempotency_payload,
            operation="submit_workflow_trial",
            authoring_trial=authoring_trial,
        )

    @staticmethod
    def _authorize_actor(uow, actor, project):
        if actor.token_id is None:
            uow.access.authorize_principal(actor.principal_id, project, "EDITOR")
        else:
            uow.access.authorize(actor, project, "EDITOR")

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


class PackageProcessor:
    """Compile outside the transaction, then publish under the original lease generation."""

    def __init__(
        self,
        store: Store,
        validator: PackageValidator,
        *,
        owner: str,
        lease_seconds: float = 120,
    ):
        self.store = store
        self.validator = validator
        self.owner = owner
        self.lease_seconds = lease_seconds

    @staticmethod
    def freeze(request: Record) -> Record:
        compiled = compile_package(request)
        frozen = json.loads(
            json.dumps(compiled, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )
        manifest = hashlib.sha256(
            json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return frozen | {"manifest_digest": "sha256:" + manifest}

    def process_one(self) -> bool:
        with self.store.transaction() as uow:
            claim = uow.workflows.claim_package(self.owner, self.lease_seconds)
            candidate = uow.authoring.candidate_by_package(claim["package_id"]) if claim else None
        if claim is None:
            return False
        try:
            compiled = self.freeze(claim["request"])
            if candidate is not None:
                current = {
                    "catalog_digest": configured_catalog()["catalog_digest"],
                    "compiler_digest": canonical_digest(
                        {"compiler": "bounded-workflow-compiler", "version": "a5-v1"}
                    ),
                    "validator_image_digest": self.validator.image_id,
                }
                frozen = candidate["context"]
                require(
                    all(frozen.get(key) == value for key, value in current.items()),
                    "VALIDATION_ENVIRONMENT_CHANGED",
                    "Candidate validation environment changed",
                    409,
                )
                require(
                    compiled == candidate["expected_manifest"],
                    "INVALID_BINDING",
                    "Candidate compiled manifest differs from the frozen expectation",
                    409,
                )
            self.validator.validate(compiled)
            status, error = "READY", None
        except RuleViolation as problem:
            compiled = None
            status, error = "REJECTED", {"code": problem.code, "message": problem.message}
        except (OSError, TimeoutError):
            # Infrastructure failure must be retried by a later lease owner, not
            # converted into a permanent rejection of the user's package.
            return True
        with self.store.transaction() as uow:
            uow.workflows.finish_package(
                claim["package_id"],
                self.owner,
                claim["generation"],
                status=status,
                compiled=compiled,
                error=error,
            )
        return True
