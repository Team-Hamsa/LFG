"""The nft-number allocator must survive more than one event loop.

`_nft_number_lock` used to be a module-level `asyncio.Lock`, which binds
permanently to the first loop that awaits it. Any process that runs a second
loop (the test suite does this constantly) then hit "Lock is bound to a
different event loop" and every mint on that loop failed.
"""

import asyncio

from lfg_core import mint_flow


def _allocate_on_a_fresh_loop() -> int:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(mint_flow._allocate_nft_number())
    finally:
        loop.close()


def test_allocate_works_across_separate_event_loops(monkeypatch):
    monkeypatch.setattr(mint_flow, "get_next_nft_number", lambda: 4242)
    monkeypatch.setattr(mint_flow, "_reserved_numbers", set())

    first = _allocate_on_a_fresh_loop()
    second = _allocate_on_a_fresh_loop()

    assert first == 4242
    assert second == 4243  # reservation still shared across loops
