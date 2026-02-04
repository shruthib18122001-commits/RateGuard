import time
from dataclasses import dataclass

@dataclass
class Bucket:
    tokens: float
    last_refill: float

class TokenBucketLimiter:
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
