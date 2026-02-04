from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.limiter import TokenBucketLimiter

# One limiter instance per process
limiter = TokenBucketLimiter(rate=5, capacity=10)

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Identify the client
        key = request.headers.get("x-api-key")

        if not key and request.client:
            key = request.client.host

        if not key:
            key = "anonymous"

        allowed, remaining = limiter.allow(key)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"}
            )

        response = await call_next(request)

        # Add rate-limit headers (production signal)
        response.headers["X-RateLimit-Limit"] = "10"
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
