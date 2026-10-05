import asyncio
import enum
import os
import threading
import time

from redis.exceptions import RedisError


class BreakerState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# Numeric encoding used by the rateguard_breaker_state gauge.
STATE_VALUES = {
    BreakerState.CLOSED: 0,
    BreakerState.OPEN: 1,
    BreakerState.HALF_OPEN: 2,
}

# What counts as "Redis is unhealthy". asyncio.TimeoutError/OSError cover
# timeouts and socket errors that surface outside redis-py's own exception
# hierarchy (e.g. an asyncio.wait_for around a call).
BREAKER_FAILURES = (RedisError, asyncio.TimeoutError, OSError)


class CircuitOpenError(Exception):
    """Raised by CircuitBreaker.call() when the call was short-circuited
    without ever touching the protected dependency."""


class CircuitBreaker:
    """Thread-safe closed / open / half_open circuit breaker.

    - closed:    calls go through; `failure_threshold` *consecutive* failures
                 trip the breaker open.
    - open:      calls are rejected immediately (no Redis round trip, no
                 socket timeout to wait out) until `reset_timeout` seconds
                 have passed since it opened.
    - half_open: exactly one trial call is let through. Success closes the
                 breaker; failure re-opens it and restarts the timeout.

    The open -> half_open move is lazy (it happens on the first
    allow_request() after the timeout) so no background timer is needed.
    A lock guards all state so it's safe to share across threads as well as
    across concurrent asyncio tasks.
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 10.0,
                 clock=time.monotonic, on_transition=None):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout < 0:
            raise ValueError("reset_timeout must be >= 0")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        # Called as on_transition(from_state, to_state) while holding the
        # lock, so observers see transitions in the order they happened.
        self._on_transition = on_transition
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        # Bumped on every state change. A call remembers the generation it
        # was admitted under, so a slow call that finishes after the breaker
        # has since opened (or re-opened) can't close it or re-open it, and
        # only the half_open trial's own outcome decides that state.
        self._generation = 0

    @classmethod
    def from_env(cls, **kwargs):
        return cls(
            failure_threshold=int(os.environ.get("BREAKER_FAILURE_THRESHOLD", "5")),
            reset_timeout=float(os.environ.get("BREAKER_RESET_TIMEOUT", "10")),
            **kwargs,
        )

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def _transition(self, to_state: BreakerState):
        from_state = self._state
        if from_state is to_state:
            return
        self._state = to_state
        self._generation += 1
        if to_state is BreakerState.OPEN:
            self._opened_at = self._clock()
        if to_state is not BreakerState.HALF_OPEN:
            self._trial_in_flight = False
        if self._on_transition is not None:
            self._on_transition(from_state, to_state)

    def allow_request(self) -> bool:
        """Return True if the caller may hit the dependency now. A True in
        half_open claims the single trial slot; the caller must then report
        the outcome via record_success()/record_failure()/release()."""
        return self._admit() is not None

    def _admit(self):
        """Like allow_request(), but returns the generation the caller was
        admitted under (to pass back with its outcome), or None if rejected."""
        with self._lock:
            if self._state is BreakerState.CLOSED:
                return self._generation
            if self._state is BreakerState.OPEN:
                if self._clock() - self._opened_at < self.reset_timeout:
                    return None
                self._transition(BreakerState.HALF_OPEN)
            # half_open: only one trial at a time.
            if self._trial_in_flight:
                return None
            self._trial_in_flight = True
            return self._generation

    def record_success(self, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return  # admitted under an earlier state; says nothing about now
            if self._state is BreakerState.OPEN:
                # Only the half_open trial may close the breaker, never a
                # straggler that finished after it opened.
                return
            self._failures = 0
            self._transition(BreakerState.CLOSED)

    def record_failure(self, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            if self._state is BreakerState.HALF_OPEN:
                self._transition(BreakerState.OPEN)
                return
            if self._state is BreakerState.OPEN:
                # Late failure from a call admitted before the breaker
                # opened; already open, nothing to do.
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._transition(BreakerState.OPEN)

    def release(self, generation=None):
        """Give back a half_open trial slot without judging Redis health,
        e.g. when the trial was cancelled or failed for an unrelated reason.
        Without this a cancelled trial would wedge the breaker in half_open."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return  # not the trial's slot; leave the real trial alone
            self._trial_in_flight = False

    async def call(self, func, *args, **kwargs):
        """Await func(*args, **kwargs) under the breaker.

        Raises CircuitOpenError if short-circuited; re-raises the original
        exception (after recording it) if the call itself fails."""
        generation = self._admit()
        if generation is None:
            raise CircuitOpenError(f"circuit breaker is {self.state.value}")
        try:
            result = await func(*args, **kwargs)
        except BREAKER_FAILURES:
            self.record_failure(generation)
            raise
        except BaseException:
            self.release(generation)
            raise
        self.record_success(generation)
        return result
