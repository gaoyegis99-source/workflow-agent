"""Authorized Control query operations."""

from __future__ import annotations

from dataclasses import asdict

from platform_core.application.control_contracts import digest
from platform_core.application.control_views import grant_view, project_view, task_view
from platform_core.application.ports import Record, UnitOfWork
from platform_core.domain.control import Identity, Task, require, valid_identifier


class ControlQueries:
    def _task_view(self, uow: UnitOfWork, task: Task) -> Record:
        # A deployment without the P6.1 profile cannot contain supported Agent
        # tasks.  Supplying DIRECT here also keeps historical pre-0011 upgrade
        # fixtures readable by the current application code.
        mode = None if self.agent_runtime_profile is not None else "DIRECT"
        return task_view(uow, task, mode)

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

    def grants(
        self, actor: Identity, project: str, after: str = "", limit: int = 51
    ) -> list[Record]:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            return [grant_view(row) for row in uow.tasks.grants(project, after, limit)]

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
