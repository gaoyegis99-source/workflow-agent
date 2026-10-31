"""Control grant lifecycle commands."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from platform_core.application.control_contracts import compatible_control_request
from platform_core.application.control_views import grant_view
from platform_core.application.ports import Record
from platform_core.domain.control import Identity, require


class ControlGrants:
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
