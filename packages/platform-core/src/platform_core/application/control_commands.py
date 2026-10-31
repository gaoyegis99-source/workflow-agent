"""Control authorization and idempotent task-command operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from platform_core.application.commands import authorize, validate_idempotency_key, write_command
from platform_core.application.control_contracts import (
    compatible_control_request,
    require_current_acceptance_contract,
)
from platform_core.application.control_views import project_view, task_view
from platform_core.application.ports import Record, UnitOfWork
from platform_core.domain.control import Budget, Identity, Task, require


class ControlCommands:
    def _apply_agent_task_change(self, uow: UnitOfWork, task: Task, kind: str) -> None:
        # P6.1 keeps the production Agent entrypoint disabled.  Besides
        # preserving that boundary, this guard lets historical migration
        # fixtures execute current DIRECT commands before schema 0011 exists.
        if self.agent_runtime_profile is None:
            return
        from platform_core.application.agent_runs import apply_task_change

        apply_task_change(uow, task, kind)

    @staticmethod
    def _authorize(uow: UnitOfWork, actor: Identity, project: str | None, role: str) -> None:
        authorize(uow, actor, project, role)

    @staticmethod
    def validate_key(key: str) -> None:
        validate_idempotency_key(key)

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
        def replay_check(uow, replay):
            if project is None and operation == "create_project":
                # A GLOBAL create key must not expose a project after its
                # original creator has lost current membership.
                self._authorize(uow, actor, replay["project_id"], "VIEWER")

        return write_command(
            self.store,
            actor,
            project,
            operation,
            key,
            payload,
            role,
            action,
            replay_check=replay_check,
        )

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
                from platform_core.application.agent_runs import create_agent_run
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
