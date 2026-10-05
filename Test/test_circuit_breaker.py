import asyncio
import threading

import pytest
import redis

from app.circuit_breaker import BreakerState, CircuitBreaker, CircuitOpenError


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _breaker(threshold=3, timeout=10.0, transitions=None):
    clock = FakeClock()
    on_transition = (lambda f, t: transitions.append((f.value, t.value))) if transitions is not None else None
    return CircuitBreaker(threshold, timeout, clock=clock, on_transition=on_transition), clock


async def _ok():
    return "ok"


async def _fail():
    raise redis.ConnectionError("redis is down")


def test_opens_after_threshold_consecutive_failures():
    breaker, _ = _breaker(threshold=3)
    for _ in range(2):
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN


def test_success_resets_consecutive_failure_count():
    breaker, _ = _breaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count == 0
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


def test_rejects_while_open_without_calling_dependency():
    breaker, clock = _breaker(threshold=1)
    breaker.record_failure()
    calls = []

    async def tracked():
        calls.append(1)

    clock.advance(9.9)
    with pytest.raises(CircuitOpenError):
        asyncio.run(breaker.call(tracked))
    assert calls == []
    assert breaker.state is BreakerState.OPEN


def test_half_open_after_timeout_admits_exactly_one_trial():
    breaker, clock = _breaker(threshold=1, timeout=10)
    breaker.record_failure()
    clock.advance(10)
    assert breaker.allow_request() is True
    assert breaker.state is BreakerState.HALF_OPEN
    # Trial still in flight: everyone else keeps getting short-circuited.
    assert breaker.allow_request() is False


def test_half_open_success_closes():
    transitions = []
    breaker, clock = _breaker(threshold=2, timeout=10, transitions=transitions)
    for _ in range(2):
        with pytest.raises(redis.ConnectionError):
            asyncio.run(breaker.call(_fail))
    clock.advance(10)
    assert asyncio.run(breaker.call(_ok)) == "ok"
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allow_request() is True
    assert transitions == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


def test_half_open_failure_reopens_and_restarts_timeout():
    transitions = []
    breaker, clock = _breaker(threshold=1, timeout=10, transitions=transitions)
    with pytest.raises(redis.ConnectionError):
        asyncio.run(breaker.call(_fail))
    clock.advance(10)
    with pytest.raises(redis.TimeoutError):
        async def timeout():
            raise redis.TimeoutError("timed out")
        asyncio.run(breaker.call(timeout))
    assert breaker.state is BreakerState.OPEN
    # The reset timeout counts from the re-open, not the original open.
    clock.advance(5)
    assert breaker.allow_request() is False
    clock.advance(5)
    assert breaker.allow_request() is True
    assert transitions == [("closed", "open"), ("open", "half_open"), ("half_open", "open"),
                           ("open", "half_open")]


def test_non_redis_error_in_trial_releases_slot_without_judging_health():
    breaker, clock = _breaker(threshold=1)
    breaker.record_failure()
    clock.advance(10)

    async def bug():
        raise ValueError("not a Redis problem")

    with pytest.raises(ValueError):
        asyncio.run(breaker.call(bug))
    assert breaker.state is BreakerState.HALF_OPEN
    # The slot was released, so the next request can be the trial.
    assert breaker.allow_request() is True


def test_from_env_reads_thresholds(monkeypatch):
    monkeypatch.setenv("BREAKER_FAILURE_THRESHOLD", "7")
    monkeypatch.setenv("BREAKER_RESET_TIMEOUT", "2.5")
    breaker = CircuitBreaker.from_env()
    assert breaker.failure_threshold == 7
    assert breaker.reset_timeout == 2.5


def test_defaults():
    breaker = CircuitBreaker()
    assert breaker.failure_threshold == 5
    assert breaker.reset_timeout == 10.0


def test_concurrent_failures_from_many_threads_open_exactly_once():
    transitions = []
    breaker, _ = _breaker(threshold=50, transitions=transitions)
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(25):
            breaker.record_failure()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert breaker.state is BreakerState.OPEN
    assert transitions == [("closed", "open")]


def test_concurrent_half_open_trials_only_one_admitted():
    breaker, clock = _breaker(threshold=1)
    breaker.record_failure()
    clock.advance(10)
    barrier = threading.Barrier(16)
    results = []

    def worker():
        barrier.wait()
        results.append(breaker.allow_request())

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_straggler_success_does_not_close_an_open_breaker():
    """A call admitted while closed that only finishes after the breaker has
    opened must not close it: that would skip the reset timeout and the
    half_open trial entirely."""
    breaker, _ = _breaker(threshold=1)

    async def scenario():
        release = asyncio.Event()

        async def slow_ok():
            await release.wait()
            return "late"

        straggler = asyncio.create_task(breaker.call(slow_ok))
        await asyncio.sleep(0)  # let it get admitted
        with pytest.raises(redis.ConnectionError):
            await breaker.call(_fail)  # trips the breaker
        assert breaker.state is BreakerState.OPEN

        release.set()
        assert await straggler == "late"
        assert breaker.state is BreakerState.OPEN

    asyncio.run(scenario())


@pytest.mark.parametrize("straggler_fails", [False, True])
def test_straggler_outcome_does_not_decide_the_half_open_trial(straggler_fails):
    """Only the trial request's own outcome may close or re-open the breaker
    in half_open, not a slow call that was admitted back when it was closed."""
    breaker, clock = _breaker(threshold=1, timeout=10)

    async def scenario():
        release_straggler = asyncio.Event()
        release_trial = asyncio.Event()

        async def straggler_call():
            await release_straggler.wait()
            if straggler_fails:
                raise redis.ConnectionError("late failure")

        async def trial_call():
            await release_trial.wait()
            return "trial ok"

        straggler = asyncio.create_task(breaker.call(straggler_call))  # admitted while closed
        await asyncio.sleep(0)
        with pytest.raises(redis.ConnectionError):
            await breaker.call(_fail)  # opens the breaker
        clock.advance(10)
        trial = asyncio.create_task(breaker.call(trial_call))  # the half_open trial
        await asyncio.sleep(0)
        assert breaker.state is BreakerState.HALF_OPEN

        release_straggler.set()
        if straggler_fails:
            with pytest.raises(redis.ConnectionError):
                await straggler
        else:
            await straggler
        assert breaker.state is BreakerState.HALF_OPEN  # trial is still pending

        release_trial.set()
        assert await trial == "trial ok"
        assert breaker.state is BreakerState.CLOSED

    asyncio.run(scenario())
