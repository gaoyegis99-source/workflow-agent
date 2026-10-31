"""Read-model projections owned by Control."""

from __future__ import annotations

from dataclasses import asdict

from platform_core.application.ports import Record, UnitOfWork
from platform_core.domain.control import Task


def task_view(uow: UnitOfWork, task: Task, execution_mode: str | None = None) -> Record:
    if execution_mode is None:
        from platform_core.application.agent_runs import task_execution_mode

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
