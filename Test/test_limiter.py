import asyncio
import json
import time

import redis
import redis.asyncio as aioredis
from fastapi.testclient import TestClient

from app.limiter import TokenBucketLimiter, RedisTokenBucketLimiter

# Separate Redis DB from the app's default (db 0) so tests never collide
# with a locally running instance of the app itself.
REDIS_TEST_URL = "redis://localhost:6379/1"


def test_in_memory_token_bucket_basic_flow():
    limiter = TokenBucketLimiter(rate=1, capacity=2)
    key = "user-123"

    allowed, remaining = limiter.allow(key)
    assert allowed is True
    assert remaining == 1

    allowed, remaining = limiter.allow(key)
    assert allowed is True
    assert remaining == 0

    allowed, remaining = limiter.allow(key)
    assert allowed is False

    # wait for refill
    time.sleep(1.1)

    allowed, remaining = limiter.allow(key)
    assert allowed is True


def _redis_client():
    return aioredis.from_url(REDIS_TEST_URL, decode_responses=True)


async def _flush(client, prefix):
    keys = [k async for k in client.scan_iter(f"{prefix}*")]
    if keys:
        await client.delete(*keys)


def test_redis_token_bucket_allows_then_denies():
    async def run():
        client = _redis_client()
        prefix = "test:allow-deny:"
        limiter = RedisTokenBucketLimiter(client, rate=1, capacity=2, key_prefix=prefix)
        await _flush(client, prefix)
        try:
            allowed, remaining = await limiter.allow("user-1")
            assert allowed is True
            assert remaining == 1

            allowed, remaining = await limiter.allow("user-1")
            assert allowed is True
            assert remaining == 0

            allowed, remaining = await limiter.allow("user-1")
            assert allowed is False
            assert remaining == 0
        finally:
            await _flush(client, prefix)
            await client.aclose()

    asyncio.run(run())


def test_redis_token_bucket_refills_over_time():
    async def run():
        client = _redis_client()
        prefix = "test:refill:"
        limiter = RedisTokenBucketLimiter(client, rate=2, capacity=2, key_prefix=prefix)
        await _flush(client, prefix)
        try:
            await limiter.allow("user-2")
            await limiter.allow("user-2")
            allowed, _ = await limiter.allow("user-2")
            assert allowed is False

            await asyncio.sleep(0.6)  # ~2 tokens/sec => at least 1 token back

            allowed, remaining = await limiter.allow("user-2")
            assert allowed is True
            assert remaining >= 0
        finally:
            await _flush(client, prefix)
            await client.aclose()

    asyncio.run(run())


def test_redis_token_bucket_isolates_keys():
    async def run():
        client = _redis_client()
        prefix = "test:isolate:"
        limiter = RedisTokenBucketLimiter(client, rate=1, capacity=1, key_prefix=prefix)
        await _flush(client, prefix)
        try:
            allowed_a, _ = await limiter.allow("tenant-a")
            allowed_b, _ = await limiter.allow("tenant-b")
            assert allowed_a is True
            assert allowed_b is True  # separate buckets per key

            denied_a, _ = await limiter.allow("tenant-a")
            assert denied_a is False
        finally:
            await _flush(client, prefix)
            await client.aclose()

    asyncio.run(run())


def test_redis_token_bucket_override_takes_precedence_over_default():
    """A per-key override in the rateguard:overrides hash must win over the
    limiter's configured default rate/capacity, and must not affect other
    keys that share the same overrides hash but have no entry of their own."""

    async def run():
        client = _redis_client()
        prefix = "test:override:"
        overrides_key = "test:override:overrides"
        limiter = RedisTokenBucketLimiter(
            client, rate=5, capacity=10, key_prefix=prefix, overrides_key=overrides_key,
        )
        await _flush(client, prefix)
        await client.delete(overrides_key)
        try:
            # No override yet: behaves exactly like the global default.
            allowed, remaining = await limiter.allow("tenant-default")
            assert allowed is True
            assert remaining == 9

            # Install a much tighter override for a different key.
            await client.hset(
                overrides_key, "tenant-override",
                json.dumps({"rate": 0, "capacity": 1}),
            )

            allowed, remaining = await limiter.allow("tenant-override")
            assert allowed is True
            assert remaining == 0

            denied, remaining = await limiter.allow("tenant-override")
            assert denied is False
            assert remaining == 0

            # A key with no override entry, even though the hash now has
            # entries in it, must still see the unchanged global default.
            allowed, remaining = await limiter.allow("tenant-untouched")
            assert allowed is True
            assert remaining == 9
        finally:
            await _flush(client, prefix)
            await client.delete(overrides_key)
            await client.aclose()

    asyncio.run(run())


