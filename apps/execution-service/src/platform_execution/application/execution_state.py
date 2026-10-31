"""Declared execution phase sets shared by orchestration and recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ExecutionStateMachine:
    name: str
    transitions: Mapping[str, frozenset[str]]

    @property
    def phases(self) -> frozenset[str]:
        return frozenset(self.transitions)

    def phase(self, row: dict) -> str:
        phase = row.get("runtime", {}).get("phase")
        if phase not in self.phases:
            raise ValueError(f"Unknown {self.name} phase: {phase!r}")
        return phase

    def validate_transition(self, source: str, target: object) -> None:
        if source not in self.transitions:
            raise ValueError(f"Unknown {self.name} phase: {source!r}")
        if target is None or target == source:
            return
        if target not in self.transitions:
            raise ValueError(f"Unknown {self.name} phase: {target!r}")
        if target not in self.transitions[source]:
            raise ValueError(f"Illegal {self.name} transition: {source} -> {target}")


LOCAL_EXECUTION_STATE = ExecutionStateMachine(
    "execution",
    MappingProxyType(
        {
            "ACCEPTED": frozenset({"PREPARED", "REPORTING", "FINAL"}),
            "PREPARED": frozenset({"CREATE_INTENT", "REMOTE_SUBMITTING", "REPORTING", "FINAL"}),
            "CREATE_INTENT": frozenset({"CREATED", "REPORTING", "FINAL"}),
            "CREATED": frozenset({"START_INTENT", "REPORTING", "FINAL"}),
            "START_INTENT": frozenset({"RUNNING", "STOPPING_UNSTARTED", "REPORTING", "FINAL"}),
            "RUNNING": frozenset({"STOPPING_UNSTARTED", "REPORTING", "PUBLISHING", "FINAL"}),
            "STOPPING_UNSTARTED": frozenset({"RUNNING", "REPORTING", "FINAL"}),
            "REMOTE_SUBMITTING": frozenset({"REMOTE_TRACKING", "REPORTING", "FINAL"}),
            "REMOTE_TRACKING": frozenset({"REPORTING", "PUBLISHING", "FINAL"}),
            "REPORTING": frozenset({"FINAL"}),
            "PUBLISHING": frozenset({"FINAL"}),
            "FINAL": frozenset(),
        }
    ),
)

WORKFLOW_EXECUTION_STATE = ExecutionStateMachine(
    "workflow execution",
    MappingProxyType(
        {
            "ACCEPTED": frozenset({"PREPARED", "FINAL"}),
            "PREPARED": frozenset({"ENGINE_CREATED", "FINAL"}),
            "RESTART_READY": frozenset({"ENGINE_CREATED", "FINAL"}),
            "ENGINE_CREATED": frozenset({"ENGINE_RUNNING", "FINAL"}),
            "ENGINE_RUNNING": frozenset({"RESTART_READY", "PUBLISHING", "FINAL"}),
            "PUBLISHING": frozenset({"FINAL"}),
            "FINAL": frozenset(),
        }
    ),
)
