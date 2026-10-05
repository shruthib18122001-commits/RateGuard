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
