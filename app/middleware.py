import os
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
import redis.asyncio as aioredis

from app.limiter import RedisTokenBucketLimiter
from app.metrics import RATE_LIMIT_DECISIONS, REQUEST_LATENCY

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
RATE_LIMIT_RATE = int(os.environ.get("RATE_LIMIT_RATE", "5"))
RATE_LIMIT_CAPACITY = int(os.environ.get("RATE_LIMIT_CAPACITY", "10"))

# One Redis connection pool shared by every replica's middleware instance;
# the bucket state itself lives in Redis, not in this process.
_redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

limiter = RedisTokenBucketLimiter(
    _redis_client,
    rate=RATE_LIMIT_RATE,
    capacity=RATE_LIMIT_CAPACITY,
)


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
        allowed, remaining = await limiter.allow(key)
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
