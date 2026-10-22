"""Minimal LangGraph adapter. It yields proposals and never executes business tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
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

from platform_agent_runtime.common import (
    DecisionFunction,
    frame_from_record,
    proposal_from_record,
)
from platform_agent_runtime.errors import (
    RuntimeAdapterError,
    RuntimeConflict,
    RuntimeNotFound,
    RuntimeStopped,
    RuntimeUnavailable,
)
from platform_agent_runtime.profiles import LANGGRAPH_PROFILE_ID, profile, validate


class _State(TypedDict, total=False):
    runtime_ref: str
    agent_run_id: str
    profile: dict[str, Any]
    epoch: int
    checkpoint_revision: int
    state: str
    initial_frame_digest: str
    frame: dict[str, Any]
    frame_digest: str
    observations: list[dict[str, Any]]
    proposal: dict[str, Any]
    trace: list[dict[str, Any]]


class LangGraphRuntime:
    def __init__(self, decide: DecisionFunction, checkpointer):
        self._decide = decide
        self._profile = profile(LANGGRAPH_PROFILE_ID)
        builder = StateGraph(_State)
        builder.add_node("wait_for_frame", self._wait_for_frame)
        builder.add_node("decide", self._decide_node)
        builder.add_edge(START, "wait_for_frame")
        builder.add_edge("wait_for_frame", "decide")
        builder.add_edge("decide", "wait_for_frame")
        self._graph = builder.compile(checkpointer=checkpointer, name="v7-agent-minimal")

    def _config(self, runtime_ref: str) -> dict:
        # LangGraph reserves checkpoint_ns for compiled subgraphs. A stable
        # thread prefix isolates this top-level graph without invoking subgraph
        # routing. Including the frozen profile digest prevents a future profile
        # from interpreting checkpoints written by another runtime definition.
        thread_id = f"p6-runtime-v1:{self._profile['profile_digest']}:{runtime_ref}"
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _wait_for_frame(state: _State) -> dict:
        payload = interrupt(
            {
                "kind": "FRAME_REQUIRED",
                "runtime_ref": state["runtime_ref"],
                "checkpoint_revision": state.get("checkpoint_revision", 0),
            }
        )
        return {
            "frame": payload["frame"],
            "frame_digest": payload["frame_digest"],
            "observations": payload.get("observations", []),
            "state": "ADVANCING",
        }

    def _decide_node(self, state: _State) -> dict:
        frame = frame_from_record(state["frame"])
        proposal = self._decide(frame, tuple(state.get("observations", [])))
        if proposal.frame_id != frame.frame_id:
            raise RuntimeConflict("The decision refers to another frame")
        trace = list(state.get("trace", []))
        trace.append(
            {
                "sequence": len(trace) + 1,
                "type": "PROPOSAL_CREATED",
                "frame_id": frame.frame_id,
                "proposal_digest": proposal.digest,
            }
        )
        return {
            "proposal": proposal.as_record(),
            "checkpoint_revision": state.get("checkpoint_revision", 0) + 1,
            "state": "WAITING_FRAME",
            "trace": trace,
        }

    def _snapshot(self, runtime_ref: str):
        try:
            snapshot = self._graph.get_state(self._config(runtime_ref))
        except Exception as error:
            raise RuntimeUnavailable("The runtime checkpoint store is unavailable") from error
        if not snapshot.values:
            raise RuntimeNotFound("Unknown runtime identity")
        return snapshot

    @staticmethod
    def _receipt(state: Mapping[str, Any]) -> RuntimeReceipt:
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
        # ``validate`` has already proved that every adapter-owned field is the
        # exact allowlisted LangGraph profile.  P7 deployments append immutable
        # tool/operator catalogue bindings to that profile, so comparing the
        # complete object with the bare adapter profile would reject every new
        # production AgentRun.
        if selected["profile_id"] != self._profile["profile_id"]:
            raise RuntimeConflict("The run profile does not belong to the LangGraph adapter")
        runtime_ref = run_spec.get("runtime_ref", initial_frame.agent_run_id)
        if not valid_identifier(runtime_ref):
            raise RuntimeConflict("Invalid runtime identity")
        config = self._config(runtime_ref)
        try:
            existing = self._graph.get_state(config)
            if existing.values:
                if existing.values.get("agent_run_id") != initial_frame.agent_run_id:
                    raise RuntimeConflict("The runtime identity belongs to another AgentRun")
                if existing.values.get("profile") != selected:
                    raise RuntimeConflict(
                        "The runtime identity belongs to another deployment profile"
                    )
                return self._receipt(existing.values)
            state: _State = {
                "runtime_ref": runtime_ref,
                "agent_run_id": initial_frame.agent_run_id,
                "profile": selected,
                "epoch": 1,
                "checkpoint_revision": 0,
                "state": "WAITING_FRAME",
                "initial_frame_digest": initial_frame.digest,
                "trace": [
                    {
                        "sequence": 1,
                        "type": "RUNTIME_STARTED",
                        "frame_id": initial_frame.frame_id,
                    }
                ],
            }
            result = self._graph.invoke(state, config)
            return self._receipt(result)
        except RuntimeAdapterError:
            raise
        except Exception as error:
            raise RuntimeUnavailable("The runtime checkpoint store is unavailable") from error

    def advance(
        self,
        runtime_ref: str,
        frame: DecisionFrame,
        prior_observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal:
        snapshot = self._snapshot(runtime_ref)
        state = snapshot.values
        if state.get("state") == "STOPPED":
            raise RuntimeStopped("The runtime has been interrupted")
        if frame.agent_run_id != state.get("agent_run_id", runtime_ref):
            raise RuntimeConflict("The frame belongs to another runtime")
        prior_frame = state.get("frame")
        if prior_frame and prior_frame.get("frame_id") == frame.frame_id:
            if state.get("frame_digest") != frame.digest:
                raise RuntimeConflict("The same frame identity has different content")
            if state.get("proposal"):
                return proposal_from_record(state["proposal"])
            if snapshot.next == ("decide",):
                try:
                    result = self._graph.invoke(None, self._config(runtime_ref))
                    return proposal_from_record(result["proposal"])
                except RuntimeAdapterError:
                    raise
                except Exception as error:
                    raise RuntimeUnavailable("The runtime could not resume the decision") from error
        if snapshot.next != ("wait_for_frame",):
            raise RuntimeConflict("The runtime is not waiting for a decision frame")
        try:
            result = self._graph.invoke(
                Command(
                    resume={
                        "frame": frame.as_record(),
                        "frame_digest": frame.digest,
                        "observations": [dict(item) for item in prior_observations],
                    }
                ),
                self._config(runtime_ref),
            )
            return proposal_from_record(result["proposal"])
        except RuntimeAdapterError:
            raise
        except Exception as error:
            raise RuntimeUnavailable("The runtime could not persist the decision") from error

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
        snapshot = self._snapshot(runtime_ref)
        state = snapshot.values
        if state.get("state") != "STOPPED":
            trace = list(state.get("trace", []))
            trace.append(
                {
                    "sequence": len(trace) + 1,
                    "type": "RUNTIME_INTERRUPTED",
                    "reason": reason,
                }
            )
            try:
                self._graph.update_state(
                    self._config(runtime_ref),
                    {
                        "state": "STOPPED",
                        "checkpoint_revision": state.get("checkpoint_revision", 0) + 1,
                        "trace": trace,
                    },
                    as_node="wait_for_frame",
                )
            except Exception as error:
                raise RuntimeUnavailable("The runtime checkpoint store is unavailable") from error
            state = self._snapshot(runtime_ref).values
        return RuntimeInterruptReceipt(runtime_ref, state["epoch"], "STOPPED", reason)

    def inspect(self, runtime_ref: str) -> RuntimeStatus:
        state = self._snapshot(runtime_ref).values
        proposal = state.get("proposal")
        frame = state.get("frame")
        return RuntimeStatus(
            runtime_ref=runtime_ref,
            epoch=state["epoch"],
            checkpoint_revision=state["checkpoint_revision"],
            state=state["state"],
            last_frame_id=frame.get("frame_id") if frame else None,
            last_proposal_digest=canonical_digest(proposal) if proposal else None,
        )

    def export_visible_trace(self, runtime_ref: str) -> RuntimeTraceRef:
        state = self._snapshot(runtime_ref).values
        trace = list(state.get("trace", []))
        return RuntimeTraceRef(
            runtime_ref=runtime_ref,
            epoch=state["epoch"],
            trace_digest=canonical_digest({"events": trace}),
            event_count=len(trace),
        )
