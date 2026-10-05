import uuid

import redis
import redis.asyncio as aioredis
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import app.middleware as middleware
from app.limiter import RedisTokenBucketLimiter
from app.main import app as fastapi_app

REDIS_TEST_URL = "redis://localhost:6379/1"
PREFIX = "test:labels:"
OVERRIDES_KEY = "test:labels:overrides"


def _flush():
    client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    for k in client.scan_iter(f"{PREFIX}*"):
        client.delete(k)
    client.close()


def _duration_paths():
    paths = set()
    for metric in REGISTRY.collect():
        if metric.name == "rateguard_request_duration_seconds":
            for sample in metric.samples:
                if "path" in sample.labels:
                    paths.add(sample.labels["path"])
    return paths


def test_request_duration_path_label_is_bounded(monkeypatch):
    """Random 404 URLs must all share one label value instead of creating a
    new Prometheus time series each, while real routes keep their own."""
    limiter = RedisTokenBucketLimiter(
        aioredis.from_url(REDIS_TEST_URL, decode_responses=True),
        rate=1000, capacity=1000, key_prefix=PREFIX, overrides_key=OVERRIDES_KEY,
    )
    monkeypatch.setattr(middleware, "limiter", limiter)
    _flush()
    try:
        before = _duration_paths()
        with TestClient(fastapi_app) as client:
            assert client.get("/data").status_code == 200
            assert client.get("/health").status_code == 200
            for _ in range(25):
                assert client.get(f"/scan-{uuid.uuid4()}").status_code == 404
        new_paths = _duration_paths() - before

        assert new_paths <= {"/data", "/health", "unmatched"}
        assert "unmatched" in _duration_paths()
        assert not any(p.startswith("/scan-") for p in _duration_paths())
        assert {"/data", "/health"} <= _duration_paths()
    finally:
        _flush()
