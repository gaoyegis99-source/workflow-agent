"""Optional redacted OpenTelemetry export; never a business-state dependency."""

from __future__ import annotations

import os
from importlib.metadata import version

from opentelemetry import trace

AGENT_VERSION = version("groundwater-agent-runtime")


def configure_telemetry():
    endpoint = os.environ.get("V7_AGENT_OTEL_ENDPOINT")
    if not endpoint:
        return trace.get_tracer("groundwater.agent", AGENT_VERSION)

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "groundwater-agent-worker",
                "service.version": AGENT_VERSION,
            }
        )
    )
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider.get_tracer("groundwater.agent", AGENT_VERSION)
