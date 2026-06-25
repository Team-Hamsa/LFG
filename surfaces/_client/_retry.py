# Exponential-backoff retry shared by REST calls and the WS reconnect loop.
# Reuses the same env knobs as the rest of the app without importing main.py.

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

RETRY_MAX_ATTEMPTS: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

T = TypeVar("T")


async def with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float,
    retryable: Callable[[Exception], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call factory(), retrying transient failures with exponential backoff.

    Retries while retryable(exc) is True and attempts remain; otherwise the
    last exception propagates. Backoff before retry k is base_delay * 2 ** (k-1).
    """
    attempt = 0
    while True:
        try:
            return await factory()
        except Exception as exc:
            attempt += 1
            if attempt >= max_attempts or not retryable(exc):
                raise
            await sleep(base_delay * (2 ** (attempt - 1)))
