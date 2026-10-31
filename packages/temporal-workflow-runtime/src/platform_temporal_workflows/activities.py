"""Temporal Activity wrappers around the governed V7 execution boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .governance import GovernanceLedger, GovernedActivityService
from .models import ActionCommand, WorkflowContractError, contains_secret_name
from .observability import trigger_span

try:
    from temporalio import activity
except (
    ImportError
) as _temporal_import_error:  # pragma: no cover - package integration check covers it
    activity = None  # type: ignore[assignment]
else:
    _temporal_import_error = None


if activity is not None:

    class V7TemporalActivities:
        """Activities may perform I/O; the Temporal Workflow itself may not."""

        def __init__(self, service: GovernedActivityService, ledger: GovernanceLedger) -> None:
            self._service = service
            self._ledger = ledger

        @activity.defn(name="v7_execute_governed_node")
        async def execute_governed_node(self, raw: dict[str, Any]) -> dict[str, Any]:
            command = ActionCommand.from_mapping(raw)
            outcome = await self._service.execute(command)
            return dict(outcome.to_mapping())

        @activity.defn(name="v7_record_workflow_event")
        async def record_workflow_event(self, raw: dict[str, Any]) -> None:
            run_id = raw.get("run_id")
            event_type = raw.get("event_type")
            payload = raw.get("payload")
            if (
                not isinstance(run_id, str)
                or not isinstance(event_type, str)
                or not isinstance(payload, Mapping)
                or contains_secret_name(payload)
            ):
                raise WorkflowContractError("invalid or sensitive workflow event")
            with trigger_span("workflow-event", run_id=run_id):
                await self._ledger.workflow_event(run_id, event_type, dict(payload))


else:

    class V7TemporalActivities:  # pragma: no cover - only used when a dependency is absent
        def __init__(self, service: GovernedActivityService, ledger: GovernanceLedger) -> None:
            del service, ledger
            raise RuntimeError(
                "temporalio is required for V7 Temporal Activities"
            ) from _temporal_import_error
