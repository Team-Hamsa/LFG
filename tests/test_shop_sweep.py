# tests/test_shop_sweep.py
# Task 7: Trait Shop sweep — offer expiry burn/reversal + settlement retry.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them. (Copy the block verbatim from
# tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402

from lfg_core import (
    config,  # noqa: E402
    shop_store,  # noqa: E402
)
from lfg_core import economy_flow as ef  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
from lfg_service import app as server  # noqa: E402

BUYER = "rBuyerAddress000000000000000000000"
TRAIT1 = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C7000000a1"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _init_onchain(path):
    conn = init_onchain_db(path)
    es.init_economy_schema(conn)
    shop_store.ensure_schema(conn)
    conn.commit()
    return conn


def _reopen(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def onchain_env(tmp_path, monkeypatch):
    onchain_path = str(tmp_path / "onchain_testnet.db")
    conn = _init_onchain(onchain_path)
    conn.commit()
    conn.close()
    app_db = str(tmp_path / "lfg_nfts_testnet.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    monkeypatch.setattr(server.db_path, "app_db_path", lambda net=None: app_db)
    server._shop_settle_attempts.clear()
    yield onchain_path
    server._shop_settle_attempts.clear()


def _seed_order(
    conn,
    session_id,
    *,
    status,
    created_ts,
    nft_id=TRAIT1,
    offer_index="OFFER1",
    buyer=BUYER,
    slot="Hat",
    value="Wizard Hat",
    price_brix=100,
):
    shop_store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO shop_orders (session_id, buyer, slot, value, price_brix,"
        " nft_id, offer_index, status, created_ts, updated_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            buyer,
            slot,
            value,
            price_brix,
            nft_id,
            offer_index,
            status,
            created_ts,
            created_ts,
        ),
    )
    conn.commit()


def _active_buyer_closet(conn, owner=BUYER):
    es.set_closet_token(conn, owner, "CLOSET", "AB", status="active", offer_id=None)
    es.set_closet_contents(conn, owner, [], [])


@dataclass
class _FakeSettleDeps:
    fail_closet_sync: bool = False
    burn_ok: bool = True
    burns: list = field(default_factory=list)

    async def trait_info(self, nft_id):
        return {"taxon": config.TRAIT_TAXON, "issuer": config.SWAP_ISSUER_ADDRESS, "owner": BUYER}

    async def trait_meta(self, nft_id):
        return {"lfg_trait": {"slot": "Hat", "value": "Wizard Hat"}}

    async def trait_burn(self, nft_id, owner):
        self.burns.append((nft_id, owner))
        return "BURNHASH" if self.burn_ok else None

    async def closet_upload(self, meta):
        return "https://cdn/closet.json"

    async def closet_modify(self, nft_id, owner, url):
        return None if self.fail_closet_sync else "MODHASH"

    async def closet_offer(self, nft_id, owner):
        return "OFFER"

    async def closet_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def closet_owner(self, nft_id):
        return BUYER


def _settle_deps(conn, f, tmp_path):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=None,
        closet_offer_fn=f.closet_offer,
        closet_accept_fn=f.closet_accept,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=None,
        char_mint_fn=None,
        char_modify_fn=None,
        char_burn_fn=None,
        char_offer_fn=f.closet_offer,
        char_accept_fn=f.closet_accept,
        closet_owner_fn=f.closet_owner,
        trait_burn_fn=f.trait_burn,
        trait_info_fn=f.trait_info,
        trait_meta_fn=f.trait_meta,
        records_dir=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Expiry pass
# ---------------------------------------------------------------------------


def test_expiry_closes_cancels_burns_and_writes_reversal(onchain_env, monkeypatch):
    conn = _reopen(onchain_env)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_order(conn, "S1", status="pending_accept", created_ts=old_ts)
    conn.close()

    cancels = []
    burns = []

    async def fake_cancel(offer_index, **kw):
        cancels.append(offer_index)
        return "CANCELHASH"

    async def fake_burn(nft_id, *a, **kw):
        burns.append(nft_id)
        return "BURNHASH"

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", fake_burn)

    _run(server.sweep_shop_orders())

    assert cancels == ["OFFER1"]
    assert burns == [TRAIT1]

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S1")
    assert order["status"] == "expired"

    rows = conn.execute(
        "SELECT kind, trait_deltas_json, actor, reason FROM supply_changes"
    ).fetchall()
    assert len(rows) == 1
    kind, deltas_json, actor, reason = rows[0]
    assert kind == "burn"
    assert json.loads(deltas_json) == {"Hat|Wizard Hat": -1}
    assert actor == "shop"
    assert reason == "shop expiry S1"


def test_expiry_contains_reversal_failure_journals_and_closes_expired(
    onchain_env, monkeypatch, tmp_path
):
    conn = _reopen(onchain_env)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_order(conn, "S1B", status="pending_accept", created_ts=old_ts)
    conn.close()

    burns = []

    async def fake_cancel(offer_index, **kw):
        return "CANCELHASH"

    async def fake_burn(nft_id, *a, **kw):
        burns.append(nft_id)
        return "BURNHASH"

    def fake_record_supply_change(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    records_dir = str(tmp_path / "econ_records")
    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(server.economy_store, "record_supply_change", fake_record_supply_change)
    monkeypatch.setattr(server.config, "ECONOMY_RECORDS_DIR", records_dir)

    _run(server.sweep_shop_orders())

    assert burns == [TRAIT1]

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S1B")
    assert order["status"] == "expired"
    rows = conn.execute("SELECT * FROM supply_changes").fetchall()
    assert rows == []
    conn.close()

    journal_path = os.path.join(records_dir, "shop-expiry-reversal-giveup-S1B.json")
    assert os.path.exists(journal_path)
    with open(journal_path) as f:
        record = json.load(f)
    assert record["slot"] == "Hat"
    assert record["value"] == "Wizard Hat"
    assert record["delta"] == -1
    assert record["session_id"] == "S1B"

    # A second sweep pass must not re-burn or re-touch the now-`expired` order.
    burns.clear()
    _run(server.sweep_shop_orders())
    assert burns == []
    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S1B")
    assert order["status"] == "expired"
    conn.close()


def test_expiry_rescues_landed_accept_instead_of_burning(onchain_env, monkeypatch):
    conn = _reopen(onchain_env)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_order(conn, "S2", status="pending_accept", created_ts=old_ts)
    conn.close()

    async def fake_cancel(offer_index, **kw):
        return "CANCELHASH"

    async def fake_burn(nft_id, *a, **kw):
        return None  # owner-mismatch / token-not-ours

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", fake_burn)

    _run(server.sweep_shop_orders())

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S2")
    assert order["status"] == "accepted"

    rows = conn.execute("SELECT * FROM supply_changes").fetchall()
    assert rows == []


def test_expiry_ensures_economy_schema_before_recording_supply_change(tmp_path, monkeypatch):
    # Regression: _expire_shop_order must call init_economy_schema itself
    # (parity with _settle_shop_order), not rely on the caller/fixture having
    # already created supply_changes. Build the onchain DB with only the
    # nft_index + shop_store schema present -- no economy_store init -- to
    # prove the expiry path is self-sufficient.
    onchain_path = str(tmp_path / "onchain_testnet.db")
    conn = init_onchain_db(onchain_path)
    shop_store.ensure_schema(conn)
    conn.commit()
    conn.close()
    app_db = str(tmp_path / "lfg_nfts_testnet.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    monkeypatch.setattr(server.db_path, "app_db_path", lambda net=None: app_db)
    server._shop_settle_attempts.clear()

    conn = _reopen(onchain_path)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_order(conn, "S0", status="pending_accept", created_ts=old_ts)
    conn.close()

    async def fake_cancel(offer_index, **kw):
        return "CANCELHASH"

    async def fake_burn(nft_id, *a, **kw):
        return "BURNHASH"

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", fake_burn)

    _run(server.sweep_shop_orders())

    conn = _reopen(onchain_path)
    order = shop_store.get_order(conn, "S0")
    assert order["status"] == "expired"
    rows = conn.execute("SELECT kind FROM supply_changes").fetchall()
    assert len(rows) == 1


def test_expiry_leaves_non_expired_and_settled_orders_untouched(onchain_env, monkeypatch):
    conn = _reopen(onchain_env)
    recent_ts = int(time.time())
    _seed_order(conn, "S3", status="pending_accept", created_ts=recent_ts, offer_index="OFFER3")
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_order(conn, "S4", status="settled", created_ts=old_ts, offer_index="OFFER4")
    conn.close()

    async def boom(*a, **kw):
        raise AssertionError("must not touch non-expired or already-settled orders")

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", boom)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", boom)

    _run(server.sweep_shop_orders())

    conn = _reopen(onchain_env)
    assert shop_store.get_order(conn, "S3")["status"] == "pending_accept"
    assert shop_store.get_order(conn, "S4")["status"] == "settled"


# ---------------------------------------------------------------------------
# Settlement pass
# ---------------------------------------------------------------------------


def test_settlement_retry_settles_and_increments_shop_count(onchain_env, monkeypatch, tmp_path):
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    _seed_order(conn, "S5", status="accepted", created_ts=int(time.time()))
    conn.close()

    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    _run(server.sweep_shop_orders())

    assert f.burns == [(TRAIT1, BUYER)]

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S5")
    assert order["status"] == "settled"

    app_conn = sqlite3.connect(server.db_path.app_db_path("testnet"))
    row = app_conn.execute(
        "SELECT shop_count FROM trait_rarity WHERE network=? AND category=? AND trait=?",
        ("testnet", "Hat", "Wizard Hat"),
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_settlement_marks_order_settled_even_if_shop_count_increment_raises(
    onchain_env, monkeypatch, tmp_path
):
    """Bot review finding (#217): the settled status write must happen before
    the best-effort shop_count increment, so a raising increment never leaves
    a completed purchase as a ghost order."""
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    _seed_order(conn, "S5B", status="accepted", created_ts=int(time.time()))
    conn.close()

    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    def raising_increment(*a, **kw):
        raise RuntimeError("increment boom")

    monkeypatch.setattr(server.rarity, "increment_shop_count", raising_increment)

    _run(server.sweep_shop_orders())

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S5B")
    assert order["status"] == "settled"


def test_settlement_pass_isolates_per_order_failures(onchain_env, monkeypatch, tmp_path):
    """Bot review finding (#217): one order's settle call raising must not
    prevent the next order in the same sweep pass from being processed,
    mirroring the adjacent expiry loop's per-order isolation."""
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    _seed_order(conn, "S5C", status="accepted", created_ts=int(time.time()), offer_index="OFFERC")
    _seed_order(conn, "S5D", status="accepted", created_ts=int(time.time()), offer_index="OFFERD")
    conn.close()

    calls = []
    real_settle = server._settle_shop_order

    async def flaky_settle(order, network):
        calls.append(order["session_id"])
        if order["session_id"] == "S5C":
            raise RuntimeError("boom")
        return await real_settle(order, network)

    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )
    monkeypatch.setattr(server, "_settle_shop_order", flaky_settle)

    _run(server.sweep_shop_orders())

    assert calls == ["S5C", "S5D"]
    conn = _reopen(onchain_env)
    assert shop_store.get_order(conn, "S5D")["status"] == "settled"


def test_settlement_giveup_after_max_attempts_journals_and_fails(
    onchain_env, monkeypatch, tmp_path
):
    conn = _reopen(onchain_env)
    # No active Closet for the buyer -> run_deposit's precondition always fails.
    _seed_order(conn, "S6", status="accepted", created_ts=int(time.time()))
    conn.close()

    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )
    monkeypatch.setattr(server.config, "ECONOMY_RECORDS_DIR", str(tmp_path))

    for _ in range(server._SHOP_SWEEP_MAX_ATTEMPTS):
        _run(server.sweep_shop_orders())

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S6")
    assert order["status"] == "failed"

    records = list(tmp_path.glob("shop-settlement-giveup-S6.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["status"] == "abandoned"
    assert record["session_id"] == "S6"
    assert record["nft_id"] == TRAIT1
    assert record["buyer"] == BUYER


def test_orphan_after_transient_expiry_burn_error_then_settlement_failure(
    onchain_env, monkeypatch, tmp_path
):
    """Adjudicated trajectory: a transient error burning the token during
    expiry rescues the order to `accepted` (fail-closed -- never burn on
    uncertainty). If settlement then keeps failing (e.g. the buyer never
    activated a Closet), the order eventually gives up as `failed` -- but the
    token must never actually be burned along the way: xrpl_ops.burn_nft
    (the expiry burn) is called exactly once, and run_deposit's issuer burn
    is never reached because the Closet precondition fails first."""
    conn = _reopen(onchain_env)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_order(conn, "S7", status="pending_accept", created_ts=old_ts)
    conn.close()

    expiry_burn_calls = []

    async def fake_cancel(offer_index, **kw):
        return "CANCELHASH"

    async def flaky_burn(nft_id, *a, **kw):
        expiry_burn_calls.append(nft_id)
        raise RuntimeError("transient RPC timeout")

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", flaky_burn)

    _run(server.sweep_shop_orders())

    assert expiry_burn_calls == [TRAIT1]  # attempted once, crashed -> rescued

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S7")
    assert order["status"] == "accepted"
    assert conn.execute("SELECT * FROM supply_changes").fetchall() == []
    conn.close()

    # No active Closet for the buyer -> run_deposit's precondition always
    # fails, so the settlement sweep never even reaches the deposit burn.
    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )
    monkeypatch.setattr(server.config, "ECONOMY_RECORDS_DIR", str(tmp_path))

    for _ in range(server._SHOP_SWEEP_MAX_ATTEMPTS):
        _run(server.sweep_shop_orders())

    # burn_nft must not have been retried by later expiry-sweep passes (the
    # order is no longer `pending_accept`), and run_deposit's own issuer
    # burn must never have fired either.
    assert expiry_burn_calls == [TRAIT1]
    assert f.burns == []

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "S7")
    assert order["status"] == "failed"
    conn.close()

    records = list(tmp_path.glob("shop-settlement-giveup-S7.json"))
    assert len(records) == 1


# ---------------------------------------------------------------------------
# #238 XRP payment fallback: expiry parity + sweep-path buyback
# ---------------------------------------------------------------------------


def _seed_xrp_order(conn, session_id, *, status, created_ts, **kw):
    _seed_order(conn, session_id, status=status, created_ts=created_ts, **kw)
    shop_store.update_order(
        conn, session_id, now_ts=created_ts, pay_with="XRP", price_xrp="0.105000"
    )


def test_expiry_xrp_order_identical_and_no_buyback(onchain_env, monkeypatch):
    """An expired XRP-path pending_accept order is cancelled/burned/supply-
    reversed exactly like BRIX — and fires NO buyback (no XRP was ever
    collected)."""
    conn = _reopen(onchain_env)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_xrp_order(conn, "X1", status="pending_accept", created_ts=old_ts)
    conn.close()

    cancels, burns, buybacks = [], [], []

    async def fake_cancel(offer_index, **kw):
        cancels.append(offer_index)
        return "CANCELHASH"

    async def fake_burn(nft_id, *a, **kw):
        burns.append(nft_id)
        return "BURNHASH"

    async def fake_buy_and_burn(*a, **kw):
        buybacks.append((a, kw))
        return "HASH"

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(server.xrpl_ops, "buy_and_burn", fake_buy_and_burn)

    _run(server.sweep_shop_orders())

    assert cancels == ["OFFER1"]
    assert burns == [TRAIT1]
    assert buybacks == []

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "X1")
    assert order["status"] == "expired"
    assert order["buyback_done"] == 0
    rows = conn.execute("SELECT kind, trait_deltas_json FROM supply_changes").fetchall()
    assert len(rows) == 1 and rows[0][0] == "burn"
    assert json.loads(rows[0][1]) == {"Hat|Wizard Hat": -1}


