"""Strict structured-output decision driver backed by the P6.2 model ledger."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from platform_contracts.agent_runtime import parse_decision_proposal
from platform_core.application.model_calls import AgentModelCoordinator
from platform_core.domain.agent_runtime import DecisionFrame, DecisionProposal
from platform_core.domain.control import RuleViolation

from platform_agent_runtime.errors import ModelOutputInvalid, RuntimeUnavailable


def parse_proposal(response, frame):
    value = response.get("output") if isinstance(response, dict) else None
    try:
        wire = parse_decision_proposal(value)
    except ValueError as error:
        details = error.errors(include_url=False, include_input=False)
        failures = [
            {
                "path": list(item["loc"]),
                "type": item["type"],
                "message": item["msg"][:200],
            }
            for item in details
        ]
        return None, failures
    record = wire.model_dump(mode="json")
    kind = record.pop("kind")
    frame_id = record.pop("frame_id")
    revision = record.pop("expected_task_revision")
    proposal = DecisionProposal(kind, frame_id, revision, record)
    if (
        proposal.frame_id != frame.frame_id
        or proposal.expected_task_revision != frame.task_spec_revision
    ):
        return None, [
            {
                "path": ["frame_id"],
                "type": "identity",
                "message": "Proposal targets another frame or Task revision",
            }
        ]
    return proposal, None


class ModelDecisionDriver:
    def __init__(self, database, provider, profile):
        self.coordinator = AgentModelCoordinator(database)
        self.provider = provider
        self.profile = profile
        self.claim = None
        self.last_model_call_id = None

    def bind(self, claim):
        self.claim = claim
        self.last_model_call_id = None

    def runtime_ahead_is_proven(self, status):
        if not status.last_frame_id or not status.last_proposal_digest:
            return False
        call = self.coordinator.call_for_frame(status.last_frame_id)
        return bool(
            call
            and call["status"] == "COMPLETED"
            and call["proposal_digest"] == status.last_proposal_digest
        )

    def __call__(
        self,
        frame: DecisionFrame,
        observations: Sequence[Mapping[str, object]],
    ) -> DecisionProposal:
        if self.claim is None or self.claim.agent_run_id != frame.agent_run_id:
            raise RuntimeUnavailable("Model decision driver has no current Agent lease")
        repair = None
        for attempt in (1, 2):
            try:
                call = self.coordinator.prepare_call(
                    self.claim,
                    frame,
                    self.profile,
                    attempt=attempt,
                    repair_of=repair["model_call_id"] if repair else None,
                    parse_errors=repair["parse_errors"] if repair else None,
                    invalid_response_digest=repair["response_digest"] if repair else None,
                )
            except RuleViolation as error:
                if error.code != "BUDGET_BLOCKED":
                    raise
                self.coordinator.block(self.claim, frame, "MODEL_BUDGET_EXHAUSTED")
                raise ModelOutputInvalid("Model budget cannot admit this decision") from error
            self.last_model_call_id = call["model_call_id"]
            if call["proposal"] is not None:
                value = call["proposal"]
                return DecisionProposal(
                    value["kind"],
                    value["frame_id"],
                    value["expected_task_revision"],
                    value["body"],
                )
            if call["status"] != "COMPLETED":
                call = self.coordinator.mark_sending(self.claim, call["model_call_id"])
                observation = (
                    self.provider.query(call)
                    if call.get("provider_ref")
                    else self.provider.submit(call)
                )
                call = self.coordinator.observe(self.claim, observation)
            if call["status"] in {"REJECTED", "CANCELLED"}:
                self.coordinator.block(self.claim, frame, "MODEL_REJECTED")
                raise ModelOutputInvalid("Model provider explicitly rejected the call")
            if call["status"] != "COMPLETED" or call.get("response") is None:
                raise RuntimeUnavailable("Model result is not yet confirmed")
            proposal, errors = parse_proposal(call["response"], frame)
            call = self.coordinator.save_parse_result(
                self.claim,
                call["model_call_id"],
                proposal=proposal,
                errors=errors,
            )
            if proposal is not None:
                return proposal
            repair = call
        self.coordinator.block(self.claim, frame, "MODEL_OUTPUT_INVALID")
        raise ModelOutputInvalid("Model output failed strict validation and one format repair")
