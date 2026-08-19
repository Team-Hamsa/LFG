"""The nft-number allocator must survive more than one event loop.

`_nft_number_lock` used to be a single module-level `asyncio.Lock`, which binds
permanently to the first loop that awaits it *under contention*. Any process
that then runs a second loop (the test suite does this constantly) hit "Lock is
bound to a different event loop" and every mint on that loop failed.

The contention matters: an uncontended `acquire()` never touches the loop, so a
test that allocates sequentially passes against the buggy implementation too.
Each case below forces a real waiter onto the lock before moving loops.
"""

import asyncio
import gc

from lfg_core import mint_flow


def _allocate_twice_concurrently(numbers: list[int]) -> None:
    """Two overlapping allocations on one fresh loop, then close it.

    The allocator awaits `asyncio.to_thread` while holding the lock, so the
    second call is a genuine waiter — which is what binds the lock to a loop.
    """

    async def both():
        return await asyncio.gather(
            mint_flow._allocate_nft_number(),
            mint_flow._allocate_nft_number(),
        )

    loop = asyncio.new_event_loop()
    try:
        numbers.extend(loop.run_until_complete(both()))
    finally:
        loop.close()


def test_allocate_works_across_separate_event_loops(monkeypatch):
    monkeypatch.setattr(mint_flow, "get_next_nft_number", lambda: 4242)
    monkeypatch.setattr(mint_flow, "_reserved_numbers", set())
    monkeypatch.setattr(mint_flow, "_nft_number_locks", {}, raising=False)

    numbers: list[int] = []
    _allocate_twice_concurrently(numbers)
    _allocate_twice_concurrently(numbers)

    # Reservations are shared across loops even though the locks are not.
    assert sorted(numbers) == [4242, 4243, 4244, 4245]


def test_closed_loops_are_not_retained(monkeypatch):
    """A contended Lock strongly references its loop, so the registry has to
    drop entries for closed loops itself — otherwise every loop the process
    ever ran stays alive."""
    monkeypatch.setattr(mint_flow, "get_next_nft_number", lambda: 1)
    monkeypatch.setattr(mint_flow, "_reserved_numbers", set())
    monkeypatch.setattr(mint_flow, "_nft_number_locks", {}, raising=False)

    for _ in range(3):
        _allocate_twice_concurrently([])
    gc.collect()

    # The prune runs on lookup, so the last (still-open at the time) entry is
    # all that may remain; the earlier closed loops must be gone.
    assert len(mint_flow._nft_number_locks) <= 1
