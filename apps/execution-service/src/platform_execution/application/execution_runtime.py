"""Shared durable state, tracing, and permit primitives for execution runners."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Protocol

from platform_contracts.service import ensure

from platform_execution.application.execution_policies import PermitPolicy, TracePolicy
from platform_execution.trace import record


class LeaseLostError(RuntimeError):
    """Raised before a new external effect when this worker lost its lease."""


class EffectGuard(Protocol):
    def assert_active(self) -> None: ...


class _GuardedService:
    """Route every client effect through the per-claim lease fence."""

    def __init__(self, runtime: ExecutionRuntime, name: str, target: object) -> None:
        self._runtime = runtime
        self._name = name
        self._target = target

    def __getattr__(self, attribute: str):
        value = getattr(self._target, attribute)
        if not callable(value) or attribute == "close":
            return value

        def guarded(*args, **kwargs):
            return self._runtime.effect(f"{self._name}.{attribute}", value, *args, **kwargs)

        return guarded


class ExecutionRuntime:
    """One implementation of frozen writes, runtime changes, traces, and permits."""

    def __init__(
        self,
        repository,
        platform,
        *,
        trace_policy: TracePolicy,
        permit_policy: PermitPolicy,
        changed_message: str,
        permit_source: str,
        state_machine=None,
    ):
        self.repository = repository
        self.platform = platform
        self.trace_policy = trace_policy
        self.permit_policy = permit_policy
        self.changed_message = changed_message
        self.permit_source = permit_source
        self.state_machine = state_machine
        self._effect_guard: EffectGuard | None = None

    def bind_lease(self, guard: EffectGuard | None) -> None:
        """Attach the currently held lease for one synchronous worker step."""

        self._effect_guard = guard

    def guard(self, name: str, target: object) -> object:
        return _GuardedService(self, name, target)

    def assert_effect_allowed(self) -> None:
        if self._effect_guard is not None:
            self._effect_guard.assert_active()

    def effect(self, _name: str, operation, *args, **kwargs):
        self.assert_effect_allowed()
        return operation(*args, **kwargs)

    def save(self, path: Path, content: bytes, mode: int = 0o444) -> None:
        self.assert_effect_allowed()
        if path.exists():
            ensure(path.read_bytes() == content, "INVALID_BINDING", self.changed_message)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        descriptor = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def change(self, row: dict, **changes):
        """Apply a fenced, non-lifecycle runtime update.

        Phase/state/executed changes are intentionally rejected here.  Those
        must use ``transition`` so their source phase is checked atomically.
        """

        if {"phase", "state", "executed", "runtime"}.intersection(changes):
            raise ValueError("EXECUTION_TRANSITION_REQUIRED")
        self.assert_effect_allowed()
        runtime = row["runtime"] | changes
        runtime.pop("uncertain", None)
        runtime.pop("failures", None)
        return self.repository.change(row, runtime=runtime)

    def transition(
        self,
        row: dict,
        phase: str,
        *,
        state: str | None = None,
        executed: bool | None = None,
        container_id: str | None = None,
        remove_runtime_keys: tuple[str, ...] = (),
        **runtime_changes,
    ):
        """Atomically fence and persist the only legal lifecycle transition."""

        if self.state_machine is None:
            raise ValueError("EXECUTION_STATE_MACHINE_REQUIRED")
        if "phase" in runtime_changes or "state" in runtime_changes:
            raise ValueError("EXECUTION_TRANSITION_INVALID")
        source = self.state_machine.phase(row)
        self.state_machine.validate_transition(source, phase)
        runtime = row["runtime"] | runtime_changes | {"phase": phase}
        for key in remove_runtime_keys:
            runtime.pop(key, None)
        runtime.pop("uncertain", None)
        runtime.pop("failures", None)
        self.assert_effect_allowed()
        return self.repository.transition(
            row,
            source_phase=source,
            runtime=runtime,
            **({} if state is None else {"state": state}),
            **({} if executed is None else {"executed": executed}),
            **({} if container_id is None else {"container_id": container_id}),
        )

    def trace(
        self,
        row: dict,
        event_type: str,
        discriminator,
        *,
        duration_ms=None,
        attributes=None,
        epoch: int | None = None,
        source: str | None = None,
    ):
        self.assert_effect_allowed()
        context = self.trace_policy.context(row, event_type, epoch, source)
        if context is None:
            return None
        return record(
            self.repository,
            **context,
            event_type=event_type,
            discriminator=discriminator,
            source_monotonic_ns=time.monotonic_ns(),
            duration_ms=duration_ms,
            attributes=attributes,
        )

    def permit(self, row: dict):
        started = time.perf_counter_ns()
        self.trace(
            row,
            "PERMIT_REQUESTED",
            [row["generation"], row["runtime"]["phase"]],
            source=self.permit_source,
            attributes={"phase": row["runtime"]["phase"]},
        )
        path, payload = self.permit_policy.request(row)
        result = self.effect("platform.permit", self.platform.json, "POST", path, json=payload)
        event = {"ALLOW": "PERMIT_ALLOWED", "WAIT": "PERMIT_WAIT"}.get(
            result["decision"], "PERMIT_DENIED"
        )
        self.trace(
            row,
            event,
            [row["generation"], row["runtime"]["phase"], result["decision"]],
            source=self.permit_source,
            duration_ms=(time.perf_counter_ns() - started) / 1_000_000,
            attributes={"decision": result["decision"]},
        )
        return result
