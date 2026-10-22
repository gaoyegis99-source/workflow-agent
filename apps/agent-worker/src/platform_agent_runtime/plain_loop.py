"""Small framework-free reference runtime used to test adapter semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from threading import RLock

from platform_core.domain.agent_runtime import (
    DecisionFrame,
    DecisionProposal,
    RuntimeInterruptReceipt,
    RuntimeReceipt,
    RuntimeStatus,
    RuntimeTraceRef,
    WakeBatch,
    canonical_digest,
)
from platform_core.domain.control import valid_identifier

from platform_agent_runtime.common import DecisionFunction, proposal_from_record
from platform_agent_runtime.errors import RuntimeConflict, RuntimeNotFound, RuntimeStopped
from platform_agent_runtime.profiles import PLAIN_PROFILE_ID, profile, validate


class PlainLoopRuntime:
    """Reference implementation with no framework-owned business behavior."""

    def __init__(self, decide: DecisionFunction):
        self._decide = decide
        self._states: dict[str, dict] = {}
        self._lock = RLock()
        self._profile = profile(PLAIN_PROFILE_ID)

    @staticmethod
    def _receipt(state: dict) -> RuntimeReceipt:
        return RuntimeReceipt(
            runtime_ref=state["runtime_ref"],
            profile_id=state["profile"]["profile_id"],
            profile_digest=state["profile"]["profile_digest"],
            epoch=state["epoch"],
            checkpoint_revision=state["checkpoint_revision"],
            state=state["state"],
        )

    def start(self, run_spec: Mapping[str, object], initial_frame: DecisionFrame) -> RuntimeReceipt:
        selected = validate(dict(run_spec.get("runtime_profile", {})))
        if selected["profile_id"] != self._profile["profile_id"]:
            raise RuntimeConflict("The run profile does not belong to the plain-loop adapter")
        runtime_ref = run_spec.get("runtime_ref", initial_frame.agent_run_id)
        if not valid_identifier(runtime_ref):
            raise RuntimeConflict("Invalid runtime identity")
        with self._lock:
            existing = self._states.get(runtime_ref)
            if existing is not None:
                if existing["agent_run_id"] != initial_frame.agent_run_id:
                    raise RuntimeConflict("The runtime identity belongs to another AgentRun")
                if existing["profile"] != selected:
                    raise RuntimeConflict(
                        "The runtime identity belongs to another deployment profile"
                    )
                return self._receipt(existing)
            state = {
                "runtime_ref": runtime_ref,
                "agent_run_id": initial_frame.agent_run_id,
                "profile": selected,
                "epoch": 1,
                "checkpoint_revision": 0,
                "state": "WAITING_FRAME",
                "initial_frame_digest": initial_frame.digest,
                "last_frame_id": None,
                "last_frame_digest": None,
                "proposal": None,
                "trace": [
                    {
                        "sequence": 1,
                        "type": "RUNTIME_STARTED",
                        "frame_id": initial_frame.frame_id,
                    }
                ],
            }
            self._states[runtime_ref] = state
            return self._receipt(state)

    def _state(self, runtime_ref: str) -> dict:
        try:
            return self._states[runtime_ref]
        except KeyError as error:
            raise RuntimeNotFound("Unknown runtime identity") from error

    def advance(
        self,
        runtime_ref: str,
        frame: DecisionFrame,
        prior_observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal:
        with self._lock:
            state = self._state(runtime_ref)
            if state["state"] == "STOPPED":
                raise RuntimeStopped("The runtime has been interrupted")
            if frame.agent_run_id != state.get("agent_run_id", runtime_ref):
                raise RuntimeConflict("The frame belongs to another runtime")
            if state["last_frame_id"] == frame.frame_id:
                if state["last_frame_digest"] != frame.digest:
                    raise RuntimeConflict("The same frame identity has different content")
                return proposal_from_record(state["proposal"])
            proposal = self._decide(frame, tuple(prior_observations))
            if proposal.frame_id != frame.frame_id:
                raise RuntimeConflict("The decision refers to another frame")
            state["checkpoint_revision"] += 1
            state["last_frame_id"] = frame.frame_id
            state["last_frame_digest"] = frame.digest
            state["proposal"] = proposal.as_record()
            state["state"] = "WAITING_FRAME"
            state["trace"].append(
                {
                    "sequence": len(state["trace"]) + 1,
                    "type": "PROPOSAL_CREATED",
                    "frame_id": frame.frame_id,
                    "proposal_digest": proposal.digest,
                }
            )
            return proposal

    def resume(
        self,
        runtime_ref: str,
        wake_batch: WakeBatch,
        frame: DecisionFrame,
        prior_observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal:
        observations = tuple(prior_observations) + (
            {
                "kind": "WAKE_BATCH",
                "wake_id": wake_batch.wake_id,
                "event_ids": list(wake_batch.event_ids),
                "reason": wake_batch.reason,
            },
        )
        return self.advance(runtime_ref, frame, observations)

    def interrupt(self, runtime_ref: str, reason: str) -> RuntimeInterruptReceipt:
        with self._lock:
            state = self._state(runtime_ref)
            if state["state"] != "STOPPED":
                state["state"] = "STOPPED"
                state["checkpoint_revision"] += 1
                state["trace"].append(
                    {
                        "sequence": len(state["trace"]) + 1,
                        "type": "RUNTIME_INTERRUPTED",
                        "reason": reason,
                    }
                )
            return RuntimeInterruptReceipt(runtime_ref, state["epoch"], "STOPPED", reason)

    def inspect(self, runtime_ref: str) -> RuntimeStatus:
        with self._lock:
            state = self._state(runtime_ref)
            proposal = state.get("proposal")
            return RuntimeStatus(
                runtime_ref=runtime_ref,
                epoch=state["epoch"],
                checkpoint_revision=state["checkpoint_revision"],
                state=state["state"],
                last_frame_id=state["last_frame_id"],
                last_proposal_digest=(canonical_digest(proposal) if proposal else None),
            )

    def export_visible_trace(self, runtime_ref: str) -> RuntimeTraceRef:
        with self._lock:
            state = self._state(runtime_ref)
            trace = deepcopy(state["trace"])
            return RuntimeTraceRef(
                runtime_ref=runtime_ref,
                epoch=state["epoch"],
                trace_digest=canonical_digest({"events": trace}),
                event_count=len(trace),
            )
