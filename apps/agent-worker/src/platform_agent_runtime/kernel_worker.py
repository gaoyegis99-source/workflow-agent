"""P6.1 worker: one leased ledger frame, one runtime decision, then yield."""

from __future__ import annotations

from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.domain.control import RuleViolation

from platform_agent_runtime.errors import (
    ModelOutputInvalid,
    RuntimeConflict,
    RuntimeCorrupt,
    RuntimeNotFound,
    RuntimeUnavailable,
)


class KernelWorker:
    def __init__(
        self,
        database,
        runtime,
        owner: str,
        *,
        lease_seconds: float = 30,
        model_decision=None,
        context_compiler=None,
        read_tools=None,
        action_tools=None,
        action_reconciler=None,
        wait_reconciler=None,
        tracer=None,
        catalog_binding=None,
    ):
        self.coordinator = AgentCoordinator(database, context_compiler=context_compiler)
        self.runtime = runtime
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.model_decision = model_decision
        self.read_tools = read_tools
        self.action_tools = action_tools
        self.action_reconciler = action_reconciler
        self.wait_reconciler = wait_reconciler
        self.tracer = tracer
        self.catalog_binding = catalog_binding or {}

    def process_one(self) -> bool:
        if self.tracer is not None:
            with self.tracer.start_as_current_span("agent.process_one") as span:
                return self._process_one(span)
        return self._process_one(None)

    def _process_one(self, span) -> bool:
        reconciled = self.action_reconciler.process() if self.action_reconciler else False
        if self.wait_reconciler:
            reconciled = self.wait_reconciler.process() or reconciled
        claim = self.coordinator.claim(self.owner, self.lease_seconds)
        if claim is None:
            return reconciled
        frozen_tool = claim.runtime_profile.get("tool_catalog_digest")
        frozen_operator = claim.runtime_profile.get("operator_catalog_digest")
        if frozen_tool is not None or frozen_operator is not None:
            if frozen_tool != self.catalog_binding.get(
                "tool_catalog_digest"
            ) or frozen_operator != self.catalog_binding.get("operator_catalog_digest"):
                raise RuntimeError("AgentRun catalogue binding differs from this worker")
        elif str(self.catalog_binding.get("tool_catalog_id", "")).startswith("p7-"):
            raise RuntimeError("A P7 Agent worker cannot advance an unfrozen legacy AgentRun")
        if span is not None:
            span.set_attribute("agent.run.id", claim.agent_run_id)
            span.set_attribute("agent.runtime.epoch", claim.epoch)
            span.set_attribute("agent.worker.generation", claim.generation)
            span.set_attribute("task.id", claim.task_id)
        try:
            expected_revision = self.coordinator.runtime_revision(claim)
            try:
                status = self.runtime.inspect(claim.runtime_ref)
            except RuntimeNotFound:
                if expected_revision:
                    self.coordinator.recover_runtime_from_ledger(claim, "MISSING")
                    return True
                status = None
            except RuntimeCorrupt:
                self.coordinator.recover_runtime_from_ledger(claim, "CORRUPT")
                return True
            except RuntimeUnavailable:
                self.coordinator.release_transient(claim)
                return True
            if status is not None and status.checkpoint_revision != expected_revision:
                proven_model_result = (
                    status.checkpoint_revision > expected_revision
                    and self.model_decision is not None
                    and self.model_decision.runtime_ahead_is_proven(status)
                )
                if not proven_model_result:
                    reason = "BEHIND" if status.checkpoint_revision < expected_revision else "AHEAD"
                    self.coordinator.recover_runtime_from_ledger(claim, reason)
                    return True

            frame = self.coordinator.prepare_frame(claim)
            if self.model_decision is not None:
                self.model_decision.bind(claim)
            self.runtime.start(
                {
                    "runtime_profile": claim.runtime_profile,
                    "runtime_ref": claim.runtime_ref,
                },
                frame,
            )
            proposal = self.runtime.advance(claim.runtime_ref, frame, ())
            accepted = self.coordinator.accept_proposal(
                claim,
                frame,
                proposal,
                model_call_id=(
                    self.model_decision.last_model_call_id
                    if self.model_decision is not None
                    else None
                ),
            )
            if span is not None:
                span.set_attribute("agent.proposal.kind", proposal.kind)
                span.set_attribute("agent.proposal.id", accepted["proposal_id"])
            if proposal.kind == "READ_TOOL_REQUESTS" and self.read_tools is not None:
                self.read_tools.execute_proposal(accepted["proposal_id"])
            elif proposal.kind == "ACTION_REQUESTS" and self.action_tools is not None:
                self.action_tools.execute_proposal(accepted["proposal_id"])
        except RuntimeUnavailable:
            self.coordinator.release_transient(claim)
        except RuntimeConflict:
            self.coordinator.recover_runtime_from_ledger(claim, "CORRUPT")
        except ModelOutputInvalid:
            # The decision driver already wrote the bounded terminal failure.
            pass
        except RuleViolation as error:
            # Task revisions, pause/cancel and event-first races revoke the
            # lease transactionally. Their proposals are intentionally ignored.
            if error.code not in {"REVISION_CONFLICT", "PRECONDITION_UNRESOLVED"}:
                raise
        return True
