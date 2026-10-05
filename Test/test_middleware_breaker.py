"""RateLimitMiddleware behaviour when Redis is unavailable.

Healthy-path requests go to the real Redis test DB like the other tests;
"Redis is stopped" is simulated with a limiter pointed at a closed port,
so the middleware sees a genuine redis.ConnectionError."""
import redis
import redis.asyncio as aioredis
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import app.middleware as middleware
from app.circuit_breaker import BreakerState, CircuitBreaker
from app.limiter import RedisTokenBucketLimiter
from app.main import app as fastapi_app

REDIS_TEST_URL = "redis://localhost:6379/1"
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"
PREFIX = "test:breaker-mw:"
OVERRIDES_KEY = "test:breaker-mw:overrides"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _flush_test_keys():
    client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    for k in client.scan_iter(f"{PREFIX}*"):
        client.delete(k)
    client.close()


def _limiter(url):
    client = aioredis.from_url(url, decode_responses=True,
                               socket_timeout=0.5, socket_connect_timeout=0.5)
    return RedisTokenBucketLimiter(client, rate=0, capacity=3,
                                   key_prefix=PREFIX, overrides_key=OVERRIDES_KEY)


def _install(monkeypatch, policy, threshold=2, timeout=10):
    clock = FakeClock()
    breaker = CircuitBreaker(threshold, timeout, clock=clock,
                             on_transition=middleware._record_breaker_transition)
    monkeypatch.setattr(middleware, "breaker", breaker)
    monkeypatch.setattr(middleware, "FALLBACK_POLICY", policy)
    middleware.BREAKER_STATE.set(0)
    return breaker, clock


def _metric(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_fail_open_serves_requests_while_redis_is_down_and_recovers(monkeypatch):
    _flush_test_keys()
    breaker, clock = _install(monkeypatch, middleware.FAIL_OPEN, threshold=2, timeout=10)
    healthy, dead = _limiter(REDIS_TEST_URL), _limiter(DEAD_REDIS_URL)
    opened_before = _metric("rateguard_breaker_transitions_total", from_state="closed", to_state="open")
    try:
        with TestClient(fastapi_app) as client:
            monkeypatch.setattr(middleware, "limiter", healthy)
            r = client.get("/data", headers={"x-api-key": "tenant"})
            assert r.status_code == 200
            assert r.headers["X-RateLimit-Remaining"] == "2"

            # Redis goes away: each request still gets served, unthrottled.
            monkeypatch.setattr(middleware, "limiter", dead)
            for _ in range(4):
                r = client.get("/data", headers={"x-api-key": "tenant"})
                assert r.status_code == 200
                assert r.json() == {"message": "This is rate-limited data"}
                assert r.headers["X-RateGuard-Fallback"] == "fail-open"
                assert "X-RateLimit-Remaining" not in r.headers
            assert breaker.state is BreakerState.OPEN
            assert _metric("rateguard_breaker_state") == 1
            assert _metric("rateguard_breaker_transitions_total",
                           from_state="closed", to_state="open") == opened_before + 1

            # Redis is back; after the reset timeout the half_open trial
            # succeeds and normal limiting (state kept in Redis) resumes.
            monkeypatch.setattr(middleware, "limiter", healthy)
            clock.now += 10
            r = client.get("/data", headers={"x-api-key": "tenant"})
            assert r.status_code == 200
            assert r.headers["X-RateLimit-Remaining"] == "1"
            assert breaker.state is BreakerState.CLOSED
            assert _metric("rateguard_breaker_state") == 0

            client.get("/data", headers={"x-api-key": "tenant"})
            r = client.get("/data", headers={"x-api-key": "tenant"})
            assert r.status_code == 429
    finally:
        _flush_test_keys()


def test_fail_closed_returns_503_json_while_redis_is_down(monkeypatch):
    breaker, clock = _install(monkeypatch, middleware.FAIL_CLOSED, threshold=2, timeout=10)
    monkeypatch.setattr(middleware, "limiter", _limiter(DEAD_REDIS_URL))
    with TestClient(fastapi_app) as client:
        for _ in range(3):
            r = client.get("/data")
            assert r.status_code == 503
            body = r.json()
            assert body["error"] == "rate_limiter_unavailable"
            assert "unavailable" in body["detail"]
            assert r.headers["Retry-After"] == "10"
        assert breaker.state is BreakerState.OPEN
        assert body["breaker_state"] == "open"

        # A half_open trial that fails re-opens the breaker.
        clock.now += 10
        assert client.get("/data").status_code == 503
        assert breaker.state is BreakerState.OPEN


def test_open_breaker_skips_redis_entirely(monkeypatch):
    breaker, _ = _install(monkeypatch, middleware.FAIL_OPEN, threshold=1)
    breaker.record_failure()
    calls = []

    class ExplodingLimiter:
        async def allow(self, key):
            calls.append(key)
            raise AssertionError("limiter must not be called while open")

    monkeypatch.setattr(middleware, "limiter", ExplodingLimiter())
    with TestClient(fastapi_app) as client:
        r = client.get("/data")
    assert r.status_code == 200
    assert calls == []


def test_unprotected_routes_unaffected_by_open_breaker(monkeypatch):
    breaker, _ = _install(monkeypatch, middleware.FAIL_CLOSED, threshold=1)
    breaker.record_failure()
    with TestClient(fastapi_app) as client:
        assert client.get("/metrics").status_code == 200
