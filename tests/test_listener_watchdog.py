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


def test_bounded_retries_once_then_succeeds():
    calls = {"n": 0}

    async def fetch(arg):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(5.0)  # first attempt stalls
        return {"got": arg}

    wrapped = oln._bounded(fetch, label="nft_info", network="testnet", timeout=0.05)
    assert _run(wrapped("abc")) == {"got": "abc"}
    assert calls["n"] == 2


def test_bounded_attempts_are_bounded():
    calls = {"n": 0}

    async def fetch(arg):
        calls["n"] += 1
        await asyncio.sleep(5.0)

    wrapped = oln._bounded(fetch, label="nft_info", network="testnet", timeout=0.05)
    assert _run(wrapped("abc")) is None
    assert calls["n"] == oln.SIDE_CALL_ATTEMPTS


def test_bounded_retries_internal_timeout_none():
    """fetch_metadata swallows its own ~20s internal timeout and returns None —
    shorter than the 30s side-call bound, so _bounded never sees a wait_for
    timeout. A None result must still get the full SIDE_CALL_ATTEMPTS tries."""
    calls = {"n": 0}

    async def fetch(arg):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # internal timeout swallowed, surfaced as None
        return {"got": arg}

    wrapped = oln._bounded(fetch, label="fetch_meta", network="testnet", timeout=1.0)
    assert _run(wrapped("abc")) == {"got": "abc"}
    assert calls["n"] == 2


def test_bounded_all_none_attempts_degrade_to_none():
    calls = {"n": 0}

    async def fetch(arg):
        calls["n"] += 1
        return None

    wrapped = oln._bounded(fetch, label="fetch_meta", network="testnet", timeout=1.0)
    assert _run(wrapped("abc")) is None
    assert calls["n"] == oln.SIDE_CALL_ATTEMPTS


# ------------------------------------------- fail-closed economy on unresolved metadata


def _economy_conn():
    import sqlite3

    from lfg_core import economy_store, market_store, nft_index

    c = sqlite3.connect(":memory:")
    c.executescript(nft_index._SCHEMA)
    economy_store.init_economy_schema(c)
    market_store.init_db(c)
    return c


def _closet_token_shape():
    from lfg_core import config

    return {
        "nft_id": "CLOSET_TO",
        "owner": "rUser",
        "taxon": config.CLOSET_TAXON,
        "uri_hex": "AB",
        "issuer": config.SWAP_ISSUER_ADDRESS,
    }


_CLOSET_MODIFY_TX = {
    "TransactionType": "NFTokenModify",
    "NFTokenID": "CLOSET_TO",
    "meta": {"TransactionResult": "tesSUCCESS"},
}


def test_metadata_timeout_preserves_closet_supply_and_archive(tmp_path):
    """A timed-out metadata fetch (the None `_bounded` returns) must fail
    closed: persisted closet contents and the supply ledger stay untouched,
    lifecycle status (owner-derived) still updates, and the SourceTag archive
    evidence — written before the economy apply — survives."""
    from lfg_core import closet_token as bt
    from lfg_core import config, history_store
    from lfg_core import economy_store as es

    conn = _economy_conn()
    es.set_closet_contents(conn, "rUser", [("Head", "Crown", 2), ("Eyes", "Blue", 1)], [])
    supply_before = es.read_supply_changes(conn)

    hconn = history_store.init_history_db(str(tmp_path / "h.db"))
    ctx = {
        "network": "testnet",
        "nft_issuer": config.SWAP_ISSUER_ADDRESS,
        "genesis_hash": "",
        "source_tag": config.SOURCE_TAG,
        "brix_issuer": "rBrixIssuer",
        "brix_hex": "00",
        "distributor": None,
        "numbers": {},
    }

    async def fetch_token(nft_id):
        return _closet_token_shape()

    async def fetch_meta(uri_hex):
        return None  # what _bounded returns after exhausted timeouts

    tx = dict(
        _CLOSET_MODIFY_TX,
        hash="AB" * 32,
        validated=True,
        ledger_index=100,
        SourceTag=config.SOURCE_TAG,
        date=1_000_000,
    )
    _run(
        oln.process_stream_tx(
            conn,
            tx,
            fetch_token=fetch_token,
            fetch_meta=fetch_meta,
            is_ours=lambda t: False,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )
    # Closet contents preserved — NOT wiped by a rebuild from {}.
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 2, ("Eyes", "Blue"): 1}
    # Supply ledger untouched.
    assert es.read_supply_changes(conn) == supply_before
    # Owner-derived lifecycle still updated (owner != issuer → active).
    record = es.get_closet_record(conn, "rUser")
    assert record is not None and record[2] == bt.ACTIVE
    # SourceTag archive evidence survived the degraded economy apply.
    assert hconn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0] == 1


def test_next_event_processes_after_metadata_timeout():
    """The event AFTER a timed-out one must still apply normally: resolved
    metadata rebuilds the closet contents as usual."""
    from lfg_core import closet_token as bt
    from lfg_core import economy_store as es

    conn = _economy_conn()
    es.set_closet_contents(conn, "rUser", [("Head", "Crown", 2)], [])
    meta = bt.build_closet_metadata("rUser", [("Head", "Crown", 1), ("Mouth", "Grin", 1)], [])
    responses = [None, meta]  # first fetch times out, second resolves

    async def fetch_token(nft_id):
        return _closet_token_shape()

    async def fetch_meta(uri_hex):
        return responses.pop(0)

    for _ in range(2):
        _run(
            oln.process_stream_tx(
                conn,
                dict(_CLOSET_MODIFY_TX),
                fetch_token=fetch_token,
                fetch_meta=fetch_meta,
                is_ours=lambda t: False,
            )
        )
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 1, ("Mouth", "Grin"): 1}


def test_nft_token_timeout_skips_economy_apply_without_state_change():
    """A timed-out nft_info (fetch_token → None) skips both index and economy
    application for that tx and leaves persisted state untouched."""
    from lfg_core import economy_store as es

    conn = _economy_conn()
    es.set_closet_contents(conn, "rUser", [("Head", "Crown", 2)], [])

    async def fetch_token(nft_id):
        return None

    async def fetch_meta(uri_hex):
        raise AssertionError("metadata must not be fetched when the token is unresolved")

    _run(
        oln.process_stream_tx(
            conn,
            dict(_CLOSET_MODIFY_TX),
            fetch_token=fetch_token,
            fetch_meta=fetch_meta,
            is_ours=lambda t: False,
        )
    )
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 2}
    assert es.read_supply_changes(conn) == []


# ---------------------------------------------------------------- env validation


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "-inf"])
def test_timeout_env_rejects_non_positive_and_non_finite(monkeypatch, bad):
    monkeypatch.setenv("LISTENER_STREAM_IDLE_TIMEOUT", bad)
    with pytest.raises(ValueError):
        oln._read_positive_timeout("LISTENER_STREAM_IDLE_TIMEOUT", "300")


def test_timeout_env_accepts_positive_finite(monkeypatch):
    monkeypatch.setenv("LISTENER_SIDE_CALL_TIMEOUT", "12.5")
    assert oln._read_positive_timeout("LISTENER_SIDE_CALL_TIMEOUT", "30") == 12.5
    monkeypatch.delenv("LISTENER_SIDE_CALL_TIMEOUT")
    assert oln._read_positive_timeout("LISTENER_SIDE_CALL_TIMEOUT", "30") == 30.0
