"""P2 application commands. A transaction never contains external I/O."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime

from platform_core.application.ports import Record, Store, UnitOfWork
from platform_core.domain.control import Budget, Identity, Task, require, valid_identifier


def digest(value: Record) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def task_view(uow: UnitOfWork, task: Task, execution_mode: str | None = None) -> Record:
    if execution_mode is None:
        from platform_core.application.agent_kernel import task_execution_mode

        execution_mode = task_execution_mode(uow, task.task_id)
    validity = uow.science.validity(task.task_id) if task.lifecycle == "COMPLETED" else None
    completion_validity = (
        validity["validity"] if validity else ("CURRENT" if task.lifecycle == "COMPLETED" else None)
    )
    return asdict(task) | {
        "created_at": task.created_at.isoformat(),
        "execution_mode": execution_mode,
        "spec": uow.tasks.spec(task.task_id, task.spec_revision),
        "completion_validity": completion_validity,
        "completion_validity_revision": (
            validity["revision"] if validity else (1 if completion_validity else None)
        ),
    }


def project_view(value: Record) -> Record:
    return value | {"created_at": value["created_at"].isoformat()}


def grant_view(value: Record) -> Record:
    return value | {
        "expires_at": value["expires_at"].isoformat(),
        "revoked_at": value["revoked_at"].isoformat() if value["revoked_at"] else None,
    }


def compatible_control_request(request: Record) -> Record:
    """New opt-in defaults must not change the digest of a delivered P2 request."""
    request = request.copy()
    if "spec" in request and request["spec"].get("acceptance_spec") is None:
        request["spec"] = {
            key: value for key, value in request["spec"].items() if key != "acceptance_spec"
        }
    if request.get("derived_outputs") is False:
        request.pop("derived_outputs")
    if request.get("acceptance_profiles") == []:
        request.pop("acceptance_profiles")
    if request.get("model_budget") is None:
        request.pop("model_budget", None)
    return request


def require_current_acceptance_contract(spec: Record) -> None:
    acceptance = spec.get("acceptance_spec")
    if not acceptance or acceptance.get("profile") != "open-science-v1":
        return
    if acceptance.get("delivery_class") != "VERIFIED":
        return
    outputs = acceptance.get("required_outputs", [])
    require(
        bool(outputs) and all("role_id" in value and "expected" in value for value in outputs),
        "MALFORMED_REQUEST",
        "New VERIFIED task revisions require exact output role contracts",
        422,
    )


class ControlService:
    def __init__(
        self,
        store: Store,
        *,
        agent_runtime_profile: Record | None = None,
        agent_model_catalog: Record | None = None,
        agent_tool_catalog: Record | None = None,
        operator_catalog: Record | None = None,
    ):
        self.store = store
        self.agent_runtime_profile = agent_runtime_profile
        self.agent_model_catalog = agent_model_catalog
        self.agent_tool_catalog = agent_tool_catalog
        self.operator_catalog = operator_catalog

    def _apply_agent_task_change(self, uow: UnitOfWork, task: Task, kind: str) -> None:
        # P6.1 keeps the production Agent entrypoint disabled.  Besides
        # preserving that boundary, this guard lets historical migration
        # fixtures execute current DIRECT commands before schema 0011 exists.
        if self.agent_runtime_profile is None:
            return
        from platform_core.application.agent_kernel import apply_task_change

        apply_task_change(uow, task, kind)

    def _task_view(self, uow: UnitOfWork, task: Task) -> Record:
        # A deployment without the P6.1 profile cannot contain supported Agent
        # tasks.  Supplying DIRECT here also keeps historical pre-0011 upgrade
        # fixtures readable by the current application code.
        mode = None if self.agent_runtime_profile is not None else "DIRECT"
        return task_view(uow, task, mode)

    @staticmethod
    def _authorize(uow: UnitOfWork, actor: Identity, project: str | None, role: str) -> None:
        if actor.token_id is None:
            require(project is not None, "SCOPE_DENIED", "Agent continuation requires a project")
            uow.access.authorize_principal(actor.principal_id, project, role)
        else:
            uow.access.authorize(actor, project, role)

    @staticmethod
    def validate_key(key: str) -> None:
        require(
            0 < len(key) <= 128 and key.isascii() and all(33 <= ord(c) <= 126 for c in key),
            "MALFORMED_REQUEST",
            "A valid Idempotency-Key is required",
            400,
        )

    def workspace_summary(self, actor: Identity, project: str) -> Record:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "VIEWER")
            role = uow.access.role(actor, project)
            return {
                "project": project_view(uow.access.project(project) | {"role": role}),
                "role": role,
                "as_of": uow.now().isoformat(),
                "authoring": uow.authoring.workspace_counts(project),
                "tasks": uow.tasks.state_counts(project),
                "executions": uow.actions.state_counts(project),
            }

    def execution_summaries(
        self,
        actor: Identity,
        project: str,
        after: str,
        limit: int,
        *,
        task_id: str | None = None,
        state: str | None = None,
    ) -> list[Record]:
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "VIEWER")
            rows = uow.actions.project_page(project, after, limit, task_id=task_id, state=state)
        values = []
        for row in rows:
            target = row.pop("request").get("target", {})
            kind = target.get("kind", "UNKNOWN")
            if kind == "OPERATOR":
                operator_id = target.get("operator_id", "unknown")
                version_id = target.get("version_id", "unknown")
                target_id = f"{operator_id}@{version_id}"
            elif kind == "WORKFLOW":
                ref = target.get("package_ref", {})
                target_id = f"{ref.get('package_id', 'unknown')}@{ref.get('version_id', 'unknown')}"
            elif kind == "CHECK":
                target_id = str(target.get("checker_id", "check"))
            else:
                kind, target_id = "UNKNOWN", "unknown"
            values.append(row | {"target_kind": kind, "target_id": target_id})
        return values

    def replay_action(
        self, actor: Identity, project: str, key: str, request: Record
    ) -> Record | None:
        """Replay a committed submission before contacting Data; retain current access checks."""
        self.validate_key(key)
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, "EDITOR")
            result = uow.delivery.replay(actor, project, "submit_action", key)
            if result is None:
                return None
            action = uow.actions.get(result["action_id"])
            original = {
                name: action["request"][name]
                for name in ("task_id", "expected_spec_revision", "target", "inputs", "parameters")
            }
            if "research_context" in action["request"]:
                original["research_context"] = action["request"]["research_context"]
            require(
                digest(request) == digest(original),
                "IDEMPOTENCY_CONFLICT",
                "Request key has different content",
            )
            return result

    def _write(
        self,
        actor: Identity,
        project: str | None,
        operation: str,
        key: str,
        payload: Record,
        role: str,
        action: Callable[[UnitOfWork], Record],
    ) -> Record:
        self.validate_key(key)
        scope = project or "GLOBAL"
        with self.store.transaction() as uow:
            self._authorize(uow, actor, project, role)
            replay = uow.delivery.idempotency(actor, scope, operation, key, digest(payload))
            if replay is not None:
                if project is None and operation == "create_project":
                    # A GLOBAL create key must not expose a project after its
                    # original creator has lost current membership.
                    self._authorize(uow, actor, replay["project_id"], "VIEWER")
                return replay
            result = action(uow)
            uow.delivery.remember(actor, scope, operation, key, result)
            return result

    @staticmethod
    def _event(uow: UnitOfWork, task: Task, kind: str) -> None:
        uow.delivery.enqueue(
            uow.new_id(),
            task.task_id,
            "TASK_EVENT",
            {
                "kind": kind,
                "project_id": task.project_id,
                "task_id": task.task_id,
                "control_version": task.control_version,
                "spec_revision": task.spec_revision,
                "grant_epoch": task.grant_epoch,
            },
        )

    @staticmethod
    def _command(uow: UnitOfWork, task: Task) -> Record:
        command = dict(
            command_id=uow.new_id(),
            project_id=task.project_id,
            task_id=task.task_id,
            spec_revision=task.spec_revision,
            status="RESOLVED",
            action_id=None,
            execution_id=None,
            error_code=None,
            error_message=None,
        )
        uow.actions.add_command(command)
        return command

    def create_project(self, actor: Identity, key: str, name: str) -> Record:
        return self._write(
            actor,
            None,
            "create_project",
            key,
            {"name": name},
            "VIEWER",
            lambda uow: project_view(uow.access.create_project(uow.new_id(), name, actor)),
        )

    def projects(self, actor: Identity, after: str = "", limit: int = 51) -> list[Record]:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, None)
            return [project_view(row) for row in uow.access.projects(actor, after, limit)]

    def project(self, actor: Identity, project: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            return project_view(
                uow.access.project(project) | {"role": uow.access.role(actor, project)}
            )

    def create_task(self, actor: Identity, project: str, key: str, request: Record) -> Record:
        request = compatible_control_request(request)
        require_current_acceptance_contract(request["spec"])

        def create(uow):
            require(
                request["execution_mode"] in {"DIRECT", "AGENT"},
                "UNSUPPORTED_TARGET",
                "Unknown task execution mode",
                422,
            )
            if request["execution_mode"] == "AGENT":
                require(
                    self.agent_runtime_profile is not None,
                    "UNSUPPORTED_TARGET",
                    "AGENT tasks are not enabled in this deployment",
                    422,
                )
                if self.agent_model_catalog is not None:
                    require(
                        request.get("model_budget") is not None,
                        "MALFORMED_REQUEST",
                        "AGENT tasks require an explicit model budget",
                        422,
                    )
            now = uow.now()
            task = Task(uow.new_id(), project, 1, 1, "OPEN", "ACTIVE", "CLEAR", 1, now)
            uow.tasks.save(task, new=True)
            uow.tasks.add_spec(task.task_id, 1, request["spec"], actor.principal_id)
            limits = request["budget"]
            require(
                limits["execution_count"] >= 0 and limits["max_in_flight"] > 0,
                "MALFORMED_REQUEST",
                "Invalid budget limits",
                400,
            )
            uow.budgets.save(Budget(task.task_id, **limits), new=True)
            if request["execution_mode"] == "AGENT":
                from platform_core.application.agent_kernel import create_agent_run
                from platform_core.application.model_catalog import selected_profile

                model_profile = (
                    selected_profile(self.agent_model_catalog)
                    if self.agent_model_catalog is not None
                    else None
                )
                create_agent_run(
                    uow,
                    task,
                    actor,
                    self.agent_runtime_profile,
                    model_profile=model_profile,
                    model_budget=request.get("model_budget"),
                    tool_catalog=self.agent_tool_catalog,
                    operator_catalog=self.operator_catalog,
                )
            self._event(uow, task, "TASK_CREATED")
            return task_view(uow, task, request["execution_mode"])

        return self._write(actor, project, "create_task", key, request, "EDITOR", create)

    def task(self, actor: Identity, project: str, task_id: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            task = uow.tasks.get(project, task_id, lock=True)
            return self._task_view(uow, task)

    def tasks(
        self, actor: Identity, project: str, after: str = "", limit: int = 51
    ) -> list[Record]:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            return [self._task_view(uow, task) for task in uow.tasks.list(project, after, limit)]

    def budget(self, actor: Identity, project: str, task_id: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            uow.tasks.get(project, task_id, lock=True)
            return asdict(uow.budgets.get(task_id))

    def revise(
        self, actor: Identity, project: str, task_id: str, key: str, request: Record
    ) -> Record:
        request = compatible_control_request(request)
        require_current_acceptance_contract(request["spec"])

        def revise(uow):
            task = uow.tasks.get(project, task_id, lock=True)
            task.version(request["expected_control_version"])
            task.open()
            require(
                task.spec_revision == request["expected_spec_revision"],
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            task = replace(
                task, spec_revision=task.spec_revision + 1, control_version=task.control_version + 1
            )
            uow.tasks.add_spec(task_id, task.spec_revision, request["spec"], actor.principal_id)
            uow.tasks.save(task)
            self._apply_agent_task_change(uow, task, "TASK_REVISED")
            self._event(uow, task, "TASK_REVISED")
            return self._task_view(uow, task)

        return self._write(
            actor, project, "revise_task", key, request | {"task_id": task_id}, "EDITOR", revise
        )

    def control(
        self, actor: Identity, project: str, task_id: str, operation: str, key: str, expected: int
    ) -> Record:
        def apply(uow):
            task = uow.tasks.get(project, task_id, lock=True)
            active = uow.actions.active(task_id)
            task = task.control_change(operation, expected, pending=bool(active))
            uow.tasks.save(task)
            self._apply_agent_task_change(uow, task, "TASK_" + operation.upper())
            if operation == "cancel":
                for action in active:
                    uow.actions.update(action["action_id"], cancel_requested=True)
            self._event(uow, task, "TASK_" + operation.upper())
            return self._command(uow, task)

        return self._write(
            actor,
            project,
            operation,
            key,
            {"task_id": task_id, "expected_control_version": expected},
            "EDITOR",
            apply,
        )

    def grants(
        self, actor: Identity, project: str, after: str = "", limit: int = 51
    ) -> list[Record]:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            return [grant_view(row) for row in uow.tasks.grants(project, after, limit)]

    def grant(self, actor: Identity, project: str, key: str, request: Record) -> Record:
        request = compatible_control_request(request)

        def grant(uow):
            task = uow.tasks.get(project, request["task_id"], lock=True)
            task.open()
            expiry = datetime.fromisoformat(request["expires_at"])
            require(
                expiry > uow.now(), "PRECONDITION_UNRESOLVED", "Grant must expire in the future"
            )
            require(
                all(r["project_id"] == project for r in request["resources"]),
                "SCOPE_DENIED",
                "Cross-project resource grant denied",
                403,
            )
            record = request | {
                "grant_id": uow.new_id(),
                "project_id": project,
                "expires_at": expiry,
                "revoked": False,
                "created_by": actor.principal_id,
                "generation": task.grant_epoch + 1,
                "revoked_at": None,
                "revoked_by": None,
            }
            uow.tasks.add_grant(record)
            task = replace(task, grant_epoch=task.grant_epoch + 1)
            uow.tasks.save(task)
            self._event(uow, task, "GRANT_CREATED")
            return grant_view(record)

        return self._write(actor, project, "create_grant", key, request, "OWNER", grant)

    def revoke_grant(self, actor: Identity, project: str, grant_id: str, key: str) -> Record:
        def revoke(uow):
            grant = uow.tasks.grant(project, grant_id)
            task = uow.tasks.get(project, grant["task_id"], lock=True)
            grant = uow.tasks.grant(project, grant_id)
            if not grant["revoked"]:
                uow.tasks.revoke_grant(grant_id, actor.principal_id)
                task = replace(task, grant_epoch=task.grant_epoch + 1)
                uow.tasks.save(task)
                for action in uow.actions.active(task.task_id):
                    request = action["request"]
                    if not uow.tasks.permits(
                        task.task_id, request["target"], request["resources"], uow.now()
                    ):
                        uow.actions.update(action["action_id"], cancel_requested=True)
                self._event(uow, task, "GRANT_REVOKED")
            return grant_view(uow.tasks.grant(project, grant_id))

        return self._write(
            actor, project, "revoke_grant", key, {"grant_id": grant_id}, "OWNER", revoke
        )

    def command(self, actor: Identity, project: str, command_id: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            result = uow.actions.command(project, command_id)
            require(
                result["execution_id"] is None or valid_identifier(result["execution_id"]),
                "INVALID_BINDING",
                "Stored execution identity violates the public contract",
            )
            return result

    def submit_action(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        *,
        agent_context: Record | None = None,
    ) -> Record:
        """Internal application entry; not exposed as a production P2 HTTP route."""

        return self._write(
            actor,
            project,
            "submit_action",
            key,
            request,
            "EDITOR",
            lambda uow: self.admit(uow, actor, project, request, agent_context=agent_context),
        )

    def admit(self, uow, actor, project, request, *, agent_context=None):
        task = uow.tasks.get(project, request["task_id"], lock=True)
        task.admissible(request["expected_spec_revision"])
        from platform_core.application.research import freeze_research_context

        research = freeze_research_context(uow, project, task, request.get("research_context"))
        if request.get("_research_policy") is not None:
            require(
                request["_research_policy"] == research,
                "REVISION_CONFLICT",
                "Research policy changed while resolving action inputs",
            )
        elif research is not None:
            request = request | {"_research_policy": research}
        require(
            uow.tasks.permits(task.task_id, request["target"], request["resources"], uow.now()),
            "SCOPE_DENIED",
            "Action is outside the current task grant",
            403,
        )
        budget = uow.budgets.get(task.task_id).reserve()
        action_id, command_id = uow.new_id(), uow.new_id()
        command = dict(
            command_id=command_id,
            project_id=project,
            task_id=task.task_id,
            spec_revision=task.spec_revision,
            status="RECORDED",
            action_id=action_id,
            execution_id=None,
            error_code=None,
            error_message=None,
        )
        uow.actions.add_command(command)
        uow.actions.add(
            dict(
                action_id=action_id,
                command_id=command_id,
                task_id=task.task_id,
                project_id=project,
                spec_revision=task.spec_revision,
                grant_epoch=task.grant_epoch,
                principal_id=actor.principal_id,
                token_id=actor.token_id,
                # One Agent frame may admit several independent tools. The
                # historical Action frame key is unique, so the stable
                # ToolRequest identity is the correct per-Action decision key.
                frame_id=(agent_context or {}).get("tool_request_id", uow.new_id()),
                decision_source="AGENT" if agent_context is not None else "CLIENT",
                request=request,
                send_started=False,
                state="RECORDED",
                external_revision=0,
                execution_id=None,
                stopped=False,
                cancel_requested=False,
                failure_count=0,
            )
        )
        uow.budgets.save(budget)
        uow.budgets.reserve(action_id, task.task_id)
        uow.delivery.enqueue(
            uow.new_id(),
            task.task_id,
            "ACTION",
            {"action_id": action_id, "project_id": project},
        )
        return command
