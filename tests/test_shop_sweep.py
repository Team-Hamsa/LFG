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
