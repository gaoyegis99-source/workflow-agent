"""P6.2 model call admission, observation and accounting use cases."""

from __future__ import annotations

from datetime import timedelta

from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.application.ports import Record, Store
from platform_core.domain.agent_kernel import AgentLeaseClaim
from platform_core.domain.agent_runtime import DecisionFrame, DecisionProposal, canonical_digest
from platform_core.domain.control import Identity, require
from platform_core.domain.model_gateway import (
    ModelUsage,
    ProviderObservation,
    estimate_request,
)

TEST_TOOL_CATALOG = {
    "schema_version": 1,
    "tools": [
        {
            "tool_id": "test-observe",
            "version": "1.0.0",
            "operation": "READ_ONLY_TEST",
        }
    ],
}
TEST_TOOL_CATALOG_DIGEST = canonical_digest(TEST_TOOL_CATALOG)


def frozen_model_request(
    frame: DecisionFrame,
    profile: Record,
    *,
    repair_of: str | None = None,
    parse_errors: list[Record] | None = None,
    invalid_response_digest: str | None = None,
) -> Record:
    """Freeze either the P6.2 minimal context or a P6.3 compiled context."""
    compiled = frame.payload.get("context_policy_digest") is not None
    request = {
        "schema_version": 1,
        "profile_id": profile["profile_id"],
        "model": profile["model"],
        "frame": {
            "frame_id": frame.frame_id,
            "task_id": frame.task_id,
            "task_spec_revision": frame.task_spec_revision,
            "context_manifest_digest": frame.context_manifest_digest,
        },
        "context": (
            frame.payload
            if compiled
            else {
                "task_spec": frame.payload["task_spec"],
                "plan": frame.payload.get("plan"),
                "test_tools": TEST_TOOL_CATALOG["tools"],
            }
        ),
        "response_schema": "v7-decision-proposal-v1",
        "max_output_tokens": profile["max_output_tokens"],
    }
    if repair_of is not None:
        request["repair"] = {
            "repair_of_model_call_id": repair_of,
            "invalid_response_digest": invalid_response_digest,
            "schema_errors": parse_errors or [],
            "instruction": "Correct JSON structure only; preserve the intended decision.",
        }
    return request


