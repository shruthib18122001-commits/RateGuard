"""OpenTelemetry tracing setup.

Off by default; set OTEL_TRACING_ENABLED=true to turn it on. Spans are
exported over OTLP/gRPC to OTEL_EXPORTER_OTLP_ENDPOINT (the standard OTel
env var, default http://localhost:4317) -- Tempo in the Compose stack.

Export happens on a background thread (BatchSpanProcessor), so an
unreachable collector only produces log warnings and dropped spans; it
never blocks or fails a request. Any error while setting tracing up is
logged and the app carries on untraced.
"""
import logging
import os

from opentelemetry import trace

logger = logging.getLogger(__name__)

SERVICE_NAME = "rateguard"
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"


def tracing_enabled() -> bool:
    return os.environ.get("OTEL_TRACING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


def setup_tracing(app):
    """Install a global TracerProvider and auto-instrument FastAPI + Redis.

    Returns the TracerProvider, or None if tracing is disabled or could not
    be initialised."""
    if not tracing_enabled():
        return None

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)
        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        # Creating the exporter doesn't connect; the gRPC channel is lazy, so
        # a down collector can't fail startup here.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)

        # Prometheus scrapes /metrics every few seconds; tracing those would
        # just bury the interesting traces.
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="metrics")
        RedisInstrumentor().instrument(tracer_provider=provider)
    except Exception:
        logger.exception("Failed to initialise OpenTelemetry tracing; continuing without it")
        return None

    logger.info("OpenTelemetry tracing enabled, exporting to %s", endpoint)
    return provider
