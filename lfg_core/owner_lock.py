# lfg_core/owner_lock.py
# Per-owner serialization of the Closet read -> sync -> mirror sequence (#180).
#
# The Closet NFToken is a full-overwrite record: sync_closet recomposes the
# ENTIRE metadata from the contents it is handed and NFTokenModifies the URI in
# place. Two flows that each read the DB mirror, compute a new full state, and
# modify the token can therefore lose an update — the second modify's URI wins
# and silently erases the first (e.g. a paid, already-burned trait vanishes from
# the authoritative on-chain Closet).
#
# Concurrency is real even though a single user gets at most one economy session
# per platform: one wallet can act from Discord AND Telegram at once, trait-sale
# settlement runs run_deposit inline (buy-status handler) while the 120s sweep
# retries the same owner, and Extract lives in market_sessions while Equip lives
# in economy_sessions. All of these run on the one service event loop, so a
# per-owner asyncio.Lock spanning the whole read-modify-write serializes them.

from __future__ import annotations

import asyncio
import weakref

# One lock registry per event loop. asyncio primitives bind to the loop that
# first drives them, so a module-global lock reused across loops (e.g. each
# test's fresh loop) would raise "bound to a different event loop". Keying on
# the running loop keeps production — a single long-lived loop — sharing exactly
# one lock per owner, while test loops each get an isolated, correctly-bound
# registry. WeakKeyDictionary drops a loop's registry when the loop is
# garbage-collected, so short-lived test loops leave nothing behind.
_registries: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


def owner_lock(owner: str) -> asyncio.Lock:
    """The asyncio.Lock guarding every Closet read-modify-write for `owner` on
    the current event loop. The get-or-create runs synchronously (no await), so
    it is atomic with respect to the single-threaded event loop and needs no
    guard of its own. The lock is NOT reentrant — a single flow must acquire it
    once at its outermost boundary and never nest another acquisition for the
    same owner."""
    loop = asyncio.get_running_loop()
    registry = _registries.get(loop)
    if registry is None:
        registry = {}
        _registries[loop] = registry
    lock = registry.get(owner)
    if lock is None:
        lock = asyncio.Lock()
        registry[owner] = lock
    return lock
