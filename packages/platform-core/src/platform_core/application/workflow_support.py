"""Typed dependency assembly shared by the split Workflow flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from platform_core.application.control import ControlService
from platform_core.application.ports import ExecutionPort, Identity, Record, Store


class WorkflowDataPort(Protocol):
    """Narrow Data API capability needed while materializing workflow inputs."""

    def json(self, method: str, path: str, **kwargs: object) -> Record: ...


@dataclass(frozen=True)
class WorkflowDependencies:
    store: Store
    data: WorkflowDataPort | None
    execution: ExecutionPort | None
    operator_catalog: Record


class WorkflowFlow:
    """Base port holder for package, submission, and node lifecycle flows."""

    def __init__(self, dependencies: WorkflowDependencies) -> None:
        self.dependencies = dependencies
        self.store = dependencies.store
        self.data = dependencies.data
        self.execution = dependencies.execution
        self.operator_catalog = dependencies.operator_catalog
        self.control = ControlService(self.store)

    @staticmethod
    def _package_matches(record: Record, ref: Record) -> bool:
        return (
            record["status"] == "READY"
            and ref["package_id"] == record["package_id"]
            and ref["version_id"] == "1"
            and ref["manifest_digest"] == record["compiled"]["manifest_digest"]
        )

    @staticmethod
    def _authorize_actor(uow, actor: Identity, project: str) -> None:
        if actor.is_service_continuation:
            uow.access.authorize_principal(actor.principal_id, project, "EDITOR")
        else:
            uow.access.authorize(actor, project, "EDITOR")
