"""Ports isolating the P6 kernel from agent frameworks and model SDKs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from platform_core.domain.agent_runtime import (
    DecisionFrame,
    DecisionProposal,
    ModelObservation,
    ModelRequest,
    RuntimeInterruptReceipt,
    RuntimeReceipt,
    RuntimeStatus,
    RuntimeTraceRef,
    ToolObservation,
    ToolRequest,
    WakeBatch,
)
from platform_core.domain.embeddings import EmbeddingBatch, EmbeddingRequest


class RuntimePort(Protocol):
    def start(
        self, run_spec: Mapping[str, object], initial_frame: DecisionFrame
    ) -> RuntimeReceipt: ...

    def advance(
        self,
        runtime_ref: str,
        frame: DecisionFrame,
        prior_observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal: ...

    def resume(
        self,
        runtime_ref: str,
        wake_batch: WakeBatch,
        frame: DecisionFrame,
        prior_observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal: ...

    def interrupt(self, runtime_ref: str, reason: str) -> RuntimeInterruptReceipt: ...

    def inspect(self, runtime_ref: str) -> RuntimeStatus: ...

    def export_visible_trace(self, runtime_ref: str) -> RuntimeTraceRef: ...


class ModelPort(Protocol):
    def submit(self, request: ModelRequest) -> ModelObservation: ...

    def query(self, model_call_id: str, provider_ref: str | None) -> ModelObservation: ...

    def cancel(self, model_call_id: str, provider_ref: str | None) -> ModelObservation: ...

    def estimate_usage(self, request: ModelRequest) -> Mapping[str, int]: ...


class EmbeddingPort(Protocol):
    def embed(self, request: EmbeddingRequest) -> EmbeddingBatch: ...


class ToolTransport(Protocol):
    def invoke(self, request: ToolRequest) -> ToolObservation: ...

    def query(self, call_id: str) -> ToolObservation | None: ...