class AgentModelCoordinator:
    def __init__(self, store: Store):
        self.store = store
        self.agents = AgentCoordinator(store)

    def prepare_call(
        self,
        claim: AgentLeaseClaim,
        frame: DecisionFrame,
        profile: Record,
        *,
        attempt: int = 1,
        repair_of: str | None = None,
        parse_errors: list[Record] | None = None,
        invalid_response_digest: str | None = None,
    ) -> Record:
        require(attempt in {1, 2}, "INVALID_BINDING", "Invalid model call attempt", 422)
        require(
            (attempt == 1 and repair_of is None)
            or (attempt == 2 and repair_of is not None and parse_errors is not None),
            "INVALID_BINDING",
            "Invalid model repair relationship",
            422,
        )
        request = frozen_model_request(
            frame,
            profile,
            repair_of=repair_of,
            parse_errors=parse_errors,
            invalid_response_digest=invalid_response_digest,
        )
        request_digest = canonical_digest(request)
        estimate = estimate_request(request, profile)
        require(
            estimate.input_tokens <= profile["max_context_tokens"],
            "BUDGET_BLOCKED",
            "Model context exceeds the frozen profile",
        )
        with self.store.transaction() as uow:
            owned = self.agents._require_owned(uow, claim)
            run, task = owned["run"], owned["task"]
            saved_frame = uow.agents.frame(frame.frame_id, lock=True)
            require(
                saved_frame["agent_run_id"] == run["agent_run_id"]
                and saved_frame["state"] == "PENDING"
                and saved_frame["task_spec_revision"] == task.spec_revision
                and saved_frame["control_version"] == task.control_version,
                "REVISION_CONFLICT",
                "Model call frame is stale",
            )
            budget = uow.agents.model_budget(run["agent_run_id"], lock=True)
            require(
                budget["profile_id"] == profile["profile_id"]
                and budget["profile_digest"] == profile["profile_digest"],
                "INVALID_BINDING",
                "Model profile differs from the Task budget",
                422,
            )
            require(
                saved_frame["sequence"] <= budget["max_decision_frames"],
                "BUDGET_BLOCKED",
                "Agent decision frame budget is exhausted",
            )
            require(
                uow.now()
                <= budget["created_at"] + timedelta(seconds=budget["max_agent_wall_seconds"]),
                "BUDGET_BLOCKED",
                "Agent wall-clock budget is exhausted",
            )
            existing = uow.agents.model_call_for_frame(frame.frame_id, attempt, lock=True)
            if existing is not None:
                require(
                    existing["request_digest"] == request_digest
                    and existing["profile_digest"] == profile["profile_digest"],
                    "INVALID_BINDING",
                    "Model call identity has conflicting content",
                    422,
                )
                return existing
            if attempt == 2:
                original = uow.agents.model_call(repair_of, lock=True)
                require(
                    original["frame_id"] == frame.frame_id
                    and original["attempt"] == 1
                    and original["status"] == "COMPLETED"
                    and original["proposal"] is None
                    and original["parse_errors"],
                    "PRECONDITION_UNRESOLVED",
                    "Format repair requires one invalid completed response",
                )
            now = uow.now()
            call = {
                "model_call_id": uow.new_id(),
                "agent_run_id": run["agent_run_id"],
                "frame_id": frame.frame_id,
                "attempt": attempt,
                "repair_of_model_call_id": repair_of,
                "epoch": run["epoch"],
                "generation": claim.generation,
                "profile_id": profile["profile_id"],
                "profile_digest": profile["profile_digest"],
                "context_digest": frame.context_manifest_digest,
                "tool_catalog_digest": frame.payload.get(
                    "tool_catalog_digest", TEST_TOOL_CATALOG_DIGEST
                ),
                "request": request,
                "request_digest": request_digest,
                "status": "RESERVED",
                "provider_ref": None,
                "provider_revision": 0,
                "response": None,
                "response_digest": None,
                "proposal": None,
                "proposal_digest": None,
                "parse_errors": None,
                "usage_status": "RESERVED",
                "contains_sensitive_context": True,
                "created_at": now,
                "sending_at": None,
                "accepted_at": None,
                "first_byte_at": None,
                "completed_at": None,
            }
            uow.agents.reserve_model_call(call, estimate.as_record())
            return call

    def mark_sending(self, claim: AgentLeaseClaim, model_call_id: str) -> Record:
        with self.store.transaction() as uow:
            self.agents._require_owned(uow, claim)
            call = uow.agents.model_call(model_call_id, lock=True)
            require(
                call["agent_run_id"] == claim.agent_run_id,
                "INVALID_BINDING",
                "Model call belongs to another AgentRun",
                422,
            )
            if call["status"] == "RESERVED":
                uow.agents.update_model_call(
                    model_call_id, status="SUBMITTING", sending_at=uow.now()
                )
                call = call | {"status": "SUBMITTING"}
            return call

    def observe(
        self,
        claim: AgentLeaseClaim,
        observation: ProviderObservation,
    ) -> Record:
        with self.store.transaction() as uow:
            self.agents._require_owned(uow, claim)
            call = uow.agents.model_call(observation.model_call_id, lock=True)
            require(
                call["agent_run_id"] == claim.agent_run_id,
                "INVALID_BINDING",
                "Model observation belongs to another AgentRun",
                422,
            )
            if call["provider_ref"] is not None and observation.provider_ref is not None:
                require(
                    call["provider_ref"] == observation.provider_ref,
                    "INVALID_BINDING",
                    "Provider response identity changed",
                    422,
                )
            if observation.provider_revision < call["provider_revision"]:
                return call
            now = uow.now()
            inserted = uow.agents.add_model_observation(
                {
                    "source_event_id": observation.source_event_id,
                    "model_call_id": observation.model_call_id,
                    "provider_revision": observation.provider_revision,
                    "provider_ref": observation.provider_ref,
                    "status": observation.status,
                    "executed": observation.executed,
                    "response_digest": observation.response_digest,
                    "usage": observation.usage,
                    "observation_digest": observation.digest,
                    "observed_at": now,
                }
            )
            if not inserted:
                return uow.agents.model_call(observation.model_call_id)
            usage = (
                ModelUsage.from_record(observation.usage).as_record()
                if observation.usage is not None
                else None
            )
            usage_status = call["usage_status"]
            if usage is not None:
                usage_status = "KNOWN"
            elif observation.executed is False:
                usage_status = "NOT_EXECUTED"
            elif observation.executed is True:
                usage_status = "UNKNOWN"
            changes = {
                "status": observation.status,
                "provider_ref": observation.provider_ref or call["provider_ref"],
                "provider_revision": observation.provider_revision,
                "usage_status": usage_status,
            }
            if observation.provider_ref and call["accepted_at"] is None:
                changes["accepted_at"] = now
            if observation.response is not None:
                changes.update(
                    response=observation.response,
                    response_digest=observation.response_digest,
                    first_byte_at=call["first_byte_at"] or now,
                )
            if observation.status in {"COMPLETED", "REJECTED", "ERROR", "CANCELLED"}:
                changes["completed_at"] = now
            uow.agents.update_model_call(observation.model_call_id, **changes)
            uow.agents.settle_model_reservation(
                observation.model_call_id,
                executed=observation.executed,
                status=observation.status,
                usage=usage,
                settled_at=now,
            )
            needs_reconciliation = observation.status in {
                "ACCEPTED",
                "RETRYABLE",
                "UNKNOWN",
                "ERROR",
            } or (observation.status == "COMPLETED" and usage is None)
            uow.agents.update_model_reconciliation(
                observation.model_call_id,
                required=needs_reconciliation,
                next_attempt_at=now + timedelta(seconds=1),
                error_code=observation.error_code,
            )
            return call | changes

    def save_parse_result(
        self,
        claim: AgentLeaseClaim,
        model_call_id: str,
        *,
        proposal: DecisionProposal | None,
        errors: list[Record] | None,
    ) -> Record:
        require(
            (proposal is None) != (errors is None),
            "INVALID_BINDING",
            "A model response has either a proposal or parse errors",
            422,
        )
        with self.store.transaction() as uow:
            self.agents._require_owned(uow, claim)
            call = uow.agents.model_call(model_call_id, lock=True)
            require(
                call["agent_run_id"] == claim.agent_run_id
                and call["status"] == "COMPLETED"
                and call["response_digest"] is not None,
                "PRECONDITION_UNRESOLVED",
                "Model response is not complete",
            )
            if call["proposal"] is not None or call["parse_errors"] is not None:
                if proposal is not None:
                    require(
                        call["proposal_digest"] == proposal.digest,
                        "INVALID_BINDING",
                        "Parsed proposal changed",
                        422,
                    )
                return call
            changes = (
                {"proposal": proposal.as_record(), "proposal_digest": proposal.digest}
                if proposal is not None
                else {"parse_errors": errors}
            )
            uow.agents.update_model_call(model_call_id, **changes)
            return call | changes

    def call_for_frame(self, frame_id: str) -> Record | None:
        with self.store.transaction() as uow:
            return uow.agents.model_call_for_frame(frame_id)

    def block(self, claim: AgentLeaseClaim, frame: DecisionFrame, reason: str) -> None:
        require(
            reason in {"MODEL_OUTPUT_INVALID", "MODEL_BUDGET_EXHAUSTED", "MODEL_REJECTED"},
            "INVALID_BINDING",
            "Invalid Agent model blocking reason",
            422,
        )
        with self.store.transaction() as uow:
            owned = self.agents._require_owned(uow, claim)
            uow.agents.update_frame(frame.frame_id, state="STALE")
            uow.agents.update_run(
                claim.agent_run_id,
                status="BLOCKED",
                blocked_reason=reason,
                updated_at=uow.now(),
            )
            uow.agents.release_lease(claim.agent_run_id, claim.owner, claim.generation)
            require(owned["task"].lifecycle == "OPEN", "REVISION_CONFLICT", "Task closed")

    def usage(self, actor: Identity, project_id: str, task_id: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project_id, "VIEWER")
            uow.tasks.get(project_id, task_id)
            run = uow.agents.run_by_task(task_id, required=True)
            return uow.agents.model_usage(run["agent_run_id"])
