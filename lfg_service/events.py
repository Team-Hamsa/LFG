# lfg_service/events.py
# In-process pub/sub event bus. The EventBus protocol is the seam a future
# Redis Streams implementation drops into (spec §6); these semantics are the
# contract that implementation must also satisfy.

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Event:
    type: str
    ts: int
    identity: dict[str, Any] | None
    wallet: str | None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "ts": self.ts,
            "identity": self.identity,
            "wallet": self.wallet,
            "data": self.data,
        }


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    def subscribe(self, predicate: Callable[[Event], bool]) -> Any: ...


class InMemoryEventBus:
    def __init__(self) -> None:
        self._subscribers: set[tuple[Callable[[Event], bool], asyncio.Queue[Event]]] = set()

    async def publish(self, event: Event) -> None:
        for predicate, queue in list(self._subscribers):
            try:
                if predicate(event):
                    queue.put_nowait(event)
            except Exception:
                # Each subscriber's queue is unbounded (asyncio.Queue maxsize=0),
                # so put_nowait cannot raise QueueFull here; this guard isolates a
                # misbehaving predicate so one bad subscriber can't break fan-out
                # to the others. If a bounded queue is ever introduced, handle
                # QueueFull explicitly (log/drop-with-signal) instead of swallowing.
                pass

    @asynccontextmanager
    async def subscribe(
        self, predicate: Callable[[Event], bool]
    ) -> AsyncIterator[AsyncIterator[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        entry = (predicate, queue)
        self._subscribers.add(entry)

        async def _stream() -> AsyncIterator[Event]:
            while True:
                yield await queue.get()

        try:
            yield _stream()
        finally:
            self._subscribers.discard(entry)
