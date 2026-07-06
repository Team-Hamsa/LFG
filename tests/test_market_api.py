# tests/test_market_api.py
# Task 7: service endpoints for the in-app marketplace —
# GET /api/market/listings (public, cached), GET /api/market/mine (wallet-gated),
# GET /api/market/history (public). Task 8 extends this same file with
# list/cancel/buy session handlers.
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
from urllib.parse import quote  # noqa: E402

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core.economy_store import (  # noqa: E402
    _ECONOMY_SCHEMA,  # noqa: E402
    set_closet_contents,
    upsert_trait_token,
)
from lfg_core.history_store import init_history_db, insert_nft_event  # noqa: E402
from lfg_core.market_store import (
    MarketListing,  # noqa: E402
    upsert_listing,  # noqa: E402
)
from lfg_core.market_store import init_db as init_market_db  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
from lfg_core.nft_index import upsert as upsert_onchain_nft  # noqa: E402
from lfg_service import app as server  # noqa: E402

SELLER = "rSellerAddress0000000000000000000"
BUYER = "rBuyerAddress000000000000000000000"
CHAR1 = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000001"
CHAR2 = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000002"
CHAR3_UNLISTED = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000003"
TRAIT1 = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C7000000a1"
TRAIT2_UNLISTED = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C7000000a2"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mocked_request(method, path):
    return make_mocked_request(method, path, app=web.Application())


async def _read_json(resp):
    return json.loads(resp.body.decode())


def _init_onchain(path):
    conn = init_onchain_db(path)
    conn.executescript(_ECONOMY_SCHEMA)
    init_market_db(conn)
    conn.commit()
    return conn


def _seed_character(conn, nft_id, owner, nft_number, attrs=None, image="https://cdn.example/x.png"):
    attrs = attrs if attrs is not None else [{"trait_type": "Hat", "value": "Wizard Hat"}]
    upsert_onchain_nft(
        conn,
        OnchainNft(
            nft_id=nft_id,
            nft_number=nft_number,
            owner=owner,
            is_burned=False,
            mutable=True,
            uri_hex="",
            body="Ape",
            attributes=attrs,
            image=image,
            ledger_index=1,
        ),
    )


def _seed_listing(conn, **overrides):
    base = {
        "offer_index": "A" * 64,
        "nft_id": CHAR1,
        "kind": "character",
        "seller": SELLER,
        "amount_drops": 1_000_000,
        "created_ledger": 100,
        "created_ts": 1000,
    }
    base.update(overrides)
    upsert_listing(conn, MarketListing(**base))


@pytest.fixture
def onchain_env(tmp_path, monkeypatch):
    onchain_path = str(tmp_path / "onchain_testnet.db")
    conn = _init_onchain(onchain_path)
    conn.commit()
    conn.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    server._MARKET_CACHE.clear()
    yield onchain_path
    server._MARKET_CACHE.clear()


def _reopen(onchain_path):
    conn = sqlite3.connect(onchain_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# GET /api/market/listings
# ---------------------------------------------------------------------------


def test_browse_default_kind_character_200(onchain_env):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn)
    conn.commit()
    conn.close()

    req = _mocked_request("GET", "/api/market/listings")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["nft_id"] == CHAR1
    assert row["kind"] == "character"
    assert row["nft_number"] == 1
    assert row["amount_drops"] == 1_000_000
    assert row["amount_xrp"] == "1"
    assert isinstance(row["amount_xrp"], str)
    assert row["seller"] == SELLER
    assert row["offer_index"] == "A" * 64
    assert row["image"] == "https://cdn.example/x.png"
    assert row["attributes"] == [{"trait_type": "Hat", "value": "Wizard Hat"}]


def test_browse_bad_kind_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?kind=vehicle")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_bad_sort_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?sort=random")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_bad_trait_format_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?trait=NoColonHere")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_bad_min_xrp_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?min_xrp=abc")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_min_xrp_too_many_decimals_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?min_xrp=1.1234567")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_min_xrp_zero_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?min_xrp=0")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_negative_limit_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?limit=-1")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_limit_over_100_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?limit=101")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_negative_offset_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?offset=-5")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_huge_offset_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?offset=99999999999")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_non_integer_limit_400(onchain_env):
    req = _mocked_request("GET", "/api/market/listings?limit=abc")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 400


