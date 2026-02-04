import time
from app.limiter import TokenBucketLimiter

def test_token_bucket_basic_flow():
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
