import time
from dataclasses import dataclass


@dataclass
class Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    """Original single-process, in-memory token bucket.

    Correct within one process, but each replica keeps its own buckets, so a
    client spread across multiple app instances effectively gets
    (capacity * number_of_replicas) tokens instead of one shared limit.
    Kept here as a lightweight fallback for local dev/tests that don't want
    to spin up Redis.
    """

    def __init__(self, rate: int, capacity: int):
        self.rate = rate              # tokens per second
        self.capacity = capacity
        self.buckets = {}

    def allow(self, key: str, cost: int = 1):
        now = time.time()

        if key not in self.buckets:
            self.buckets[key] = Bucket(self.capacity, now)

        bucket = self.buckets[key]

        elapsed = now - bucket.last_refill
        refill = elapsed * self.rate
        bucket.tokens = min(self.capacity, bucket.tokens + refill)
        bucket.last_refill = now

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True, int(bucket.tokens)

        return False, int(bucket.tokens)


# Refill + consume happens atomically in a single EVAL so concurrent requests
# for the same key -- whether from one process or many replicas talking to
# the same Redis -- can never both read stale tokens and both succeed.
#
# Per-key overrides are looked up inside this same script (rather than in
# Python beforehand) so the override check and the bucket read/write stay
# one atomic operation -- a Python-side check-then-EVAL would let a
# concurrent admin update race a concurrent request.
#
# KEYS[1]  = bucket key
# KEYS[2]  = overrides hash key (rateguard:overrides)
# ARGV[1]  = default rate (tokens/sec)
# ARGV[2]  = default capacity
# ARGV[3]  = now (float seconds)
# ARGV[4]  = cost
# ARGV[5]  = idle ttl (seconds) so buckets for clients who stop calling
#            eventually expire instead of accumulating in Redis forever
# ARGV[6]  = client key (hash field used to look up a per-key override)
_TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local overrides_key = KEYS[2]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local client_key = ARGV[6]

local override_json = redis.call("HGET", overrides_key, client_key)
if override_json then
    local ok, override = pcall(cjson.decode, override_json)
    if ok and type(override) == "table" then
        if override.rate ~= nil then
            rate = tonumber(override.rate)
        end
        if override.capacity ~= nil then
            capacity = tonumber(override.capacity)
        end
    end
end

local bucket = redis.call("HMGET", key, "tokens", "last_refill")
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = now - last_refill
if elapsed < 0 then
    elapsed = 0
end
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
end

redis.call("HMSET", key, "tokens", tostring(tokens), "last_refill", tostring(now))
redis.call("EXPIRE", key, ttl)

return {allowed, tostring(tokens)}
"""


class RedisTokenBucketLimiter:
    """Distributed token-bucket limiter backed by Redis.

    Same refill/consume semantics as TokenBucketLimiter, but the
    read-refill-check-decrement sequence runs as one atomic Lua script
    inside Redis instead of as separate Python steps against a local dict.
    That's what makes it safe across multiple app processes/pods sharing
    the same Redis: two concurrent requests for the same key can't both
    observe the same stale token count and both be allowed through.
    """

    def __init__(self, redis_client, rate: int, capacity: int,
                 ttl_seconds: int = 3600, key_prefix: str = "rateguard:bucket:",
                 overrides_key: str = "rateguard:overrides"):
        self.redis = redis_client
        self.rate = rate
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self.overrides_key = overrides_key
        self._script = self.redis.register_script(_TOKEN_BUCKET_SCRIPT)

    async def allow(self, key: str, cost: int = 1):
        now = time.time()
        redis_key = f"{self.key_prefix}{key}"
        allowed, tokens = await self._script(
            keys=[redis_key, self.overrides_key],
            args=[self.rate, self.capacity, now, cost, self.ttl_seconds, key],
        )
        return bool(int(allowed)), int(float(tokens))