def test_browse_filters_price_and_trait(onchain_env):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1, attrs=[{"trait_type": "Hat", "value": "Wizard Hat"}])
    _seed_character(conn, CHAR2, SELLER, 2, attrs=[{"trait_type": "Hat", "value": "Top Hat"}])
    _seed_listing(conn, offer_index="A" * 64, nft_id=CHAR1, amount_drops=2_000_000, created_ts=1000)
    _seed_listing(conn, offer_index="B" * 64, nft_id=CHAR2, amount_drops=1_000_000, created_ts=1001)
    conn.commit()
    conn.close()

    # AND-across-slots / OR-within-slot filter: only Wizard Hat or Cowboy Hat
    req = _mocked_request(
        "GET",
        "/api/market/listings?trait="
        + quote("Hat:Wizard Hat")
        + "&trait="
        + quote("Hat:Cowboy Hat"),
    )
    resp = _run(server.handle_market_listings(req))
    body = _run(_read_json(resp))
    assert [r["nft_id"] for r in body["rows"]] == [CHAR1]

    # sort=price_desc across both listings
    req2 = _mocked_request("GET", "/api/market/listings?sort=price_desc")
    resp2 = _run(server.handle_market_listings(req2))
    body2 = _run(_read_json(resp2))
    assert [r["nft_id"] for r in body2["rows"]] == [CHAR1, CHAR2]

    # min_xrp/max_xrp edge filter (1.5 XRP - 3 XRP only keeps CHAR1 @ 2 XRP)
    req3 = _mocked_request("GET", "/api/market/listings?min_xrp=1.5&max_xrp=3")
    resp3 = _run(server.handle_market_listings(req3))
    body3 = _run(_read_json(resp3))
    assert [r["nft_id"] for r in body3["rows"]] == [CHAR1]


def test_browse_trait_kind_rows_have_slot_value_and_image(onchain_env):
    conn = _reopen(onchain_env)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    _seed_listing(
        conn,
        offer_index="C" * 64,
        nft_id=TRAIT1,
        kind="trait",
        slot="Hat",
        value="Wizard Hat",
        amount_drops=500_000,
    )
    conn.commit()
    conn.close()

    req = _mocked_request("GET", "/api/market/listings?kind=trait")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["kind"] == "trait"
    assert row["slot"] == "Hat"
    assert row["value"] == "Wizard Hat"
    assert row["image"]  # some layer-proxy URL, non-empty
    assert row["image"].startswith("/api/layer?")


def test_browse_pagination_limit_offset(onchain_env):
    conn = _reopen(onchain_env)
    for i in range(5):
        nft_id = CHAR1[:-1] + str(i)
        _seed_character(conn, nft_id, SELLER, i)
        _seed_listing(
            conn,
            offer_index=chr(ord("A") + i) * 64,
            nft_id=nft_id,
            amount_drops=(i + 1) * 1_000_000,
            created_ts=1000 + i,
        )
    conn.commit()
    conn.close()

    req = _mocked_request("GET", "/api/market/listings?limit=2&offset=1")
    resp = _run(server.handle_market_listings(req))
    body = _run(_read_json(resp))
    assert len(body["rows"]) == 2
    # price_asc default: skip the cheapest (offset=1), take next 2
    assert body["rows"][0]["amount_drops"] == 2_000_000
    assert body["rows"][1]["amount_drops"] == 3_000_000


def test_browse_cache_hit_once_across_filters(onchain_env, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn)
    conn.commit()
    conn.close()

    req1 = _mocked_request("GET", "/api/market/listings?min_xrp=0.5")
    resp1 = _run(server.handle_market_listings(req1))
    assert resp1.status == 200

    def _boom(*args, **kwargs):
        raise AssertionError("the store must not be hit again on a cache hit")

    monkeypatch.setattr(server, "_compute_market_rows", _boom)

    req2 = _mocked_request("GET", "/api/market/listings?max_xrp=5&sort=newest&limit=1")
    resp2 = _run(server.handle_market_listings(req2))
    assert resp2.status == 200


def test_browse_cache_cardinality_bounded(onchain_env):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn)
    conn.commit()
    conn.close()

    for i in range(30):
        req = _mocked_request(
            "GET", f"/api/market/listings?min_xrp=0.{i + 1}&limit={(i % 90) + 1}&offset={i}"
        )
        resp = _run(server.handle_market_listings(req))
        assert resp.status == 200
    # only (network, kind) combos exist as keys -- one network x <=2 kinds here
    assert len(server._MARKET_CACHE) <= 2


def test_browse_cache_expires_after_ttl(onchain_env, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn)
    conn.commit()
    conn.close()

    req1 = _mocked_request("GET", "/api/market/listings")
    resp1 = _run(server.handle_market_listings(req1))
    assert resp1.status == 200

    real_monotonic = time.monotonic
    monkeypatch.setattr(server.time, "monotonic", lambda: real_monotonic() + 3600)

    calls = {"n": 0}
    real_compute = server._compute_market_rows

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(server, "_compute_market_rows", _counting)
    req2 = _mocked_request("GET", "/api/market/listings")
    resp2 = _run(server.handle_market_listings(req2))
    assert resp2.status == 200
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# GET /api/market/mine
# ---------------------------------------------------------------------------


