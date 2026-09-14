from prometheus_client import Counter, Histogram

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
