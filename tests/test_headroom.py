# tests/test_headroom.py
# #226: atomic, durable headroom reservations under MAX_COLLECTION_SIZE —
# grant math, one-writer-wins atomicity, fail-closed store errors, release
# idempotence, retire-to-pending accounting, prune-on-index-catch-up, startup
# rebuild, and the single-mint session's reserve/settle lifecycle.
#
# Env guard: set before lfg_core imports so frozen config constants are sane
# when this file runs first (see test-env-guard convention).
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402

import pytest  # noqa: E402

from lfg_core import config, headroom, mint_flow, supply  # noqa: E402

NET = "testnet"


def _run(coro):
    # New private loop per call, never touching the thread's current-loop
    # slot: asyncio.run() ends with set_event_loop(None), which poisons the
    # legacy asyncio.get_event_loop() idiom used by test files that sort
    # after this one (test_market_api etc.) in full-suite order.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    monkeypatch.setattr(supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(headroom.nft_index, "index_db_path", lambda net: str(tmp_path / "idx.db"))


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "app.db")


def _make_index(tmp_path, nft_ids):
    conn = sqlite3.connect(str(tmp_path / "idx.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS onchain_nfts (nft_id TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO onchain_nfts (nft_id) VALUES (?)", [(i,) for i in nft_ids]
    )
    conn.commit()
    conn.close()


# --- grant math --------------------------------------------------------------


def test_grant_full_partial_and_zero(db, monkeypatch):
    monkeypatch.setattr(supply, "current_supply", lambda net: 9995)
    assert headroom.try_reserve(db, "bulk:a", 5, NET) == 5
    assert headroom.try_reserve(db, "bulk:b", 5, NET) == 0  # tail already taken
    headroom.release(db, "bulk:a", 2)
    assert headroom.try_reserve(db, "bulk:b", 5, NET) == 2  # partial grant
    assert headroom.outstanding(db) == 5


def test_grant_zero_qty_is_noop(db):
    assert headroom.try_reserve(db, "bulk:a", 0, NET) == 0
    assert headroom.outstanding(db) == 0


def test_two_concurrent_reservers_never_grant_past_max(db, monkeypatch):
    """One-writer-wins: two threads racing for the 5-slot tail must be
    serialized by BEGIN IMMEDIATE — grants sum to exactly 5, never 10.

    The supply read carries a 2-party rendezvous (review on #226): only a
    NON-atomic try_reserve lets both threads reach the read window
    concurrently (both would see reserved=0 and grant 5 each — the exact
    overshoot this module exists to prevent, and grants become [5, 5]). With
    the real BEGIN IMMEDIATE the second writer blocks behind the first, the
    barrier times out (well under the 5s busy timeout, so no deadlock), and
    grants stay [0, 5] — making this test fail if the transaction is ever
    weakened to autocommit."""
    read_barrier = threading.Barrier(2)

    def _slow_supply(net):
        try:
            read_barrier.wait(timeout=1.5)  # << _BUSY_TIMEOUT_MS: atomic code times out here
        except threading.BrokenBarrierError:
            pass
        return 9995

    monkeypatch.setattr(supply, "current_supply", _slow_supply)
    grants = {}
    barrier = threading.Barrier(2)

    def _reserve(name):
        barrier.wait()
        grants[name] = headroom.try_reserve(db, f"bulk:{name}", 5, NET)

    threads = [threading.Thread(target=_reserve, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(grants.values()) == [0, 5]
    assert headroom.outstanding(db) == 5


def test_store_error_fails_closed_for_grants(tmp_path):
    bad = str(tmp_path)  # a directory is not an openable sqlite database
    assert headroom.try_reserve(bad, "bulk:a", 5, NET) == 0
    headroom.release(bad, "bulk:a")  # never raises
    headroom.retire_to_pending(bad, "bulk:a", "N1")  # never raises
    assert headroom.reserved_for(bad, "bulk:a") is None  # tri-state: read failed
    assert headroom.outstanding(bad) is None  # tri-state: read failed
    headroom.rebuild(bad, [("bulk:a", 1, [])])  # never raises


def test_unreadable_supply_fails_closed_for_grants(db, monkeypatch):
    """Review on #226: supply.current_supply now PROPAGATES non-missing-table
    OperationalErrors (e.g. a locked index DB) instead of returning 0 — a
    locked index reading as supply 0 would inflate availability to the whole
    collection and over-grant past the cap. try_reserve's blanket except must
    turn that into grant 0 (fail CLOSED for new headroom), never a grant."""

    def _locked(net):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(supply, "current_supply", _locked)
    assert headroom.try_reserve(db, "bulk:a", 5, NET) == 0
    assert headroom.outstanding(db) == 0  # nothing was granted or leaked


def test_release_is_idempotent(db):
    assert headroom.try_reserve(db, "bulk:a", 3, NET) == 3
    headroom.release(db, "bulk:a")
    headroom.release(db, "bulk:a")  # second full release: no-op
    headroom.release(db, "missing", 1)  # unknown claimant: no-op
    assert headroom.outstanding(db) == 0


# --- retire / prune ----------------------------------------------------------


def test_retire_moves_reserved_to_pending(db):
    assert headroom.try_reserve(db, "bulk:a", 3, NET) == 3
    headroom.retire_to_pending(db, "bulk:a", "N1")
    headroom.retire_to_pending(db, "bulk:a", "N1")  # idempotent on nft_id
    assert headroom.reserved_for(db, "bulk:a") == 2
    assert headroom.outstanding(db) == 3  # 2 reserved + 1 pending: no undercount


def test_prune_retires_pending_once_indexed(db, tmp_path, monkeypatch):
    """The (b)->(c) handoff: a pending mint keeps blocking headroom while the
    index lags, and stops being double-counted once the listener indexes it
    (prune runs inside try_reserve, before the supply read)."""
    monkeypatch.setattr(supply, "current_supply", lambda net: 9995)
    assert headroom.try_reserve(db, "bulk:a", 5, NET) == 5
    for i in range(5):
        headroom.retire_to_pending(db, "bulk:a", f"N{i}")
    assert headroom.reserved_for(db, "bulk:a") == 0
    # Index still lagging: all 5 pending -> zero availability.
    assert headroom.try_reserve(db, "bulk:c", 1, NET) == 0
    # Listener catches up: mints indexed AND counted by supply.
    _make_index(tmp_path, [f"N{i}" for i in range(5)])
    monkeypatch.setattr(supply, "current_supply", lambda net: 10000)
    assert headroom.try_reserve(db, "bulk:c", 1, NET) == 0  # full, not double-free
    assert headroom.outstanding(db) == 0  # pending pruned, supply took over
    # Room appears only when real supply drops (e.g. burns).
    monkeypatch.setattr(supply, "current_supply", lambda net: 9999)
    assert headroom.try_reserve(db, "bulk:c", 3, NET) == 1


# --- rebuild -----------------------------------------------------------------


def test_rebuild_drops_orphans_reinserts_live_and_keeps_pending(db):
    headroom.try_reserve(db, "bulk:dead", 4, NET)
    headroom.try_reserve(db, "mint:dead", 1, NET)
    headroom.try_reserve(db, "mint:live", 1, NET)
    headroom.retire_to_pending(db, "bulk:dead", "N-old")  # real on-chain mint
    headroom.rebuild(
        db,
        [("bulk:resumed", 2, ["N-resumed"])],
        keep=["mint:live"],
    )
    assert headroom.reserved_for(db, "bulk:dead") == 0  # orphan dropped
    assert headroom.reserved_for(db, "mint:dead") == 0  # in-memory session died
    assert headroom.reserved_for(db, "mint:live") == 1  # keep-set preserved
    assert headroom.reserved_for(db, "bulk:resumed") == 2
    # outstanding = 1 (mint:live) + 2 (bulk:resumed) + 2 pending (N-old kept,
    # N-resumed re-asserted).
    assert headroom.outstanding(db) == 5


def test_rebuild_zero_reserved_deletes_row(db):
    headroom.try_reserve(db, "bulk:a", 3, NET)
    headroom.rebuild(db, [("bulk:a", 0, ["N1", "N2", "N3"])])
    assert headroom.reserved_for(db, "bulk:a") == 0
    assert headroom.outstanding(db) == 3  # all three ride in pending now


# --- single-mint session lifecycle (#226) ------------------------------------


def _session(db, monkeypatch, tmp_path):
    monkeypatch.setattr(mint_flow.db_path, "app_db_path", lambda net=None: db)
    s = mint_flow.MintSession(discord_id="u1", wallet_address="rUSER")
    granted = headroom.try_reserve(db, f"mint:{s.id}", 1, NET)
    assert granted == 1
    s.headroom_reserved = True
    return s


def test_failed_session_releases_reservation(db, monkeypatch, tmp_path):
    """#262 fail-fast path (payment_uuid None -> FAILED) runs inside
    run_mint_session's try, so the finally settles the reservation."""
    s = _session(db, monkeypatch, tmp_path)
    assert s.payment_uuid is None
    _run(mint_flow.run_mint_session(s))
    assert s.state == mint_flow.FAILED
    assert s.headroom_reserved is False
    assert headroom.outstanding(db) == 0


def test_payment_timeout_releases_reservation(db, monkeypatch, tmp_path):
    async def _no_payment(**kw):
        return False

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", _no_payment)
    s = _session(db, monkeypatch, tmp_path)
    s.payment_uuid = "PAYUUID"
    _run(mint_flow.run_mint_session(s))
    assert s.state == mint_flow.PAYMENT_TIMEOUT
    assert headroom.outstanding(db) == 0


def test_cancel_releases_reservation(db, monkeypatch, tmp_path):
    s = _session(db, monkeypatch, tmp_path)
    assert s.cancel() is True
    assert s.headroom_reserved is False
    assert headroom.outstanding(db) == 0
    mint_flow.settle_headroom(s)  # idempotent: task-finally double-call


def test_minted_session_retires_to_pending(db, monkeypatch, tmp_path):
    """Success path: OFFER_READY retires the reservation to the pending set
    (the mint is on-chain but not yet indexed — it must keep counting)."""

    async def _paid(**kw):
        return True

    async def _alloc():
        return 4100

    async def _mint_one_unit(**kw):
        return mint_flow.UnitResult(
            nft_number=4100,
            nft_id="NFTID-226",
            image_url="https://cdn.example/i.png",
            offer_id="OFFER1",
            accept={"qr_url": "q", "xumm_url": "x", "uuid": "u"},
            error=None,
        )

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", _paid)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _alloc)
    monkeypatch.setattr(mint_flow, "mint_one_unit", _mint_one_unit)
    s = _session(db, monkeypatch, tmp_path)
    s.payment_uuid = "PAYUUID"
    s.pay_with, s.pay_amount = "LFGO", "1"  # skip the XRP buy_and_burn branch
    _run(mint_flow.run_mint_session(s))
    assert s.state == mint_flow.OFFER_READY
    assert s.headroom_reserved is False
    assert headroom.reserved_for(db, f"mint:{s.id}") == 0
    assert headroom.outstanding(db) == 1  # pending until the listener indexes it


def test_single_mint_settles_at_mint_land_not_session_end(db, monkeypatch, tmp_path):
    """#226 review: run_mint_session passes on_mint to mint_one_unit so the
    reservation is retired to the DURABLE pending set the instant the mint
    lands — symmetric with bulk. A hard crash during the offer/XUMM steps
    (seconds to tens of seconds) can then no longer uncount an on-chain mint:
    the restart rebuild drops mint:* rows, but the pending row survives and
    keeps counting until the listener indexes the mint."""

    async def _paid(**kw):
        return True

    async def _alloc():
        return 4300

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", _paid)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _alloc)
    s = _session(db, monkeypatch, tmp_path)
    s.payment_uuid = "PAYUUID"
    s.pay_with, s.pay_amount = "LFGO", "1"
    observed = {}

    async def _mint_then_die(*, on_mint=None, **kw):
        assert on_mint is not None  # run_mint_session must wire the callback
        await on_mint(4300, "NFTID-CRASH", None)
        # At this instant — before offer creation would run — the unit must
        # already ride in the pending set, not the volatile mint:* row.
        observed["reserved"] = headroom.reserved_for(db, f"mint:{s.id}")
        observed["outstanding"] = headroom.outstanding(db)
        raise RuntimeError("hard failure during offer creation")

    monkeypatch.setattr(mint_flow, "mint_one_unit", _mint_then_die)
    _run(mint_flow.run_mint_session(s))
    assert observed == {"reserved": 0, "outstanding": 1}
    assert s.state == mint_flow.FAILED
    assert s.headroom_reserved is False
    # Even after a rebuild that drops every mint:* row (restart), the mint
    # stays counted via its pending row.
    headroom.rebuild(db, [])
    assert headroom.outstanding(db) == 1
