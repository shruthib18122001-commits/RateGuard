# RateGuard – Distributed Rate-Limited API Platform

RateGuard is a backend infrastructure service that enforces per-client
API rate limits using the **token bucket algorithm**. It is designed
as middleware to provide consistent, low-latency throttling across
all endpoints.

## Features
- Token bucket rate limiting with burst support
- Redis-backed, atomic bucket updates shared across every app replica
- Middleware-based enforcement in FastAPI
- HTTP 429 responses for quota exhaustion
- Rate limit metadata via response headers
- Prometheus metrics (`/metrics`) for allow/deny counts and request latency
- Pre-provisioned Prometheus + Grafana stack for visualizing those metrics
- Per-client rate limit overrides, managed via an admin API/UI (`/admin`)
- Environment-agnostic, production-style design

## Architecture
Client requests are intercepted by a FastAPI middleware layer, which
evaluates rate limits before forwarding requests to application handlers.
The bucket for each client key lives in Redis, not in process memory, so
every replica of the app checks and updates the *same* bucket: a client
gets one consistent limit no matter which pod handles the request.

Each check-refill-consume happens inside a single Redis Lua script (`EVAL`),
so it's atomic even when many requests for the same key arrive concurrently
across replicas.

## Design Decisions
- **Token bucket** chosen over fixed window to allow controlled bursts
- **Middleware enforcement** avoids duplicated logic across endpoints
- **Redis-backed atomic updates** so rate limits hold correctly across
  horizontally scaled deployments, not just within one process
- The original in-memory limiter (`TokenBucketLimiter`) is kept as a
  lightweight fallback for local dev/tests that don't need Redis
- **Prometheus metrics** use only bounded labels (decision, route) — never
  the client key/IP — so cardinality can't grow with traffic

## Configuration
Environment variables (all optional, defaults shown):

| Variable              | Default                      | Meaning                          |
|------------------------|-------------------------------|-----------------------------------|
| `REDIS_URL`            | `redis://localhost:6379/0`   | Redis connection used for buckets |
| `RATE_LIMIT_RATE`      | `5`                          | Default tokens refilled per second |
| `RATE_LIMIT_CAPACITY`  | `10`                         | Default bucket capacity (max burst) |
| `ADMIN_API_KEY`        | *(unset — admin API always 401s)* | Required value of the `X-Admin-Key` header for the admin API |

## Running locally
```bash
pip install -r requirements.txt

# needs a Redis instance reachable at REDIS_URL
redis-server &

uvicorn app.main:app --reload
```

## Running with Docker Compose
```bash
docker compose up --build
```
Starts the app (port 8000), Redis, Prometheus, and Grafana together.

## Tests
```bash
pytest
```
The Redis-backed tests run against a real local Redis instance (`db 1`,
flushed before/after each test) rather than a mock, so they exercise the
actual Lua script.

## Load testing
```bash
locust -f locustfile.py --host http://localhost:8000
```
Or headless: `locust -f locustfile.py --host http://localhost:8000 --headless -u 20 -r 5 -t 30s`

## Metrics
`GET /metrics` exposes Prometheus text-format metrics:
- `rateguard_rate_limit_decisions_total{decision="allowed"|"denied"}`
- `rateguard_request_duration_seconds{path="/health"|"/data"}`

## Dashboards
```bash
docker compose up
```
Then open Grafana at [http://localhost:3000](http://localhost:3000) — the
"RateGuard Overview" dashboard is already loaded, with Prometheus wired up
as its data source. Nothing to click through: both are provisioned on
container startup from `monitoring/grafana/provisioning/`. The dashboard
shows:
- Allow vs deny rate over time
- Total request rate
- p50/p95/p99 request latency (`histogram_quantile` over
  `rateguard_request_duration_seconds`)

Prometheus itself is reachable at [http://localhost:9090](http://localhost:9090)
and scrapes the app's `/metrics` every 5 seconds (config in
`monitoring/prometheus/prometheus.yml`).

## Per-client rate limit overrides
Global limits come from `RATE_LIMIT_RATE`/`RATE_LIMIT_CAPACITY`, but any
individual client key (the same `X-API-Key`/IP value used by the
middleware) can be given its own rate/capacity. Overrides live in a Redis
hash (`rateguard:overrides`) and are read inside the same atomic Lua
script the limiter already uses, so applying one never introduces a race
between the override lookup and the token-bucket check.

Manage overrides at `/admin` (a minimal HTML/JS page — no build step) or
directly via the API, both requiring an `X-Admin-Key` header matching
`ADMIN_API_KEY`:

```bash
# Set/update an override
curl -X POST http://localhost:8000/admin/limits/some-client-key \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"rate": 1, "capacity": 5}'

# List all overrides
curl http://localhost:8000/admin/limits -H "X-Admin-Key: $ADMIN_API_KEY"

# Remove an override (client falls back to the global default)
curl -X DELETE http://localhost:8000/admin/limits/some-client-key \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```
