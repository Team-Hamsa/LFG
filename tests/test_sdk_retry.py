import asyncio

import pytest

from surfaces._client._retry import with_retry


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _noop_sleep(_delay: float) -> None:
    return None


def test_returns_on_first_success():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "ok"

    result = _run(
        with_retry(factory, max_attempts=5, base_delay=1.0, retryable=lambda e: True, sleep=_noop_sleep)
    )
    assert result == "ok"
    assert calls["n"] == 1


def test_retries_then_succeeds():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    result = _run(
        with_retry(factory, max_attempts=5, base_delay=1.0, retryable=lambda e: True, sleep=_noop_sleep)
    )
    assert result == "ok"
    assert calls["n"] == 3


def test_does_not_retry_when_not_retryable():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise ValueError("deterministic")

    with pytest.raises(ValueError):
        _run(
            with_retry(
                factory, max_attempts=5, base_delay=1.0, retryable=lambda e: False, sleep=_noop_sleep
            )
        )
    assert calls["n"] == 1


def test_raises_last_error_after_exhausting_attempts():
    calls = {"n": 0}
    delays: list[float] = []

    async def factory():
        calls["n"] += 1
        raise RuntimeError(f"fail-{calls['n']}")

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(RuntimeError, match="fail-3"):
        _run(
            with_retry(
                factory, max_attempts=3, base_delay=1.0, retryable=lambda e: True, sleep=record_sleep
            )
        )
    assert calls["n"] == 3
    assert delays == [1.0, 2.0]  # backoff between the 3 attempts: 1*2^0, 1*2^1
