# Watchdog + bounded side calls for the on-chain listener (#345): the mainnet
# stream wedged silently for ~10 days because an await inside the stream loop
# (nft_info / fetch_meta / the identity request) could hang forever, and a
# stalled subscription never raised into the reconnect loop. These tests pin
# the two seams that make silence impossible: `_iter_with_watchdog` (stream
# staleness → StreamStalled → reconnect path) and `_bounded` (side-channel
# timeouts degrade to None instead of freezing the loop).

import asyncio
import logging
import os
import sys

import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import onchain_listener as oln  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------- watchdog


async def _gen(items, hang_after=None):
    for item in items:
        yield item
    if hang_after is not None:
        await asyncio.sleep(hang_after)


def test_watchdog_passes_messages_through_and_ends_cleanly():
    async def go():
        out = []
        async for msg in oln._iter_with_watchdog(_gen([1, 2, 3]), idle_timeout=1.0):
            out.append(msg)
        return out

    assert _run(go()) == [1, 2, 3]


def test_watchdog_raises_stream_stalled_on_idle():
    async def go():
        out = []
        async for msg in oln._iter_with_watchdog(_gen(["only"], hang_after=5.0), idle_timeout=0.05):
            out.append(msg)
        return out

    with pytest.raises(oln.StreamStalled):
        _run(go())


def test_stream_stalled_is_a_plain_exception():
    # The reconnect loop catches `Exception`; the watchdog must ride that path
    # (never BaseException / CancelledError, which would kill the process).
    assert issubclass(oln.StreamStalled, Exception)
    assert not issubclass(oln.StreamStalled, asyncio.CancelledError)


def test_watchdog_default_timeout_is_configured_constant():
    assert oln.STREAM_IDLE_TIMEOUT > 0
    assert oln.SIDE_CALL_TIMEOUT > 0


# ---------------------------------------------------------------- bounded side calls


def test_bounded_returns_result_when_fast():
    async def fetch(arg):
        return {"got": arg}

    wrapped = oln._bounded(fetch, label="nft_info", network="testnet", timeout=1.0)
    assert _run(wrapped("abc")) == {"got": "abc"}


def test_bounded_times_out_to_none_and_logs(caplog):
    async def fetch(arg):
        await asyncio.sleep(5.0)

    wrapped = oln._bounded(fetch, label="nft_info", network="testnet", timeout=0.05)
    with caplog.at_level(logging.WARNING):
        assert _run(wrapped("deadbeef")) is None
    assert any("nft_info" in r.message and "timed out" in r.message for r in caplog.records)


def test_bounded_propagates_non_timeout_errors():
    async def fetch(arg):
        raise ValueError("boom")

    wrapped = oln._bounded(fetch, label="fetch_meta", network="testnet", timeout=1.0)
    with pytest.raises(ValueError):
        _run(wrapped("x"))
