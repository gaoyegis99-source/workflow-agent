"""Shared adapter helpers; no business service is called from this package."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from platform_core.domain.agent_runtime import DecisionFrame, DecisionProposal


class DecisionFunction(Protocol):
    def __call__(
        self,
        frame: DecisionFrame,
        observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal: ...


def frame_from_record(value: Mapping[str, object]) -> DecisionFrame:
    return DecisionFrame(
        agent_run_id=str(value["agent_run_id"]),
        frame_id=str(value["frame_id"]),
        task_id=str(value["task_id"]),
        task_spec_revision=int(value["task_spec_revision"]),
        control_version=int(value["control_version"]),
        generation=int(value["generation"]),
        context_manifest_digest=str(value["context_manifest_digest"]),
        payload=value.get("payload", {}),  # type: ignore[arg-type]
    )


def proposal_from_record(value: Mapping[str, object]) -> DecisionProposal:
    return DecisionProposal(
        kind=str(value["kind"]),
        frame_id=str(value["frame_id"]),
        expected_task_revision=int(value["expected_task_revision"]),
        body=value.get("body", {}),  # type: ignore[arg-type]
    )


IdFactory = Callable[[], str]
