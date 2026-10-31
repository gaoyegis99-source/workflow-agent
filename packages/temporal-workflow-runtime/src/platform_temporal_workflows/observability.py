"""Small, safe OpenTelemetry helpers for V7 workflow boundaries.

The helpers accept only stable identities and enum-like lifecycle metadata.
They intentionally have no API for workflow input, prompts, URLs, headers,
credentials, raw plugin output, or exception messages.  OpenTelemetry is an
optional observer: tracing failures cannot change governed execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import ActionCommand, PluginManifest, TriggerKind


class SafeSpan:
    """Best-effort context manager that never makes observability mandatory."""

    def __init__(self, name: str, attributes: Mapping[str, str | int | bool]) -> None:
        self._name = name
        self._attributes = dict(attributes)
        self._context: Any | None = None

    def __enter__(self) -> SafeSpan:
        try:
            from opentelemetry import trace

            self._context = trace.get_tracer("groundwater.v7.temporal").start_as_current_span(
                self._name, attributes=self._attributes
            )
            self._context.__enter__()
        except Exception:  # pragma: no cover - the no-op path depends on deployment SDKs
            self._context = None
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if self._context is not None:
            try:
                self._context.__exit__(exc_type, exc, traceback)
            except Exception:  # pragma: no cover - tracing must never affect work
                pass
        return False


def activity_span(command: ActionCommand, manifest: PluginManifest) -> SafeSpan:
    """Trace one governed external boundary without payload-bearing fields."""

    return SafeSpan(
        "v7.temporal.activity",
        {
            "workflow.run_id": command.run_id,
            "workflow.action_id": command.action_id,
            "workflow.node_key": command.node_key,
            "workflow.definition_digest": command.workflow_digest,
            "plugin.id": manifest.plugin.plugin_id,
            "plugin.version": manifest.plugin.version,
            "plugin.kind": manifest.kind.value,
            "plugin.execution_mode": manifest.execution_mode.value,
        },
    )


def plugin_span(operation: str, command: ActionCommand, manifest: PluginManifest) -> SafeSpan:
    """Trace a reviewed process/remote plugin operation under its Activity."""

    return SafeSpan(
        "v7.plugin." + operation,
        {
            "workflow.run_id": command.run_id,
            "workflow.action_id": command.action_id,
            "plugin.id": manifest.plugin.plugin_id,
            "plugin.version": manifest.plugin.version,
            "plugin.kind": manifest.kind.value,
            "plugin.execution_mode": manifest.execution_mode.value,
        },
    )


def trigger_span(
    operation: str,
    *,
    trigger: TriggerKind | str | None = None,
    workflow_id: str | None = None,
    version: str | None = None,
    dispatch_id: str | None = None,
    run_id: str | None = None,
) -> SafeSpan:
    """Trace public intake/outbox delivery using only frozen identities."""

    attributes: dict[str, str | int | bool] = {"workflow.operation": operation}
    if trigger is not None:
        attributes["workflow.trigger"] = (
            trigger.value if isinstance(trigger, TriggerKind) else str(trigger)
        )
    if workflow_id is not None:
        attributes["workflow.id"] = workflow_id
    if version is not None:
        attributes["workflow.version"] = version
    if dispatch_id is not None:
        attributes["workflow.dispatch_id"] = dispatch_id
    if run_id is not None:
        attributes["workflow.run_id"] = run_id
    return SafeSpan("v7.trigger." + operation, attributes)
