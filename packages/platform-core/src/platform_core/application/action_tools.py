"""Ledger-first P6.4 gateway for controlled business actions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from platform_core.application.ports import Record, Store
from platform_core.application.tool_catalog import tool_spec, validate_tool_arguments
from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import RuleViolation, require, valid_identifier


@dataclass(frozen=True, slots=True)
class ActionResult:
    value: Record
    operation: str
    business_id: str
    action_id: str | None = None
    command_id: str | None = None
    refs: tuple[Record, ...] = ()


class ActionToolBackend(Protocol):
    def execute(
        self, tool_id: str, arguments: Record, context: Record, tool_request_id: str
    ) -> ActionResult: ...


def _request_id(run_id: str, frame_id: str, index: int, request_digest: str) -> str:
    raw = f"{run_id}:{frame_id}:{index}:{request_digest}".encode()
    return "tw" + hashlib.sha256(raw).hexdigest()[:62]


def _observation_id(tool_request_id: str) -> str:
    return "to" + hashlib.sha256(tool_request_id.encode()).hexdigest()[:62]


def _error_category(error: RuleViolation) -> tuple[str, str]:
    if error.code in {"AUTHENTICATION_REQUIRED", "SCOPE_DENIED"}:
        return "DENIED", "DENIED_SCOPE"
    if error.code == "RESOURCE_NOT_FOUND":
        return "NOT_FOUND", "NOT_FOUND"
    if error.code in {"DEPENDENCY_UNAVAILABLE", "RATE_LIMITED"}:
        return "TEMPORARILY_UNAVAILABLE", "TEMPORARILY_UNAVAILABLE"
    if error.code in {"BUDGET_BLOCKED", "BUDGET_EXHAUSTED"}:
        return "FAILED", "BUDGET_BLOCKED"
    if error.code in {
        "MALFORMED_REQUEST",
        "INVALID_BINDING",
        "UNSUPPORTED_TARGET",
        "PRECONDITION_UNRESOLVED",
        "REVISION_CONFLICT",
    }:
        return "FAILED", error.code
    return "FAILED", "CONFLICT"


class ActionToolGateway:
    """Dispatch frozen write tools through the existing Platform use cases."""

    def __init__(self, store: Store, catalog: Record, backend: ActionToolBackend):
        self.store = store
        self.catalog = catalog
        self.backend = backend

    def execute_proposal(self, proposal_id: str) -> list[Record]:
        with self.store.transaction() as uow:
            proposal = uow.agent_conversations.proposal_by_id(proposal_id)
            require(
                proposal["kind"] == "ACTION_REQUESTS",
                "INVALID_BINDING",
                "Proposal does not contain action tool requests",
                422,
            )
            run = uow.agent_runs.run_by_id(proposal["agent_run_id"], lock=True)
            task = uow.tasks.get(run["project_id"], run["task_id"], lock=True)
            frame = uow.agent_conversations.frame(proposal["frame_id"])
            uow.access.authorize_principal(run["created_by"], run["project_id"], "EDITOR")
            require(
                task.lifecycle == "OPEN"
                and task.control == "ACTIVE"
                and task.spec_revision == run["task_spec_revision"]
                and frame["task_spec_revision"] == task.spec_revision,
                "STALE_FRAME",
                "Task changed before action admission",
                409,
            )
            requests = proposal["proposal"].get("body", {}).get("requests")
            require(
                isinstance(requests, list) and 1 <= len(requests) <= 8,
                "INVALID_ARGUMENTS",
                "Action proposal requires one to eight requests",
                422,
            )
            call = uow.agent_budgets.model_call_for_frame(proposal["frame_id"])
            records = [
                self._reserve(
                    uow,
                    proposal,
                    run,
                    task,
                    index,
                    value,
                    call["model_call_id"] if call else None,
                )
                for index, value in enumerate(requests)
            ]
        return [self._execute(value["tool_request_id"]) for value in records]

    def _reserve(self, uow, proposal, run, task, index, value, model_call_id):
        require(isinstance(value, dict), "INVALID_ARGUMENTS", "Tool request must be an object", 422)
        required = {
            "call_id",
            "tool_id",
            "tool_version",
            "purpose",
            "arguments",
            "expected_observation",
        }
        require(
            set(value) == required
            and valid_identifier(value.get("call_id"))
            and isinstance(value.get("purpose"), str)
            and 0 < len(value["purpose"]) <= 1000,
            "INVALID_ARGUMENTS",
            "Malformed action tool request",
            422,
        )
        spec = tool_spec(self.catalog, value.get("tool_id"), value.get("tool_version"))
        require(
            spec["operation_class"] == "CONTROLLED_WRITE",
            "UNSUPPORTED_TARGET",
            "Read tools cannot be submitted as actions",
            422,
        )
        arguments = validate_tool_arguments(spec, value["arguments"])
        frozen = {
            "tool_id": value["tool_id"],
            "tool_version": value["tool_version"],
            "purpose": value["purpose"],
            "arguments": arguments,
            "project_id": run["project_id"],
            "task_id": task.task_id,
            "task_spec_revision": task.spec_revision,
            "catalog_digest": self.catalog["catalog_digest"],
        }
        request_digest = canonical_digest(frozen)
        identity = _request_id(run["agent_run_id"], proposal["frame_id"], index, request_digest)
        existing = uow.agent_tools.tool_request_for_proposal(proposal["proposal_id"], index)
        if existing is not None:
            require(
                existing["tool_request_id"] == identity
                and existing["request_digest"] == request_digest,
                "INVALID_BINDING",
                "Tool request replay differs",
                422,
            )
            return existing
        branch_key = canonical_digest({"tool_id": value["tool_id"], "arguments": arguments})
        attempts = uow.agent_tools.delivery_attempts(run["agent_run_id"], branch_key, lock=True)
        if attempts:
            last = attempts[-1]
            require(
                last["outcome"] != "UNKNOWN",
                "PRECONDITION_UNRESOLVED",
                "Unknown external outcome must be reconciled under its original identity",
            )
            repeated = [
                item
                for item in attempts
                if item["outcome"] == "FAILED"
                and item["failure_category"] == last["failure_category"]
            ]
            require(
                last["outcome"] != "STOPPED" and len(repeated) < 2,
                "PRECONDITION_UNRESOLVED",
                "Autonomous repair branch reached its fixed retry limit",
            )
        now = uow.now()
        saved = uow.agent_tools.add_tool_request(
            {
                "tool_request_id": identity,
                "agent_run_id": run["agent_run_id"],
                "proposal_id": proposal["proposal_id"],
                "frame_id": proposal["frame_id"],
                "proposal_index": index,
                "model_call_id": model_call_id,
                "call_id": value["call_id"],
                "tool_id": value["tool_id"],
                "tool_version": value["tool_version"],
                "purpose": value["purpose"],
                "arguments": arguments,
                "request_digest": request_digest,
                "status": "PENDING",
                "created_at": now,
                "updated_at": now,
            }
        )
        uow.agent_tools.add_delivery_attempt(
            {
                "attempt_id": uow.new_id(),
                "agent_run_id": run["agent_run_id"],
                "tool_request_id": identity,
                "tool_id": value["tool_id"],
                "branch_key": branch_key,
                "ordinal": len(attempts) + 1,
                "request_digest": request_digest,
                "outcome": "RUNNING",
                "failure_category": None,
                "retry_of_attempt_id": attempts[-1]["attempt_id"] if attempts else None,
                "stop_reason": None,
                "created_at": now,
                "finished_at": None,
            }
        )
        return saved

    def _execute(self, tool_request_id: str) -> Record:
        with self.store.transaction() as uow:
            request = uow.agent_tools.tool_request(tool_request_id, lock=True)
            existing = uow.agent_tools.tool_observation(tool_request_id)
            if existing is not None:
                return existing
            run = uow.agent_runs.run_by_id(request["agent_run_id"], lock=True)
            task = uow.tasks.get(run["project_id"], run["task_id"], lock=True)
            frame = uow.agent_conversations.frame(request["frame_id"])
            uow.access.authorize_principal(run["created_by"], run["project_id"], "EDITOR")
            require(
                task.lifecycle == "OPEN"
                and task.control == "ACTIVE"
                and task.spec_revision == run["task_spec_revision"]
                and frame["task_spec_revision"] == task.spec_revision,
                "STALE_FRAME",
                "Task changed before action execution",
                409,
            )
            context = {
                "project_id": run["project_id"],
                "task_id": task.task_id,
                "task_spec_revision": task.spec_revision,
                "principal_id": run["created_by"],
                "agent_run_id": run["agent_run_id"],
                "proposal_id": request["proposal_id"],
                "frame_id": request["frame_id"],
                "tool_version": request["tool_version"],
            }
            uow.agent_tools.update_tool_request(
                tool_request_id, status="RUNNING", updated_at=uow.now()
            )

        result = None
        status, error_category = "SUCCEEDED", None
        try:
            result = self.backend.execute(
                request["tool_id"], request["arguments"], context, tool_request_id
            )
            require(
                isinstance(result, ActionResult), "INVALID_BINDING", "Invalid action result", 422
            )
            value, refs = result.value, list(result.refs)
        except RuleViolation as error:
            status, error_category = _error_category(error)
            value, refs = {"error": error_category}, []

        result_digest = canonical_digest({"status": status, "result": value, "refs": refs})
        observation_id = _observation_id(tool_request_id)
        with self.store.transaction() as uow:
            request = uow.agent_tools.tool_request(tool_request_id, lock=True)
            existing = uow.agent_tools.tool_observation(tool_request_id)
            if existing is not None:
                return existing
            run = uow.agent_runs.run_by_id(request["agent_run_id"], lock=True)
            now = uow.now()
            attempt = uow.agent_tools.delivery_attempt_for_request(tool_request_id, lock=True)
            if result is not None:
                uow.agent_tools.add_action_link(
                    {
                        "link_id": "al" + hashlib.sha256(tool_request_id.encode()).hexdigest()[:62],
                        "tool_request_id": tool_request_id,
                        "agent_run_id": run["agent_run_id"],
                        "proposal_id": request["proposal_id"],
                        "operation": result.operation,
                        "business_id": result.business_id,
                        "action_id": result.action_id,
                        "command_id": result.command_id,
                        "request_digest": request["request_digest"],
                        "created_at": now,
                    }
                )
            saved = uow.agent_tools.add_tool_observation(
                {
                    "observation_id": observation_id,
                    "tool_request_id": tool_request_id,
                    "status": status,
                    "result": value,
                    "result_digest": result_digest,
                    "refs": refs,
                    "error_category": error_category,
                    "contains_untrusted_data": False,
                    "observed_at": now,
                }
            )
            uow.agent_tools.update_tool_request(
                tool_request_id,
                status="COMPLETED" if status == "SUCCEEDED" else "FAILED",
                updated_at=now,
            )
            stop_reason = None
            if attempt is not None:
                outcome = (
                    "SUCCEEDED"
                    if status == "SUCCEEDED"
                    else ("UNKNOWN" if status == "TEMPORARILY_UNAVAILABLE" else "FAILED")
                )
                prior = uow.agent_tools.delivery_attempts(
                    run["agent_run_id"], attempt["branch_key"]
                )
                repeated = sum(
                    value["outcome"] == "FAILED" and value["failure_category"] == error_category
                    for value in prior
                    if value["attempt_id"] != attempt["attempt_id"]
                )
                stop_reason = (
                    "REPEATED_FAILURE_LIMIT" if outcome == "FAILED" and repeated >= 1 else None
                )
                uow.agent_tools.finish_delivery_attempt(
                    attempt["attempt_id"],
                    outcome="STOPPED" if stop_reason else outcome,
                    failure_category=error_category,
                    stop_reason=stop_reason,
                    finished_at=now,
                )
                if stop_reason and run["status"] not in {"STOPPED", "BLOCKED"}:
                    uow.agent_runs.update_run(
                        run["agent_run_id"],
                        status="BLOCKED",
                        blocked_reason=stop_reason,
                        updated_at=now,
                    )
            event = {
                "source": "TOOL",
                "event_id": observation_id,
                "stream_id": tool_request_id,
                "source_revision": 1,
                "kind": "ACTION_TOOL_OBSERVED",
                "payload": {
                    "tool_request_id": tool_request_id,
                    "status": status,
                    "result_digest": result_digest,
                },
            }
            accepted = uow.agent_conversations.register_event(
                event
                | {
                    "agent_run_id": run["agent_run_id"],
                    "event_digest": canonical_digest(event),
                    "observed_at": now,
                }
            )
            if accepted and stop_reason is None:
                revision = run["wake_revision"] + 1
                uow.agent_conversations.add_wakeup(
                    {
                        "wake_id": uow.new_id(),
                        "agent_run_id": run["agent_run_id"],
                        "revision": revision,
                        "reason": "TOOL",
                        "event_ids": [observation_id],
                        "state": "PENDING",
                        "created_at": now,
                        "consumed_at": None,
                    }
                )
                if run["status"] not in {"STOPPED", "BLOCKED", "QUESTION"}:
                    uow.agent_runs.update_run(
                        run["agent_run_id"],
                        status="READY",
                        wake_revision=revision,
                        blocked_reason=None,
                        updated_at=now,
                    )
            return saved
