"""Task action admission and grant-fenced command recording."""

from __future__ import annotations

from platform_core.application.ports import Record
from platform_core.domain.control import Identity, require


class ControlAdmission:
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
        research = self.research_context.freeze(uow, project, task, request.get("research_context"))
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