def test_expiry_rescue_then_settlement_fires_buyback_once(onchain_env, monkeypatch, tmp_path):
    """Rescue branch (accept landed despite the local timeout): the order is
    re-routed to `accepted`, the settlement pass settles it, and the buyback
    fires exactly once with the order's BRIX price capped at price_xrp."""
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_xrp_order(conn, "X2", status="pending_accept", created_ts=old_ts)
    conn.close()

    buybacks = []

    async def fake_cancel(offer_index, **kw):
        return "CANCELHASH"

    async def fake_burn(nft_id, *a, **kw):
        return None  # issuer no longer holds the token -> rescue to accepted

    async def fake_buy_and_burn(currency, issuer, value, max_xrp=None):
        buybacks.append((currency, issuer, value, max_xrp))
        return "HASH"

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(server.xrpl_ops, "buy_and_burn", fake_buy_and_burn)

    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    _run(server.sweep_shop_orders())  # expiry pass rescues -> settlement pass settles

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "X2")
    assert order["status"] == "settled"
    assert order["buyback_done"] == 1
    assert buybacks == [
        (config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER, "100", "0.105000")
    ]
    conn.close()

    # A second sweep pass must not re-fire the buyback.
    _run(server.sweep_shop_orders())
    assert len(buybacks) == 1


