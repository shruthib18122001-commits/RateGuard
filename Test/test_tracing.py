from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.tracing as tracing


def test_tracing_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_TRACING_ENABLED", raising=False)
    assert tracing.setup_tracing(FastAPI()) is None


def test_setup_survives_unreachable_exporter_endpoint(monkeypatch):
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")
    # Nothing listens on port 1; keep the exporter's retry budget short so
    # shutdown doesn't wait the default 10s.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    provider = tracing.setup_tracing(app)
    assert provider is not None
    try:
        with TestClient(app) as client:
            for _ in range(3):
                assert client.get("/ping").status_code == 200
        # Export to the dead endpoint fails, but only as a False result,
        # never as an exception propagating to the app.
        provider.force_flush(timeout_millis=3000)
    finally:
        provider.shutdown()
        FastAPIInstrumentor.uninstrument_app(app)
        RedisInstrumentor().uninstrument()


def test_setup_errors_are_swallowed(monkeypatch):
    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")

    import opentelemetry.sdk.trace as sdk_trace

    def boom(*args, **kwargs):
        raise RuntimeError("simulated setup failure")

    monkeypatch.setattr(sdk_trace, "TracerProvider", boom)
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    assert tracing.setup_tracing(app) is None
    assert TestClient(app).get("/ping").status_code == 200


def test_only_the_metrics_endpoint_is_excluded_from_tracing(monkeypatch):
    """/metrics scrapes are skipped, but a route whose path merely contains
    the word "metrics" must still be traced."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind

    monkeypatch.setenv("OTEL_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")

    app = FastAPI()

    @app.get("/metrics")
    def metrics():
        return {}

    @app.get("/items/{name}")
    def item(name: str):
        return {"name": name}

    provider = tracing.setup_tracing(app)
    assert provider is not None
    spans = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    try:
        with TestClient(app) as client:
            assert client.get("/metrics").status_code == 200
            assert client.get("/metrics?format=text").status_code == 200
            assert client.get("/items/metrics-team").status_code == 200
            assert client.get("/items/other").status_code == 200
    finally:
        provider.shutdown()
        FastAPIInstrumentor.uninstrument_app(app)
        RedisInstrumentor().uninstrument()

    server_spans = [s.name for s in spans.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert server_spans.count("GET /items/{name}") == 2
    assert not any("/metrics" in name for name in server_spans)
