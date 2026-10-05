import logging
import math
import os
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import redis.asyncio as aioredis

from app.circuit_breaker import (
    BREAKER_FAILURES, STATE_VALUES, BreakerState, CircuitBreaker, CircuitOpenError,
)
from app.limiter import RedisTokenBucketLimiter
from app.metrics import (
    BREAKER_FALLBACKS, BREAKER_STATE, BREAKER_TRANSITIONS, RATE_LIMIT_DECISIONS,
    REQUEST_LATENCY,
)

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
RATE_LIMIT_RATE = int(os.environ.get("RATE_LIMIT_RATE", "5"))
RATE_LIMIT_CAPACITY = int(os.environ.get("RATE_LIMIT_CAPACITY", "10"))
# Kept short on purpose: a hung Redis should cost each request a fraction of
# a second, not the OS default, before the breaker can count it as a failure.
REDIS_SOCKET_TIMEOUT = float(os.environ.get("REDIS_SOCKET_TIMEOUT", "0.5"))

FAIL_OPEN = "FAIL_OPEN"
FAIL_CLOSED = "FAIL_CLOSED"
# What to do when Redis can't give us a decision (breaker open, or the call
# just failed): FAIL_OPEN lets the request through unthrottled, FAIL_CLOSED
# rejects it with 503.
FALLBACK_POLICY = os.environ.get("BREAKER_FALLBACK_POLICY", FAIL_OPEN).upper()
if FALLBACK_POLICY not in (FAIL_OPEN, FAIL_CLOSED):
    raise ValueError(
        f"BREAKER_FALLBACK_POLICY must be {FAIL_OPEN} or {FAIL_CLOSED}, got {FALLBACK_POLICY!r}"
    )

# One Redis connection pool shared by every replica's middleware instance;
# the bucket state itself lives in Redis, not in this process.
_redis_client = aioredis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_timeout=REDIS_SOCKET_TIMEOUT,
    socket_connect_timeout=REDIS_SOCKET_TIMEOUT,
)

limiter = RedisTokenBucketLimiter(
    _redis_client,
    rate=RATE_LIMIT_RATE,
    capacity=RATE_LIMIT_CAPACITY,
)



def _record_breaker_transition(from_state: BreakerState, to_state: BreakerState):
    BREAKER_STATE.set(STATE_VALUES[to_state])
    BREAKER_TRANSITIONS.labels(from_state=from_state.value, to_state=to_state.value).inc()
    logger.warning("Redis circuit breaker %s -> %s", from_state.value, to_state.value)


# Guards every Redis call the middleware makes.
breaker = CircuitBreaker.from_env(on_transition=_record_breaker_transition)
BREAKER_STATE.set(STATE_VALUES[breaker.state])


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Don't let scraping /metrics, or using the admin API/page, consume a
        # client's rate-limit budget or pollute the latency histogram.
        if request.url.path == "/metrics" or request.url.path.startswith("/admin"):
            return await call_next(request)

        # Identify the client
        key = request.headers.get("x-api-key")

        if not key and request.client:
            key = request.client.host

        if not key:
            key = "anonymous"

        start = time.perf_counter()
        try:
            allowed, remaining = await breaker.call(limiter.allow, key)
        except CircuitOpenError:
            REQUEST_LATENCY.labels(path=request.url.path).observe(time.perf_counter() - start)
            return await self._fallback(request, call_next)
        except BREAKER_FAILURES as exc:
            REQUEST_LATENCY.labels(path=request.url.path).observe(time.perf_counter() - start)
            logger.warning("Redis rate-limit check failed (%s): %s", type(exc).__name__, exc)
            return await self._fallback(request, call_next)
        REQUEST_LATENCY.labels(path=request.url.path).observe(time.perf_counter() - start)

        if not allowed:
            RATE_LIMIT_DECISIONS.labels(decision="denied").inc()
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"}
            )

        RATE_LIMIT_DECISIONS.labels(decision="allowed").inc()
        response = await call_next(request)

        # Add rate-limit headers (production signal)
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_CAPACITY)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response

    async def _fallback(self, request: Request, call_next):
        BREAKER_FALLBACKS.labels(policy=FALLBACK_POLICY).inc()
        if FALLBACK_POLICY == FAIL_CLOSED:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Rate limiter unavailable; request rejected (fail-closed policy)",
                    "error": "rate_limiter_unavailable",
                    "breaker_state": breaker.state.value,
                },
                headers={"Retry-After": str(math.ceil(breaker.reset_timeout))},
            )

        # FAIL_OPEN: serve the request without a limit. No X-RateLimit-*
        # headers, since no limit was actually checked.
        response = await call_next(request)
        response.headers["X-RateGuard-Fallback"] = "fail-open"
        return response
