"""Ledger-first gateway for P6.3 read-only tools."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from platform_core.application.ports import Record, Store
from platform_core.application.tool_catalog import tool_spec, validate_tool_arguments
from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.control import RuleViolation, require, valid_identifier


@dataclass(frozen=True, slots=True)
class ReadResult:
    value: Record
    refs: tuple[Record, ...] = ()
    contains_untrusted_data: bool = False


class ReadToolBackend(Protocol):
    def authorize(self, tool_id: str, arguments: Record, context: Record) -> None: ...

    def execute(
        self, tool_id: str, arguments: Record, context: Record, tool_request_id: str
    ) -> ReadResult: ...


def _request_id(run_id: str, frame_id: str, index: int, request_digest: str) -> str:
    raw = f"{run_id}:{frame_id}:{index}:{request_digest}".encode()
    return "tr" + hashlib.sha256(raw).hexdigest()[:62]


def _error_category(error: RuleViolation) -> tuple[str, str]:
    if error.code in {"AUTHENTICATION_REQUIRED", "SCOPE_DENIED"}:
        return "DENIED", "DENIED_SCOPE"
    if error.code == "RESOURCE_NOT_FOUND":
        return "NOT_FOUND", "NOT_FOUND"
    if error.code in {"DEPENDENCY_UNAVAILABLE", "RATE_LIMITED"}:
        return "TEMPORARILY_UNAVAILABLE", "TEMPORARILY_UNAVAILABLE"
    if error.code in {"MALFORMED_REQUEST", "INVALID_BINDING", "UNSUPPORTED_TARGET"}:
        return "FAILED", "INVALID_ARGUMENTS"
    return "FAILED", "CONFLICT"


class ReadToolGateway:
    """Executes only frozen read tools; service I/O never occurs in a DB transaction."""

    def __init__(self, store: Store, catalog: Record, backend: ReadToolBackend):
        self.store = store
        self.catalog = catalog
        self.backend = backend

    def execute_proposal(self, proposal_id: str) -> list[Record]:
        with self.store.transaction() as uow:
            # Proposal IDs are global immutable identities.
            proposal = uow.agents.proposal_by_id(proposal_id)
            require(
                proposal["kind"] == "READ_TOOL_REQUESTS",
                "INVALID_BINDING",
                "Proposal does not contain read tool requests",
                422,
            )
            run = uow.agents.run_by_id(proposal["agent_run_id"], lock=True)
            task = uow.tasks.get(run["project_id"], run["task_id"], lock=True)
            frame = uow.agents.frame(proposal["frame_id"])
            uow.access.authorize_principal(run["created_by"], run["project_id"], "VIEWER")
            require(
                task.lifecycle == "OPEN"
                and task.control == "ACTIVE"
                and task.spec_revision == run["task_spec_revision"]
                and frame["task_spec_revision"] == task.spec_revision,
                "STALE_FRAME",
                "Task changed before the read tool request",
                409,
            )
            requests = proposal["proposal"].get("body", {}).get("requests")
            require(
                isinstance(requests, list) and 1 <= len(requests) <= 32,
                "INVALID_ARGUMENTS",
                "Read proposal requires one to 32 requests",
                422,
            )
            call = uow.agents.model_call_for_frame(proposal["frame_id"])
            records = []
            for index, value in enumerate(requests):
                records.append(
                    self._reserve(
                        uow,
                        proposal,
                        run,
                        task,
                        index,
                        value,
                        call["model_call_id"] if call else None,
                    )
                )

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
            and isinstance(value.get("tool_id"), str)
            and isinstance(value.get("tool_version"), str)
            and isinstance(value.get("purpose"), str)
            and 0 < len(value["purpose"]) <= 1000,
            "INVALID_ARGUMENTS",
            "Malformed read tool request",
            422,
        )
        spec = tool_spec(self.catalog, value["tool_id"], value["tool_version"])
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
        now = uow.now()
        return uow.agents.add_tool_request(
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

    def _execute(self, tool_request_id: str) -> Record:
        with self.store.transaction() as uow:
            request = uow.agents.tool_request(tool_request_id, lock=True)
            existing = uow.agents.tool_observation(tool_request_id)
            if existing is not None:
                return existing
            run = uow.agents.run_by_id(request["agent_run_id"], lock=True)
            task = uow.tasks.get(run["project_id"], run["task_id"], lock=True)
            frame = uow.agents.frame(request["frame_id"])
            uow.access.authorize_principal(run["created_by"], run["project_id"], "VIEWER")
            require(
                task.lifecycle == "OPEN"
                and task.control == "ACTIVE"
                and task.spec_revision == run["task_spec_revision"]
                and frame["task_spec_revision"] == task.spec_revision,
                "STALE_FRAME",
                "Task changed before read execution",
                409,
            )
            context = {
                "project_id": run["project_id"],
                "task_id": task.task_id,
                "task_spec_revision": task.spec_revision,
                "principal_id": run["created_by"],
                "agent_run_id": run["agent_run_id"],
            }
            uow.agents.update_tool_request(tool_request_id, status="RUNNING", updated_at=uow.now())

        status, error_category = "SUCCEEDED", None
        try:
            self.backend.authorize(request["tool_id"], request["arguments"], context)
            result = self.backend.execute(
                request["tool_id"], request["arguments"], context, tool_request_id
            )
            require(isinstance(result, ReadResult), "INVALID_BINDING", "Invalid tool result", 422)
            value, refs, untrusted = result.value, list(result.refs), result.contains_untrusted_data
        except RuleViolation as error:
            status, error_category = _error_category(error)
            value = {"error": error_category}
            refs, untrusted = [], False

        result_digest = canonical_digest({"status": status, "result": value, "refs": refs})
        observation_id = "to" + hashlib.sha256(tool_request_id.encode()).hexdigest()[:62]
        with self.store.transaction() as uow:
            request = uow.agents.tool_request(tool_request_id, lock=True)
            existing = uow.agents.tool_observation(tool_request_id)
            if existing is not None:
                return existing
            run = uow.agents.run_by_id(request["agent_run_id"], lock=True)
            now = uow.now()
            saved = uow.agents.add_tool_observation(
                {
                    "observation_id": observation_id,
                    "tool_request_id": tool_request_id,
                    "status": status,
                    "result": value,
                    "result_digest": result_digest,
                    "refs": refs,
                    "error_category": error_category,
                    "contains_untrusted_data": untrusted,
                    "observed_at": now,
                }
            )
            uow.agents.update_tool_request(
                tool_request_id,
                status="COMPLETED" if status == "SUCCEEDED" else "FAILED",
                updated_at=now,
            )
            event = {
                "source": "TOOL",
                "event_id": observation_id,
                "stream_id": tool_request_id,
                "source_revision": 1,
                "kind": "TOOL_OBSERVED",
                "payload": {
                    "tool_request_id": tool_request_id,
                    "status": status,
                    "result_digest": result_digest,
                },
            }
            event_digest = canonical_digest(event)
            accepted = uow.agents.register_event(
                event
                | {
                    "agent_run_id": run["agent_run_id"],
                    "event_digest": event_digest,
                    "observed_at": now,
                }
            )
            if accepted:
                revision = run["wake_revision"] + 1
                uow.agents.add_wakeup(
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
                    uow.agents.update_run(
                        run["agent_run_id"],
                        status="READY",
                        wake_revision=revision,
                        blocked_reason=None,
                        updated_at=now,
                    )
            return saved