def test_mine_requires_wallet_401(onchain_env, monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", False)
    req = _mocked_request("GET", "/api/market/mine")
    resp = _run(server.handle_market_mine(req))
    assert resp.status == 401


def test_mine_returns_four_groups(onchain_env, monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    from webapp import mock_economy

    monkeypatch.setattr(mock_economy, "DEV_OWNER", SELLER)

    conn = _reopen(onchain_env)
    # a listed character (should show in listings, NOT unlisted_characters)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, offer_index="A" * 64, nft_id=CHAR1, kind="character")
    # an unlisted character the caller still owns
    _seed_character(conn, CHAR3_UNLISTED, SELLER, 3)
    # a listed trait token (should show in listings, NOT unlisted_trait_tokens)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    _seed_listing(
        conn,
        offer_index="B" * 64,
        nft_id=TRAIT1,
        kind="trait",
        slot="Hat",
        value="Wizard Hat",
        amount_drops=500_000,
    )
    # an unlisted trait token the caller still owns
    upsert_trait_token(conn, TRAIT2_UNLISTED, SELLER, "Eyes", "Hypno")
    # loose Closet assets
    set_closet_contents(conn, SELLER, [("Mouth", "Grin", 2)], [])
    conn.commit()
    conn.close()

    req = _mocked_request("GET", "/api/market/mine")
    resp = _run(server.handle_market_mine(req))
    assert resp.status == 200
    body = _run(_read_json(resp))

    listed_ids = {r["nft_id"] for r in body["listings"]}
    assert listed_ids == {CHAR1, TRAIT1}

    unlisted_char_ids = {c["nft_id"] for c in body["unlisted_characters"]}
    assert unlisted_char_ids == {CHAR3_UNLISTED}

    unlisted_trait_ids = {t["nft_id"] for t in body["unlisted_trait_tokens"]}
    assert unlisted_trait_ids == {TRAIT2_UNLISTED}

    assert body["closet_assets"] == [{"slot": "Mouth", "value": "Grin", "count": 2}]


# ---------------------------------------------------------------------------
# GET /api/market/history
# ---------------------------------------------------------------------------


def test_history_requires_nft_id_or_slot_value_400(onchain_env):
    req = _mocked_request("GET", "/api/market/history")
    resp = _run(server.handle_market_history(req))
    assert resp.status == 400


def test_history_by_nft_id_excludes_transfer(onchain_env, monkeypatch, tmp_path):
    history_path = str(tmp_path / "history_testnet.db")
    monkeypatch.setenv("HISTORY_DB_PATH", history_path)
    hconn = init_history_db(history_path)
    events = [
        ("tx1", CHAR1, "sale", 1, 1000),
        ("tx2", CHAR1, "offer_create", 2, 1001),
        ("tx3", CHAR1, "offer_cancel", 3, 1002),
        ("tx4", CHAR1, "transfer", 4, 1003),
    ]
    for tx_hash, nft_id, event, ledger_index, ts in events:
        insert_nft_event(
            hconn,
            {
                "tx_hash": tx_hash,
                "nft_id": nft_id,
                "nft_number": 1,
                "event": event,
                "from_addr": SELLER,
                "to_addr": BUYER,
                "price_drops": 1_000_000,
                "price_token": None,
                "ledger_index": ledger_index,
                "ts": ts,
            },
        )
    hconn.commit()
    hconn.close()

    req = _mocked_request("GET", f"/api/market/history?nft_id={CHAR1}")
    resp = _run(server.handle_market_history(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    seen_events = {e["event"] for e in body["events"]}
    assert seen_events == {"sale", "offer_create", "offer_cancel"}
    assert "transfer" not in seen_events
    # ORDER BY ledger_index DESC
    assert [e["ledger_index"] for e in body["events"]] == [3, 2, 1]


def test_history_by_slot_value_returns_sold_listings(onchain_env):
    conn = _reopen(onchain_env)
    _seed_listing(
        conn,
        offer_index="D" * 64,
        nft_id=TRAIT1,
        kind="trait",
        slot="Hat",
        value="Wizard Hat",
        amount_drops=500_000,
        is_live=0,
        closed_reason="sold",
        created_ts=1000,
    )
    # a cancelled trait listing for the same slot/value must NOT appear
    _seed_listing(
        conn,
        offer_index="E" * 64,
        nft_id=TRAIT2_UNLISTED,
        kind="trait",
        slot="Hat",
        value="Wizard Hat",
        amount_drops=600_000,
        is_live=0,
        closed_reason="cancelled",
        created_ts=1001,
    )
    conn.commit()
    conn.close()

    req = _mocked_request("GET", "/api/market/history?slot=Hat&value=" + quote("Wizard Hat"))
    resp = _run(server.handle_market_history(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert len(body["sales"]) == 1
    assert body["sales"][0]["nft_id"] == TRAIT1
    assert body["sales"][0]["amount_xrp"] == "0.5"
