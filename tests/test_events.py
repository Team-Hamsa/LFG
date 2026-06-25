# tests/test_events.py
import asyncio

from lfg_service.events import Event, InMemoryEventBus


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _evt(type_, wallet):
    return Event(type=type_, ts=1, identity=None, wallet=wallet, data={"n": 1})


def test_subscriber_receives_matching_event():
    async def body():
        bus = InMemoryEventBus()
        async with bus.subscribe(lambda e: True) as stream:
            await bus.publish(_evt("mint.completed", "rA"))
            return await asyncio.wait_for(stream.__anext__(), timeout=1)

    evt = _run(body())
    assert evt.type == "mint.completed"
    assert evt.wallet == "rA"


def test_predicate_filters_out_other_users():
    async def body():
        bus = InMemoryEventBus()
        async with bus.subscribe(lambda e: e.wallet == "rME") as stream:
            await bus.publish(_evt("mint.completed", "rOTHER"))  # filtered out
            await bus.publish(_evt("mint.completed", "rME"))  # delivered
            return await asyncio.wait_for(stream.__anext__(), timeout=1)

    evt = _run(body())
    assert evt.wallet == "rME"


def test_two_subscribers_both_receive():
    async def body():
        bus = InMemoryEventBus()
        async with bus.subscribe(lambda e: True) as s1, bus.subscribe(lambda e: True) as s2:
            await bus.publish(_evt("swap.completed", "rA"))
            e1 = await asyncio.wait_for(s1.__anext__(), timeout=1)
            e2 = await asyncio.wait_for(s2.__anext__(), timeout=1)
            return e1, e2

    e1, e2 = _run(body())
    assert e1.type == e2.type == "swap.completed"


def test_event_to_dict():
    d = _evt("mint.failed", "rA").to_dict()
    assert d == {"type": "mint.failed", "ts": 1, "identity": None, "wallet": "rA", "data": {"n": 1}}