def test_sweep_settlement_brix_order_no_buyback(onchain_env, monkeypatch, tmp_path):
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    _seed_order(conn, "X3", status="accepted", created_ts=int(time.time()))
    conn.close()

    async def boom_buyback(*a, **kw):
        raise AssertionError("buy_and_burn must not fire for a BRIX-path order")

    monkeypatch.setattr(server.xrpl_ops, "buy_and_burn", boom_buyback)
    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    _run(server.sweep_shop_orders())

    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "X3")
    assert order["status"] == "settled" and order["buyback_done"] == 0


def test_sweep_settlement_buyback_failure_still_settles_single_attempt(
    onchain_env, monkeypatch, tmp_path
):
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    _seed_xrp_order(conn, "X4", status="accepted", created_ts=int(time.time()))
    conn.close()

    calls = []

    async def failing_buyback(*a, **kw):
        calls.append(1)
        raise RuntimeError("amm boom")

    monkeypatch.setattr(server.xrpl_ops, "buy_and_burn", failing_buyback)
    f = _FakeSettleDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    _run(server.sweep_shop_orders())
    _run(server.sweep_shop_orders())  # second pass: settled order, no retry

    assert calls == [1]
    conn = _reopen(onchain_env)
    order = shop_store.get_order(conn, "X4")
    assert order["status"] == "settled" and order["buyback_done"] == 1
