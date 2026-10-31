"""Controlled entry points for manual, API, webhook, and scheduled triggers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import timedelta
from typing import Any

from .models import TriggerKind, WorkflowContractError, WorkflowInvocation
from .observability import trigger_span
from .temporal_runtime import GeneralWorkflow, ScheduledInvocationWorkflow

_SCHEDULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class TemporalTriggerClient:
    """Thin client boundary; API/Webhook authentication happens before this call."""

    def __init__(self, temporal_client: Any) -> None:
        self._client = temporal_client

    async def start_manual(self, invocation: WorkflowInvocation) -> Any:
        return await self._start(invocation, TriggerKind.MANUAL)

    async def start_api(self, invocation: WorkflowInvocation) -> Any:
        return await self._start(invocation, TriggerKind.API)

    async def start_webhook(self, invocation: WorkflowInvocation) -> Any:
        return await self._start(invocation, TriggerKind.WEBHOOK)

    async def approve(self, run_id: str, *, node_key: str, approved: bool, actor_id: str) -> None:
        handle = self._client.get_workflow_handle(_workflow_id(run_id))
        with trigger_span("approve", run_id=run_id):
            await handle.signal(
                "approve", {"node_key": node_key, "approved": approved, "actor_id": actor_id}
            )

    async def reconcile(self, run_id: str, *, node_key: str) -> None:
        handle = self._client.get_workflow_handle(_workflow_id(run_id))
        with trigger_span("reconcile", run_id=run_id):
            await handle.signal("reconcile", {"node_key": node_key})

    async def cancel(self, run_id: str, *, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise WorkflowContractError("cancellation reason is required")
        handle = self._client.get_workflow_handle(_workflow_id(run_id))
        # Temporal signal payloads become history. The authenticated Platform
        # API records any operator explanation in its governed audit record;
        # this orchestration signal carries only a stable safe code.
        with trigger_span("cancel", run_id=run_id):
            await handle.signal("cancel", "CANCEL_REQUESTED")

    async def create_interval_schedule(
        self,
        schedule_id: str,
        invocation: WorkflowInvocation,
        *,
        every_seconds: int,
        dispatch_id: str | None = None,
    ) -> Any:
        if not _SCHEDULE_ID.fullmatch(schedule_id):
            raise WorkflowContractError("schedule_id must be a portable identifier")
        if (
            not isinstance(every_seconds, int)
            or isinstance(every_seconds, bool)
            or every_seconds < 60
        ):
            raise WorkflowContractError(
                "schedule interval must be an integer of at least 60 seconds"
            )
        try:
            from temporalio.client import (
                Schedule,
                ScheduleActionStartWorkflow,
                ScheduleIntervalSpec,
                ScheduleOverlapPolicy,
                SchedulePolicy,
                ScheduleSpec,
            )
            from temporalio.service import RPCError, RPCStatusCode
        except ImportError as error:  # pragma: no cover - verified by package installation test
            raise RuntimeError("temporalio is required to create V7 schedules") from error
        scheduled = replace(invocation, trigger=TriggerKind.SCHEDULE)
        memo = {"v7_temporal_dispatch_id": dispatch_id} if dispatch_id is not None else None
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                ScheduledInvocationWorkflow.run,
                scheduled.to_mapping(),
                id=_schedule_workflow_id(schedule_id),
                task_queue=scheduled.task_queue,
                memo=memo,
            ),
            spec=ScheduleSpec(
                intervals=[ScheduleIntervalSpec(every=timedelta(seconds=every_seconds))]
            ),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        )
        with trigger_span(
            "schedule",
            trigger=TriggerKind.SCHEDULE,
            workflow_id=invocation.workflow.workflow_id,
            version=invocation.workflow.version,
            dispatch_id=dispatch_id,
            run_id=invocation.run_id,
        ):
            try:
                return await self._client.create_schedule(schedule_id, schedule)
            except RPCError as error:
                if error.status is not RPCStatusCode.ALREADY_EXISTS or dispatch_id is None:
                    raise
                handle = self._client.get_schedule_handle(schedule_id)
                description = await handle.describe()
                existing = description.schedule.action
                if not isinstance(existing, ScheduleActionStartWorkflow) or existing.memo != memo:
                    raise WorkflowContractError(
                        "Temporal schedule id is already bound to another frozen dispatch"
                    ) from error
                return handle

    async def _start(self, invocation: WorkflowInvocation, trigger: TriggerKind) -> Any:
        if invocation.trigger not in {TriggerKind.MANUAL, trigger}:
            raise WorkflowContractError("trigger source does not match the frozen invocation")
        bound = replace(invocation, trigger=trigger)
        try:
            from temporalio.exceptions import WorkflowAlreadyStartedError
        except ImportError as error:  # pragma: no cover - package requires temporalio
            raise RuntimeError("temporalio is required to start V7 workflows") from error
        with trigger_span(
            "start",
            trigger=trigger,
            workflow_id=bound.workflow.workflow_id,
            version=bound.workflow.version,
            run_id=bound.run_id,
        ):
            try:
                return await self._client.start_workflow(
                    GeneralWorkflow.run,
                    bound.to_mapping(),
                    id=_workflow_id(bound.run_id),
                    task_queue=bound.task_queue,
                )
            except WorkflowAlreadyStartedError:
                # The outbox can retry after Temporal accepted a start but before
                # the worker marked delivery complete. The workflow id is derived
                # from the immutable V7 run id, so returning its handle is safe.
                return self._client.get_workflow_handle(_workflow_id(bound.run_id))


def _workflow_id(run_id: str) -> str:
    return "v7-" + run_id


def _schedule_workflow_id(schedule_id: str) -> str:
    # Temporal Schedule requires a workflow ID. Each Schedule action has this
    # durable scheduling identity, while ScheduledInvocationWorkflow derives a
    # distinct V7 business run ID from Temporal's per-fire run ID.
    digest = hashlib.sha256(schedule_id.encode("utf-8")).hexdigest()
    return "v7-schedule-" + digest[:52]
