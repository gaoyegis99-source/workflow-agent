"""Bounded, best-effort workflow diagnostics that never decide execution state."""

import hashlib
import json
import logging
import math
import time

from sqlalchemy.exc import SQLAlchemyError

SOURCES = {
    "BRIDGE",
    "DATA_CLIENT",
    "ENGINE",
    "EXECUTION_API",
    "EXECUTION_WORKER",
    "PLATFORM_CLIENT",
}

ATTRIBUTE_FIELDS = {
    "BRIDGE_STARTED": {"bridge_instance_id"},
    "ENGINE_BINDING_OBSERVED": {"kind", "binding_id"},
    "NODE_ACCEPTED": {"created"},
    "NODE_TERMINAL_OBSERVED": {"bridge_instance_id", "revision", "state"},
    "BRIDGE_RETURNED": {"bridge_instance_id", "revision"},
    "EXECUTION_CLAIMED": {"phase"},
    "PERMIT_REQUESTED": {"phase"},
    "PERMIT_ALLOWED": {"decision"},
    "PERMIT_WAIT": {"decision"},
    "PERMIT_DENIED": {"decision"},
    "CONTAINER_CREATED": {"container_id"},
    "CONTAINER_STARTED": {"container_id"},
    "CONTAINER_EXITED": {"container_id", "exit_code"},
    "OUTPUT_VERIFIED": {"file_count"},
    "NODE_OBSERVATION_REPORTED": {"revision", "state"},
    "ENGINE_CREATED": {"container_id"},
    "ENGINE_STARTED": {"container_id"},
    "ENGINE_EXITED": {"container_id", "exit_code"},
    "GATHER_FINISHED": {"member_count"},
    "PUBLICATION_PREPARED": {"state"},
    "PUBLICATION_COMMITTED": {"state"},
    "PUBLICATION_ABORTED": {"state"},
    "DIAGNOSTIC_PROBE": {"sample"},
}


def _check_scalar(value):
    return value is None or type(value) in {str, int, float, bool}


def build_event(
    *,
    workflow_execution_id,
    source,
    event_type,
    discriminator,
    node_action_id=None,
    engine_epoch=None,
    worker_generation=None,
    source_monotonic_ns=None,
    duration_ms=None,
    attributes=None,
):
    if source not in SOURCES or event_type not in ATTRIBUTE_FIELDS:
        raise ValueError("Unknown workflow trace event")
    attributes = attributes or {}
    if (
        type(attributes) is not dict
        or not set(attributes) <= ATTRIBUTE_FIELDS[event_type]
        or not all(
            isinstance(key, str) and _check_scalar(value) for key, value in attributes.items()
        )
    ):
        raise ValueError("Workflow trace attributes are not allowed")
    if source_monotonic_ns is not None and (
        type(source_monotonic_ns) is not int or source_monotonic_ns < 0
    ):
        raise ValueError("Invalid trace monotonic time")
    if duration_ms is not None and (
        type(duration_ms) not in {int, float} or duration_ms < 0 or not math.isfinite(duration_ms)
    ):
        raise ValueError("Invalid trace duration")
    identity = json.dumps(
        [workflow_execution_id, node_action_id, source, event_type, discriminator],
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {
        "event_key": hashlib.sha256(identity).hexdigest(),
        "workflow_execution_id": workflow_execution_id,
        "node_action_id": node_action_id,
        "engine_epoch": engine_epoch,
        "worker_generation": worker_generation,
        "source": source,
        "event_type": event_type,
        "source_monotonic_ns": source_monotonic_ns,
        "duration_ms": float(duration_ms) if duration_ms is not None else None,
        "attributes": attributes,
    }


def record(repository, **values):
    """Record diagnostics without allowing observability failure to alter execution."""
    try:
        event = build_event(**values)
    except (TypeError, ValueError):
        logging.warning(
            "Workflow trace rejected execution=%s node=%s event=%s",
            values.get("workflow_execution_id"),
            values.get("node_action_id"),
            values.get("event_type"),
        )
        return {"inserted": False, "failed": True, "write_ms": None}
    started = time.perf_counter_ns()
    try:
        inserted = repository.append_trace(event)
    except (OSError, SQLAlchemyError):
        logging.warning(
            "Workflow trace unavailable execution=%s node=%s event=%s",
            event["workflow_execution_id"],
            event["node_action_id"],
            event["event_type"],
        )
        return {"inserted": False, "failed": True, "write_ms": None}
    return {
        "inserted": inserted,
        "failed": False,
        "write_ms": (time.perf_counter_ns() - started) / 1_000_000,
    }
