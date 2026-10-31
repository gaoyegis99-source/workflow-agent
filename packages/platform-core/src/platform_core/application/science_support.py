"""Typed dependencies shared by the split Science application flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from platform_core.application.control import ControlService
from platform_core.application.ports import Identity, Record, Store
from platform_core.domain.control import require


class JsonServicePort(Protocol):
    """The narrow JSON-over-HTTP capability used by Science flows."""

    def json(self, method: str, path: str, **kwargs: object) -> Record: ...


@dataclass(frozen=True)
class ScienceDependencies:
    """Explicit application ports and immutable catalog inputs for Science."""

    store: Store
    data: JsonServicePort | None
    execution: JsonServicePort
    operator_catalog: Record
    checker_catalog: Record | None = None


class ScienceFlow:
    """Base flow with typed ports; concrete flows never depend on ``Science``."""

    def __init__(self, dependencies: ScienceDependencies) -> None:
        self.dependencies = dependencies
        self.store = dependencies.store
        self.data = dependencies.data
        self.execution = dependencies.execution
        self.operator_catalog = dependencies.operator_catalog
        self.checker_catalog = dependencies.checker_catalog
        self.control = ControlService(self.store)

    def authorize(self, actor: Identity, project: str, role: str = "VIEWER") -> None:
        with self.store.transaction() as uow:
            if actor.is_service_continuation:
                uow.access.authorize_principal(actor.principal_id, project, role)
            else:
                uow.access.authorize(actor, project, role)

    def data_call(
        self,
        actor: Identity,
        project: str,
        method: str,
        suffix: str,
        key: str | None = None,
        body: Record | None = None,
        params: Record | None = None,
    ) -> Record:
        self.authorize(actor, project, "VIEWER" if method == "GET" else "EDITOR")
        require(
            self.data is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Data service is not configured",
            503,
        )
        headers = {"X-Actor-ID": actor.principal_id}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.data.json(
            method,
            f"/internal/projects/{project}" + suffix,
            headers=headers,
            **({"json": body} if body is not None else {}),
            **({"params": params} if params is not None else {}),
        )

    def execution_view(self, actor: Identity, project: str, execution_id: str) -> Record:
        self.authorize(actor, project)
        with self.store.transaction() as uow:
            uow.actions.by_execution(project, execution_id)
        return self.execution.json(
            "GET",
            f"/internal/projects/{project}/executions/{execution_id}",
        )
