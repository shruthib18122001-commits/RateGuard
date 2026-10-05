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
- Circuit breaker around Redis with a configurable fail-open / fail-closed
  fallback, so a Redis outage degrades gracefully instead of hanging requests
- Distributed tracing with OpenTelemetry, exported to a pre-provisioned
  Grafana Tempo
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
| `REDIS_SOCKET_TIMEOUT` | `0.5`                        | Redis connect/read timeout (seconds) |
| `BREAKER_FAILURE_THRESHOLD` | `5`                     | Consecutive Redis failures that open the breaker |
| `BREAKER_RESET_TIMEOUT` | `10`                        | Seconds the breaker stays open before a half-open trial |
| `BREAKER_FALLBACK_POLICY` | `FAIL_OPEN`               | `FAIL_OPEN` (serve unthrottled) or `FAIL_CLOSED` (503) while Redis is unavailable |
| `OTEL_TRACING_ENABLED` | `false`                      | `true` to enable OpenTelemetry tracing |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC endpoint spans are exported to |

See [Circuit Breaker](#circuit-breaker) and
[Distributed Tracing](#distributed-tracing) for details.

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
Starts the app (port 8000), Redis, Prometheus, Grafana and Tempo together.

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
- `rateguard_breaker_state` — 0=closed, 1=open, 2=half_open
- `rateguard_breaker_transitions_total{from_state, to_state}`
- `rateguard_breaker_fallbacks_total{policy="FAIL_OPEN"|"FAIL_CLOSED"}`

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
- Redis circuit breaker state over time (closed / open / half_open)
- Breaker transitions and fallback-handled requests per minute

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

## Circuit Breaker
If Redis goes down or hangs, every rate-limit check would otherwise wait
for a timeout and then fail. A circuit breaker
([`app/circuit_breaker.py`](app/circuit_breaker.py)) wraps every Redis
call the middleware makes, so a Redis outage costs a few failed calls and
nothing more.

### Design
```
CLOSED ──(N consecutive failures)──▶ OPEN ──(reset timeout)──▶ HALF_OPEN
  ▲                                   ▲                            │
  │                                   └──────(trial fails)─────────┤
  └──────────────────────(trial succeeds)──────────────────────────┘
```
- **closed**: calls go through. Any success resets the failure count. After
  `BREAKER_FAILURE_THRESHOLD` *consecutive* failures, the breaker opens.
- **open**: calls are short-circuited straight to the fallback policy. There
  is no Redis round trip and no socket timeout to wait out.
- **half_open**: after `BREAKER_RESET_TIMEOUT` seconds, the next request
  becomes the single trial request while everyone else stays on the
  fallback. If the trial succeeds the breaker closes. If it fails the
  breaker re-opens and the timeout starts again.
- These count as failures: any `redis.RedisError` (connection errors,
  timeouts, …), `asyncio.TimeoutError` and `OSError`. Other exceptions only
  release the half-open trial slot and leave the breaker's state unchanged.
- The Redis client uses a short connect/read timeout
  (`REDIS_SOCKET_TIMEOUT`, default 0.5s). A hung Redis therefore costs at
  most that long per request until the breaker opens.
- The breaker is thread-safe: a lock guards all of its state. The move from
  open to half_open happens on the first check after the timeout, so no
  background timer is needed. Each app process has its own breaker.

### Fallback policy
`BREAKER_FALLBACK_POLICY` decides what happens when Redis can't give an
answer, either because the breaker is open or because the call just failed:

| Policy | Behaviour |
|---|---|
| `FAIL_OPEN` *(default)* | Requests are served **without rate limiting**. The response carries `X-RateGuard-Fallback: fail-open` and no `X-RateLimit-*` headers. |
| `FAIL_CLOSED` | Requests are rejected with **503** and a `Retry-After` header set to the reset timeout:<br>`{"detail": "Rate limiter unavailable; request rejected (fail-closed policy)", "error": "rate_limiter_unavailable", "breaker_state": "open"}` |

Fail-open favours availability: a Redis outage doesn't take the API down
with it. Fail-closed favours protection: use it when the limit guards
something that must never be overloaded.

`/metrics` and `/admin*` don't go through the limiter, so the breaker
doesn't affect them. The admin API itself talks to Redis directly and
returns errors while Redis is down.

### Metrics
`rateguard_breaker_state` (gauge),
`rateguard_breaker_transitions_total{from_state,to_state}` and
`rateguard_breaker_fallbacks_total{policy}`. Two panels on the Grafana
dashboard show them: **Redis Circuit Breaker State** and **Breaker
Transitions & Fallbacks**. Every transition is also logged at WARNING
level.

## Distributed Tracing
RateGuard uses OpenTelemetry ([`app/tracing.py`](app/tracing.py)) to
trace each request. A trace looks like this:

```
GET /data                              (FastAPI auto-instrumentation)
└── rateguard.rate_limit_decision      (custom span)
    └── EVALSHA                        (Redis auto-instrumentation)
```

The `rateguard.rate_limit_decision` span carries these attributes:

| Attribute | Meaning |
|---|---|
| `client_key` | The `X-API-Key` / client IP the bucket is keyed on |
| `allowed` | Whether the request was let through (`true` for fail-open fallbacks) |
| `tokens_remaining` | Tokens left in the bucket (absent when Redis was unavailable) |
| `breaker_state` | `closed` / `open` / `half_open` after the decision |
| `fallback_policy` | Set only when the fallback policy handled the request |

If the Redis call fails, the span also records the exception and has
error status.

- `service.name` is `rateguard`. Spans are batched and exported over OTLP
  gRPC to `OTEL_EXPORTER_OTLP_ENDPOINT`.
- Tracing is **off by default** (`OTEL_TRACING_ENABLED=true` turns it on).
  When it's off, the custom span is a no-op from the OpenTelemetry API.
- `/metrics` is excluded from tracing, so Prometheus scrapes don't bury
  the interesting traces.
- **Tempo being down never breaks the app.** Export runs on a background
  thread, so an unreachable endpoint only logs warnings and drops spans.
  Any error during tracing setup is logged and the app runs untraced.
- `client_key` is recorded verbatim. If API keys are secrets in your
  deployment, hash them before putting them on spans.

In Docker Compose, tracing is enabled and points at the `tempo` service.
Grafana Tempo (single binary, local storage, config in
`monitoring/tempo/tempo.yaml`) is provisioned as a Grafana data source next
to Prometheus. Tempo's OTLP port `4317` is also published, so a
`uvicorn` running on the host can export to it with
`OTEL_TRACING_ENABLED=true`.

## Demo: Redis outage end to end
```bash
# 1. Start everything (app, Redis, Prometheus, Grafana, Tempo)
docker compose up -d --build

# 2. Generate steady load in a second terminal
locust -f locustfile.py --host http://localhost:8000 --headless -u 20 -r 5 -t 5m

# 3. Open the dashboard: http://localhost:3000/d/rateguard-overview
#    The breaker panel shows "closed" and requests are a mix of allowed and denied.

# 4. Kill Redis
docker compose stop redis
```
- Within one scrape interval (5s), **Redis Circuit Breaker State** turns
  red ("open"). **Breaker Transitions & Fallbacks** shows a
  `closed → open` bar, then a steady `fallback (FAIL_OPEN)` rate. Locust
  keeps getting 200s, because requests are served unthrottled.
- Every 10s you'll see `open → half_open → open` as the trial request
  fails against the dead Redis.
- `curl -i localhost:8000/data` returns `X-RateGuard-Fallback: fail-open`.

```bash
# 5. Bring Redis back
docker compose start redis
```
- Within about 10s, the next trial succeeds. You'll see `half_open → closed`,
  the panel goes back to green, and 429s come back.

**Inspect traces in Tempo:** in Grafana, go to **Explore → Tempo** and
choose **Search**, or use **TraceQL** with one of these queries:
- `{resource.service.name="rateguard"}` for all traces
- `{span.breaker_state="open"}` for requests served while the breaker was
  open
- `{span.allowed=false}` for rate-limited (429) requests
- `{status=error}` for decisions where the Redis call itself failed

Open a trace to see the decision span and its attributes, nested under the
HTTP span. While Redis is up, the `EVALSHA` span sits under the decision
span.

**Try fail-closed:** set `BREAKER_FALLBACK_POLICY: FAIL_CLOSED` under
the `app` service in `docker-compose.yml`, then run
`docker compose up -d app` and repeat step 4. Requests now get
`503 {"error": "rate_limiter_unavailable", ...}`, and the fallback series
shows `FAIL_CLOSED`.