def test_admin_limits_requires_auth_and_round_trips_override(monkeypatch):
    """/admin/limits/{client_key} must reject missing/wrong X-Admin-Key with
    401, and correctly create/list/delete overrides when authorized."""

    import app.admin as admin_module
    from app.main import app as fastapi_app

    prefix = "test:admin:"
    overrides_key = "test:admin:overrides"
    admin_key = "test-admin-key"

    # Plain sync client for setup/teardown only, so it never shares an event
    # loop with the async client the ASGI app uses via TestClient.
    setup_client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    setup_client.delete(overrides_key)
    for k in setup_client.scan_iter(f"{prefix}*"):
        setup_client.delete(k)

    test_limiter = RedisTokenBucketLimiter(
        aioredis.from_url(REDIS_TEST_URL, decode_responses=True),
        rate=5, capacity=10, key_prefix=prefix, overrides_key=overrides_key,
    )
    monkeypatch.setattr(admin_module, "limiter", test_limiter)
    monkeypatch.setenv("ADMIN_API_KEY", admin_key)

    try:
        with TestClient(fastapi_app) as client:
            # No header at all.
            r = client.get("/admin/limits/tenant-a")
            assert r.status_code == 401

            # Wrong key.
            r = client.get("/admin/limits/tenant-a", headers={"X-Admin-Key": "wrong"})
            assert r.status_code == 401

            # Correct key, nothing set yet.
            r = client.get("/admin/limits/tenant-a", headers={"X-Admin-Key": admin_key})
            assert r.status_code == 404

            # Create an override.
            r = client.post(
                "/admin/limits/tenant-a",
                headers={"X-Admin-Key": admin_key},
                json={"rate": 1, "capacity": 2},
            )
            assert r.status_code == 200
            assert r.json() == {"client_key": "tenant-a", "rate": 1, "capacity": 2}

            # It shows up in the full listing.
            r = client.get("/admin/limits", headers={"X-Admin-Key": admin_key})
            assert r.status_code == 200
            assert r.json() == {"tenant-a": {"rate": 1, "capacity": 2}}

            # Deleting without auth must not remove it.
            r = client.delete("/admin/limits/tenant-a")
            assert r.status_code == 401
            r = client.get("/admin/limits/tenant-a", headers={"X-Admin-Key": admin_key})
            assert r.status_code == 200

            # Delete with auth.
            r = client.delete("/admin/limits/tenant-a", headers={"X-Admin-Key": admin_key})
            assert r.status_code == 204

            r = client.get("/admin/limits/tenant-a", headers={"X-Admin-Key": admin_key})
            assert r.status_code == 404
    finally:
        setup_client.delete(overrides_key)
        for k in setup_client.scan_iter(f"{prefix}*"):
            setup_client.delete(k)
        setup_client.close()


def test_redis_token_bucket_concurrent_requests_share_one_bucket():
    """The correctness property the in-memory limiter can't offer: many
    concurrent callers hitting the SAME key never over-admit past capacity,
    because each check-and-decrement is one atomic Redis operation."""

    async def run():
        client = _redis_client()
        prefix = "test:concurrent:"
        limiter = RedisTokenBucketLimiter(client, rate=0, capacity=10, key_prefix=prefix)
        await _flush(client, prefix)
        try:
            results = await asyncio.gather(
                *[limiter.allow("shared-tenant") for _ in range(25)]
            )
            allowed_count = sum(1 for allowed, _ in results if allowed)
            assert allowed_count == 10
        finally:
            await _flush(client, prefix)
            await client.aclose()

    asyncio.run(run())
