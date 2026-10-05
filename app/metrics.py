from prometheus_client import Counter, Gauge, Histogram

# Label sets are kept deliberately small and bounded:
#  - "decision" only ever takes the values allowed/denied
#  - "path" only takes the app's small, fixed set of real routes
# Neither is derived from the client key/IP, so cardinality can't grow with
# traffic or with the number of distinct clients hitting the API.
RATE_LIMIT_DECISIONS = Counter(
    "rateguard_rate_limit_decisions_total",
    "Count of rate limit decisions made by the middleware.",
    ["decision"],
)

REQUEST_LATENCY = Histogram(
    "rateguard_request_duration_seconds",
    "End-to-end request latency as observed by the rate-limiting middleware.",
    ["path"],
)

# Circuit breaker around Redis. Labels are breaker states / fallback
# policies only -- each a small fixed set.
BREAKER_STATE = Gauge(
    "rateguard_breaker_state",
    "Current Redis circuit breaker state (0=closed, 1=open, 2=half_open).",
)

BREAKER_TRANSITIONS = Counter(
    "rateguard_breaker_transitions_total",
    "Count of Redis circuit breaker state transitions.",
    ["from_state", "to_state"],
)

BREAKER_FALLBACKS = Counter(
    "rateguard_breaker_fallbacks_total",
    "Requests handled by the fallback policy because Redis was unavailable.",
    ["policy"],
)
