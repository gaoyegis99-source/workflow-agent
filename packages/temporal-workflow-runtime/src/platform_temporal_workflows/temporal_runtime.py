"""Deterministic Temporal definition for a frozen V7 general workflow.

All I/O, model calls, ledger reads/writes, HTTP/MCP calls and container work
are delegated to Activities.  Workflow code only evaluates its frozen spec,
records deterministic commands, waits for signals/timers, and starts children.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any

from .models import (
    ActionCommand,
    GeneralWorkflowSpec,
    JsonValue,
    NodeKind,
    NodeOutcome,
    OutcomeStatus,
    PluginManifest,
    PluginReference,
    WorkflowInvocation,
    WorkflowNode,
    digest,
    json_copy,
)

try:  # Keep pure contract tests usable before optional worker dependencies install.
    from temporalio import workflow
    from temporalio.common import RetryPolicy
except (
    ImportError
) as _temporal_import_error:  # pragma: no cover - exercised at package install time
    workflow = None  # type: ignore[assignment]
    RetryPolicy = None  # type: ignore[assignment,misc]
else:
    _temporal_import_error = None


class WorkflowExecutionError(RuntimeError):
    pass


def _require_temporal() -> None:
    if workflow is None:
        raise RuntimeError(
            "temporalio is required for V7 Temporal workflow execution; install groundwater-temporal-workflow-runtime"
        ) from _temporal_import_error


def _pointer(value: object, pointer: str) -> object:
    current = value
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return _MISSING
    return current


_MISSING = object()
_SIGNAL_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _resolve_input(
    value: object, values: Mapping[str, JsonValue], item: object = _MISSING
) -> dict[str, JsonValue]:
    def resolve(raw: object) -> Any:
        if isinstance(raw, Mapping):
            if set(raw) == {"$ref"}:
                pointer = raw["$ref"]
                if not isinstance(pointer, str) or not pointer.startswith("/"):
                    raise WorkflowExecutionError("$ref must be a JSON pointer")
                resolved = _pointer(values, pointer)
                if resolved is _MISSING:
                    raise WorkflowExecutionError("workflow reference is unavailable")
                return resolved
            if set(raw) == {"$item"}:
                if raw["$item"] is not True or item is _MISSING:
                    raise WorkflowExecutionError("$item is only valid inside a bounded loop")
                return item
            return {str(key): resolve(child) for key, child in raw.items()}
        if isinstance(raw, list):
            return [resolve(child) for child in raw]
        return raw

    result = resolve(value)
    if not isinstance(result, Mapping):
        raise WorkflowExecutionError("plugin input must resolve to an object")
    return json_copy(dict(result))


def _manifest_lookup(spec: GeneralWorkflowSpec) -> dict[tuple[str, str, str], PluginManifest]:
    return {
        (item.plugin.plugin_id, item.plugin.version, item.plugin.package_digest): item
        for item in spec.plugins
    }


def _child_id(run_id: str, node_key: str) -> str:
    return "child-" + hashlib.sha256(f"{run_id}:{node_key}".encode()).hexdigest()[:52]


def _child_run_id(run_id: str, node_key: str) -> str:
    return "run-" + hashlib.sha256(f"{run_id}:{node_key}".encode()).hexdigest()[:52]


if workflow is not None:

    @workflow.defn(name="V7GeneralWorkflowV1")
    class GeneralWorkflow:
        """One durable execution per published definition/version/run identity."""

        def __init__(self) -> None:
            self._approvals: dict[str, dict[str, JsonValue]] = {}
            self._reconcile_requests: set[str] = set()
            self._cancel_reason: str | None = None

        @workflow.signal(name="approve")
        def approve(self, request: dict[str, Any]) -> None:
            node_key = request.get("node_key")
            approved = request.get("approved")
            actor_id = request.get("actor_id")
            if (
                not isinstance(node_key, str)
                or not node_key
                or len(node_key) > 512
                or not isinstance(approved, bool)
                or not isinstance(actor_id, str)
                or _SIGNAL_ACTOR.fullmatch(actor_id) is None
            ):
                raise WorkflowExecutionError("invalid approval signal")
            self._approvals[node_key] = {"approved": approved, "actor_id": actor_id}

        @workflow.signal(name="reconcile")
        def reconcile(self, request: dict[str, Any]) -> None:
            node_key = request.get("node_key")
            if not isinstance(node_key, str) or not node_key or len(node_key) > 512:
                raise WorkflowExecutionError("invalid reconcile signal")
            self._reconcile_requests.add(node_key)

        @workflow.signal(name="cancel")
        def cancel(self, reason: str = "operator requested cancellation") -> None:
            if not isinstance(reason, str):
                raise WorkflowExecutionError("invalid cancellation signal")
            # The supported client sends a stable code only. The authenticated
            # Platform API records any operator explanation in its governed
            # audit record, not in common Temporal history/events.
            self._cancel_reason = "CANCEL_REQUESTED"

        @workflow.query(name="status")
        def status(self) -> dict[str, Any]:
            return {
                "cancel_requested": self._cancel_reason is not None,
                "pending_approvals": sorted(self._approvals),
                "pending_reconciliations": sorted(self._reconcile_requests),
            }

        @workflow.run
        async def run(self, raw: dict[str, Any]) -> dict[str, Any]:
            invocation = WorkflowInvocation.from_mapping(raw)
            await self._event(invocation, "WORKFLOW_STARTED", {"trigger": invocation.trigger.value})
            values: dict[str, JsonValue] = dict(invocation.inputs)
            try:
                await self._run_steps(invocation, invocation.workflow.steps, "", values)
            except _Cancelled:
                await self._event(
                    invocation,
                    "WORKFLOW_CANCELLED",
                    {"reason_code": self._cancel_reason or "CANCEL_REQUESTED"},
                )
                return {"run_id": invocation.run_id, "status": "CANCELLED", "outputs": values}
            except _NodeTerminal as terminal:
                await self._event(
                    invocation,
                    "WORKFLOW_BLOCKED",
                    {
                        "node_key": terminal.node_key,
                        "status": terminal.outcome.status.value,
                        "error_code": terminal.outcome.error_code or "UNKNOWN",
                    },
                )
                return {
                    "run_id": invocation.run_id,
                    "status": terminal.outcome.status.value,
                    "node_key": terminal.node_key,
                    "action_id": terminal.outcome.action_id,
                    "outputs": values,
                }
            await self._event(invocation, "WORKFLOW_SUCCEEDED", {})
            return {"run_id": invocation.run_id, "status": "SUCCEEDED", "outputs": values}

        async def _run_steps(
            self,
            invocation: WorkflowInvocation,
            steps: Sequence[WorkflowNode],
            prefix: str,
            values: dict[str, JsonValue],
            *,
            item: object = _MISSING,
        ) -> None:
            for node in steps:
                if self._cancel_reason is not None:
                    raise _Cancelled()
                key = prefix + node.node_id
                await self._run_node(invocation, node, key, values, item=item)

        async def _run_node(
            self,
            invocation: WorkflowInvocation,
            node: WorkflowNode,
            key: str,
            values: dict[str, JsonValue],
            *,
            item: object,
        ) -> None:
            if node.kind in {NodeKind.AGENT, NodeKind.HTTP, NodeKind.MCP, NodeKind.CONTAINER}:
                reference = PluginReference.from_mapping(node.config["plugin"])
                manifest = _manifest_lookup(invocation.workflow).get(
                    (reference.plugin_id, reference.version, reference.package_digest)
                )
                if manifest is None:
                    raise WorkflowExecutionError("frozen plugin manifest is missing")
                command = ActionCommand.create(
                    invocation,
                    key,
                    reference,
                    _resolve_input(node.config.get("input", {}), values, item),
                    agent_max_turns=(
                        int(node.config.get("max_turns", 1))
                        if node.kind is NodeKind.AGENT
                        else None
                    ),
                )
                outcome = await self._execute_action(command, manifest, invocation)
                if outcome.status is not OutcomeStatus.SUCCEEDED:
                    raise _NodeTerminal(key, outcome)
                values[key] = dict(outcome.output or {})
                return
            if node.kind is NodeKind.APPROVAL:
                decision = await self._wait_for_approval(
                    invocation, key, str(node.config["prompt"])
                )
                if not decision["approved"]:
                    raise _NodeTerminal(
                        key,
                        NodeOutcome(
                            action_id="approval-" + hashlib.sha256(key.encode()).hexdigest()[:48],
                            status=OutcomeStatus.DENIED,
                            error_code="APPROVAL_REJECTED",
                        ),
                    )
                values[key] = {"approved": True, "actor_id": decision["actor_id"]}
                return
            if node.kind is NodeKind.TIMER:
                await workflow.sleep(timedelta(seconds=int(node.config["seconds"])))
                values[key] = {"elapsed_seconds": int(node.config["seconds"])}
                return
            if node.kind is NodeKind.CONDITION:
                actual = _pointer(values, str(node.config["path"]))
                branch = 0 if actual == node.config["equals"] else 1
                await self._run_steps(
                    invocation, node.children[branch], key + "/", values, item=item
                )
                values[key] = {"branch": "then" if branch == 0 else "otherwise"}
                return
            if node.kind is NodeKind.PARALLEL:
                baseline = json_copy(values)
                branches = await asyncio.gather(
                    *(
                        self._run_parallel_branch(
                            invocation,
                            branch,
                            f"{key}/{index}/",
                            baseline,
                            item,
                        )
                        for index, branch in enumerate(node.children)
                    )
                )
                merged: list[JsonValue] = []
                for branch_values in branches:
                    additions = {
                        name: value for name, value in branch_values.items() if name not in baseline
                    }
                    values.update(additions)
                    merged.append(additions)
                values[key] = {"branches": merged}
                return
            if node.kind is NodeKind.LOOP:
                items = _pointer(values, str(node.config["items_path"]))
                maximum = int(node.config["max_iterations"])
                if not isinstance(items, list) or len(items) > maximum:
                    raise WorkflowExecutionError("loop input is not a bounded list")
                baseline = json_copy(values)
                iteration_outputs: list[JsonValue] = []
                for index, loop_item in enumerate(items):
                    local_values = json_copy(baseline)
                    await self._run_steps(
                        invocation,
                        node.children[0],
                        f"{key}/{index}/",
                        local_values,
                        item=loop_item,
                    )
                    iteration_outputs.append(
                        {
                            name: value
                            for name, value in local_values.items()
                            if name not in baseline
                        }
                    )
                values[key] = {"iterations": iteration_outputs}
                return
            if node.kind is NodeKind.SUBWORKFLOW:
                await self._run_subworkflow(invocation, node, key, values)
                return
            raise WorkflowExecutionError("unsupported workflow node")

        async def _run_parallel_branch(
            self,
            invocation: WorkflowInvocation,
            branch: Sequence[WorkflowNode],
            prefix: str,
            baseline: Mapping[str, JsonValue],
            item: object,
        ) -> dict[str, JsonValue]:
            local_values = json_copy(baseline)
            await self._run_steps(invocation, branch, prefix, local_values, item=item)
            return local_values

        async def _run_subworkflow(
            self,
            invocation: WorkflowInvocation,
            node: WorkflowNode,
            key: str,
            values: dict[str, JsonValue],
        ) -> None:
            target = (str(node.config["workflow_id"]), str(node.config["version"]))
            catalogue = {(item.workflow_id, item.version): item for item in invocation.subworkflows}
            child_spec = catalogue.get(target)
            if child_spec is None:
                raise WorkflowExecutionError("frozen subworkflow is unavailable")
            child_run = _child_run_id(invocation.run_id, key)
            child_invocation = WorkflowInvocation(
                run_id=child_run,
                workflow=child_spec,
                inputs=values,
                fence=replace(
                    invocation.fence,
                    release_digest=child_spec.release_digest,
                ),
                trigger=invocation.trigger,
                task_queue=invocation.task_queue,
                subworkflows=tuple(item for item in invocation.subworkflows if item != child_spec),
            )
            result = await workflow.execute_child_workflow(
                GeneralWorkflow.run,
                child_invocation.to_mapping(),
                id=_child_id(invocation.run_id, key),
                task_queue=invocation.task_queue,
            )
            if result.get("status") != "SUCCEEDED":
                status = result.get("status")
                try:
                    outcome_status = OutcomeStatus(status)
                except (TypeError, ValueError):
                    outcome_status = OutcomeStatus.FAILED
                raise _NodeTerminal(
                    key,
                    NodeOutcome(
                        action_id="child-" + hashlib.sha256(key.encode()).hexdigest()[:48],
                        status=outcome_status,
                        error_code="SUBWORKFLOW_" + str(status or "FAILED"),
                    ),
                )
            values[key] = {"child_run_id": child_run, "outputs": result.get("outputs", {})}

        async def _execute_action(
            self, command: ActionCommand, manifest: PluginManifest, invocation: WorkflowInvocation
        ) -> NodeOutcome:
            while True:
                raw = await workflow.execute_activity(
                    "v7_execute_governed_node",
                    command.to_mapping(),
                    start_to_close_timeout=timedelta(seconds=manifest.timeout_seconds),
                    schedule_to_close_timeout=timedelta(
                        seconds=manifest.timeout_seconds * manifest.max_attempts
                    ),
                    retry_policy=RetryPolicy(maximum_attempts=manifest.max_attempts),
                )
                outcome = NodeOutcome.from_mapping(raw)
                if outcome.status is not OutcomeStatus.RECONCILING:
                    return outcome
                await self._event(
                    invocation,
                    "NODE_RECONCILING",
                    {"node_key": command.node_key, "action_id": command.action_id},
                )
                await workflow.wait_condition(
                    lambda: (
                        self._cancel_reason is not None
                        or command.node_key in self._reconcile_requests
                    )
                )
                if self._cancel_reason is not None:
                    raise _Cancelled()
                self._reconcile_requests.discard(command.node_key)

        async def _wait_for_approval(
            self, invocation: WorkflowInvocation, node_key: str, prompt: str
        ) -> Mapping[str, JsonValue]:
            await self._event(
                invocation,
                "APPROVAL_REQUESTED",
                {"node_key": node_key, "prompt_digest": digest({"prompt": prompt})},
            )
            await workflow.wait_condition(
                lambda: self._cancel_reason is not None or node_key in self._approvals
            )
            if self._cancel_reason is not None:
                raise _Cancelled()
            decision = self._approvals.pop(node_key)
            await self._event(
                invocation,
                "APPROVAL_RECORDED",
                {
                    "node_key": node_key,
                    "approved": decision["approved"],
                    "actor_id": decision["actor_id"],
                },
            )
            return decision

        async def _event(
            self, invocation: WorkflowInvocation, event_type: str, payload: Mapping[str, JsonValue]
        ) -> None:
            await workflow.execute_activity(
                "v7_record_workflow_event",
                {"run_id": invocation.run_id, "event_type": event_type, "payload": dict(payload)},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

    @workflow.defn(name="V7ScheduledInvocationWorkflowV1")
    class ScheduledInvocationWorkflow:
        """Schedule adapter that gives every schedule fire a new governed run id."""

        @workflow.run
        async def run(self, raw: dict[str, Any]) -> dict[str, Any]:
            template = WorkflowInvocation.from_mapping(raw)
            unique_run_id = (
                "run-" + hashlib.sha256(workflow.info().run_id.encode()).hexdigest()[:52]
            )
            invocation = WorkflowInvocation(
                run_id=unique_run_id,
                workflow=template.workflow,
                inputs=template.inputs,
                fence=template.fence,
                trigger=template.trigger,
                task_queue=template.task_queue,
                subworkflows=template.subworkflows,
            )
            return await workflow.execute_child_workflow(
                GeneralWorkflow.run,
                invocation.to_mapping(),
                id=_child_id(unique_run_id, "scheduled"),
                task_queue=invocation.task_queue,
            )


else:

    class GeneralWorkflow:  # pragma: no cover - only used to give a useful dependency failure
        def __init__(self) -> None:
            _require_temporal()

    class ScheduledInvocationWorkflow:  # pragma: no cover - only used when dependency is absent
        def __init__(self) -> None:
            _require_temporal()


class _Cancelled(Exception):
    pass


class _NodeTerminal(Exception):
    def __init__(self, node_key: str, outcome: NodeOutcome) -> None:
        self.node_key = node_key
        self.outcome = outcome
