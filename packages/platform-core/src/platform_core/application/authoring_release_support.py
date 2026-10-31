"""Typed composition and shared projections for Authoring Release flows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from platform_core.application.authoring import environment_stale_reasons
from platform_core.application.control import ControlService
from platform_core.application.ports import Identity, Record, Store
from platform_core.application.workflow_packages import package_view
from platform_core.application.workflow_support import WorkflowDataPort
from platform_core.domain.control import require


class WorkflowSubmissionPort(Protocol):
    """Workflow operations required by Authoring flows, without a facade import."""

    def _replay(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        *,
        agent_context: Record | None = None,
        operation: str = "submit_workflow",
    ) -> Record | None: ...

    def _resolve_inputs(
        self, actor: Identity, project: str, schemas: Record, values: Record
    ) -> tuple[Record, list[Record]]: ...

    def submit_authoring_trial(
        self,
        actor: Identity,
        project: str,
        key: str,
        request: Record,
        authoring_trial: Record,
        *,
        client_request: Record | None = None,
    ) -> Record: ...

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
    ) -> Record: ...


class JsonExecutionPort(Protocol):
    def json(self, method: str, path: str, **kwargs: object) -> Record: ...


@dataclass(frozen=True)
class AuthoringReleaseDependencies:
    store: Store
    data: WorkflowDataPort | None
    execution: JsonExecutionPort | None
    descriptor: Record | None
    environment: Record | None
    operator_catalog: Record | None
    workflows: WorkflowSubmissionPort


class AuthoringReleaseFlow:
    """Base dependency holder; concrete flows receive only explicit ports."""

    def __init__(self, dependencies: AuthoringReleaseDependencies) -> None:
        self.dependencies = dependencies
        self.store = dependencies.store
        self.workflows = dependencies.workflows
        self._data = dependencies.data
        self._execution = dependencies.execution
        self.descriptor = dependencies.descriptor
        self.environment = dependencies.environment
        self.operator_catalog = dependencies.operator_catalog
        self.control = ControlService(self.store)

    @property
    def data(self) -> WorkflowDataPort | None:
        return self._data

    @data.setter
    def data(self, value: WorkflowDataPort | None) -> None:
        """Keep the trial facade and its submission flow on the same live port."""
        self._data = value
        self.workflows.data = value

    @property
    def execution(self) -> JsonExecutionPort | None:
        return self._execution

    @execution.setter
    def execution(self, value: JsonExecutionPort | None) -> None:
        """Keep execution lookups and delegated workflow operations aligned."""
        self._execution = value
        self.workflows.execution = value


_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?$"
)


def authorize(uow, actor: Identity, project: str, role: str = "VIEWER") -> None:
    if actor.is_service_continuation:
        uow.access.authorize_principal(actor.principal_id, project, role)
    else:
        uow.access.authorize(actor, project, role)


def candidate_view(record: Record, package: Record, environment: Record | None) -> Record:
    stale = (
        environment_stale_reasons(record["context"], environment)
        if environment is not None
        else ["VALIDATION_ENVIRONMENT_UNAVAILABLE"]
    )
    projected = package_view(package)
    return {
        "candidate_id": record["candidate_id"],
        "project_id": record["project_id"],
        "definition_id": record["definition_id"],
        "revision": record["revision"],
        "validation_id": record["validation_id"],
        "source_digest": record["source_digest"],
        "context_digest": record["context_digest"],
        "package_id": record["package_id"],
        "package_ref": projected["package_ref"],
        "status": projected["status"],
        "status_url": (
            f"/api/v1/projects/{record['project_id']}/workflow-definitions/"
            f"{record['definition_id']}/candidates/{record['candidate_id']}"
        ),
        "error": projected["error"],
        "created_by": record["created_by"],
        "created_at": record["created_at"].isoformat(),
        "current_valid": projected["status"] == "READY" and not stale,
        "stale_reasons": stale,
    }


def trial_view(record: Record, action: Record, execution_outcome: str) -> Record:
    return {
        "project_id": record["project_id"],
        "definition_id": record["definition_id"],
        "trial_id": record["trial_id"],
        "candidate_id": record["candidate_id"],
        "task_id": record["task_id"],
        "action_id": record["action_id"],
        "execution_id": action.get("execution_id"),
        "execution_outcome": execution_outcome,
        "trial_case_digest": record["trial_case_digest"],
        "case_verdict": record["case_verdict"],
        "case_result": record["case_result"],
        "case_result_digest": record["case_result_digest"],
        "error_category": record["error_category"],
        "generation": record["generation"],
        "created_by": record["created_by"],
        "created_at": record["created_at"].isoformat(),
        "finished_at": record["finished_at"].isoformat() if record["finished_at"] else None,
    }


def semver_sort(label: str) -> list[str]:
    match = _SEMVER.fullmatch(label)
    require(match is not None and len(label) <= 64, "MALFORMED_REQUEST", "Invalid SemVer", 422)
    result = [f"{int(value):020d}" for value in match.group(1, 2, 3)]
    prerelease = match.group(4)
    if prerelease is None:
        return result + ["2:"]
    return [
        *result,
        *[
            ("0:" + f"{int(value):020d}") if value.isdigit() else "1:" + value
            for value in prerelease.split(".")
        ],
    ]
