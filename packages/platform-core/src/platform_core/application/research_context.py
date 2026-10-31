"""Explicit port for Control's frozen Research context lookup."""

from __future__ import annotations

from typing import Protocol

from platform_core.domain.control import require


class ResearchContextPort(Protocol):
    def freeze(self, uow, project: str, task, context: dict | None) -> dict | None: ...


def freeze_research_context(uow, project, task, context):
    """Return immutable Research facts without constructing an application service."""

    if context is None:
        return None
    registration = uow.research.registration(
        project, task.task_id, context["analysis_registration_id"]
    )
    require(
        registration["task_spec_revision"] == task.spec_revision,
        "REVISION_CONFLICT",
        "Analysis registration targets an older task specification",
    )
    require(
        uow.research.successor(registration["revision_id"]) is None,
        "REVISION_CONFLICT",
        "Analysis registration has been superseded",
    )
    decision = uow.research.policy_decision(registration["policy_decision_id"])
    requirement = context.get("policy_requirement")
    if requirement is not None:
        require(
            decision["policy_id"] == requirement and decision["result"] == "APPLICABLE",
            "PRECONDITION_UNRESOLVED",
            "The declared action policy has unresolved missing or conflicting facts",
        )
    return {
        "analysis_registration_id": registration["registration_id"],
        "analysis_revision_id": registration["revision_id"],
        "policy_requirement": requirement,
        "policy_decision_id": decision["decision_id"],
        "policy_id": decision["policy_id"],
        "policy_version": decision["policy_version"],
        "policy_digest": decision["policy_digest"],
        "policy_result": decision["result"],
    }


class StoreResearchContext:
    def freeze(self, uow, project, task, context):
        return freeze_research_context(uow, project, task, context)
