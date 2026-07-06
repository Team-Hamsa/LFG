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
    set_closet_token,
    upsert_trait_token,
)
from lfg_core.history_store import init_history_db, insert_nft_event  # noqa: E402
from lfg_core.market_store import (
    MarketListing,  # noqa: E402
    upsert_listing,  # noqa: E402
)
from lfg_core.market_store import get_listing as market_get_listing  # noqa: E402
from lfg_core.market_store import init_db as init_market_db  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
from lfg_core.nft_index import upsert as upsert_onchain_nft  # noqa: E402
from lfg_service import app as server  # noqa: E402
from webapp import mock_economy  # noqa: E402

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
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()
    yield onchain_path
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()


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


@pytest.mark.parametrize("param", ["min_xrp", "max_xrp"])
@pytest.mark.parametrize("bad_value", ["Infinity", "nan", "-nan"])
def test_browse_non_finite_xrp_400(onchain_env, param, bad_value):
    """Decimal("Infinity")/("nan") slip past xrp_to_drops_str's <= 0 guard and
    raise OverflowError/decimal.InvalidOperation instead of ValueError — on
    this public unauthenticated endpoint that was an uncaught 500 (same review
    finding already fixed for the list handler)."""
    req = _mocked_request("GET", f"/api/market/listings?{param}={bad_value}")
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
    monkeypatch.setattr(server, "_use_market_mock", lambda: False)
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


# ---------------------------------------------------------------------------
# Split-network topology (XRPL_NETWORK != ECONOMY_NETWORK)
# ---------------------------------------------------------------------------
# The deployed topology runs the app on mainnet (XRPL_NETWORK=mainnet) while
# the trait economy stays testnet-gated (ECONOMY_NETWORK=testnet). Everything
# trait-economy-backed (trait listings + trait_tokens join, unlisted trait
# tokens, loose Closet assets, sold-trait history) must resolve its onchain db
# via ECONOMY_NETWORK; everything character-backed stays on XRPL_NETWORK.
# These tests seed trait data ONLY in the economy-network db and character
# data ONLY in the XRPL-network db, so any handler reading the wrong network
# comes back empty and fails the assertion.

TRAIT3_SOLD = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C7000000a3"


@pytest.fixture
def split_network_env(tmp_path, monkeypatch):
    paths = {
        "mainnet": str(tmp_path / "onchain_mainnet.db"),
        "testnet": str(tmp_path / "onchain_testnet.db"),
    }
    for p in paths.values():
        _init_onchain(p).close()

    # character data ONLY in the XRPL (mainnet) db
    conn = _reopen(paths["mainnet"])
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, offer_index="A" * 64, nft_id=CHAR1, kind="character")
    _seed_character(conn, CHAR3_UNLISTED, SELLER, 3)
    conn.commit()
    conn.close()

    # trait data ONLY in the economy (testnet) db
    conn = _reopen(paths["testnet"])
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
    upsert_trait_token(conn, TRAIT2_UNLISTED, SELLER, "Eyes", "Hypno")
    set_closet_contents(conn, SELLER, [("Mouth", "Grin", 2)], [])
    _seed_listing(
        conn,
        offer_index="F" * 64,
        nft_id=TRAIT3_SOLD,
        kind="trait",
        slot="Hat",
        value="Wizard Hat",
        amount_drops=700_000,
        is_live=0,
        closed_reason="sold",
    )
    conn.commit()
    conn.close()

    # ONCHAIN_DB_PATH would collapse both networks onto one file, so route
    # per-network paths through index_db_path directly instead.
    monkeypatch.delenv("ONCHAIN_DB_PATH", raising=False)
    monkeypatch.setattr(server.nft_index, "index_db_path", lambda network: paths[network])
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "mainnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    server._MARKET_CACHE.clear()
    yield paths
    server._MARKET_CACHE.clear()


def test_split_network_browse_per_kind_networks(split_network_env):
    # trait browse must read the ECONOMY_NETWORK (testnet) db
    req = _mocked_request("GET", "/api/market/listings?kind=trait")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert [r["nft_id"] for r in body["rows"]] == [TRAIT1]

    # character browse still reads the XRPL_NETWORK (mainnet) db
    req2 = _mocked_request("GET", "/api/market/listings?kind=character")
    resp2 = _run(server.handle_market_listings(req2))
    body2 = _run(_read_json(resp2))
    assert [r["nft_id"] for r in body2["rows"]] == [CHAR1]

    # cache keys derive network per kind; cardinality bound unchanged
    # (<= 2 networks x 2 kinds)
    assert set(server._MARKET_CACHE) == {("testnet", "trait"), ("mainnet", "character")}
    assert len(server._MARKET_CACHE) <= 4


def test_split_network_mine_all_four_groups(split_network_env, monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "_use_market_mock", lambda: False)
    from webapp import mock_economy

    monkeypatch.setattr(mock_economy, "DEV_OWNER", SELLER)

    req = _mocked_request("GET", "/api/market/mine")
    resp = _run(server.handle_market_mine(req))
    assert resp.status == 200
    body = _run(_read_json(resp))

    assert {r["nft_id"] for r in body["listings"]} == {CHAR1, TRAIT1}
    assert {c["nft_id"] for c in body["unlisted_characters"]} == {CHAR3_UNLISTED}
    assert {t["nft_id"] for t in body["unlisted_trait_tokens"]} == {TRAIT2_UNLISTED}
    assert body["closet_assets"] == [{"slot": "Mouth", "value": "Grin", "count": 2}]


def test_split_network_history_slot_value_reads_economy_db(split_network_env):
    req = _mocked_request("GET", "/api/market/history?slot=Hat&value=" + quote("Wizard Hat"))
    resp = _run(server.handle_market_history(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert len(body["sales"]) == 1
    assert body["sales"][0]["nft_id"] == TRAIT3_SOLD
    assert body["sales"][0]["amount_drops"] == 700_000


# ---------------------------------------------------------------------------
# Task 8: POST/GET /api/market/{list,cancel,buy} session handlers
# ---------------------------------------------------------------------------
# Dev-mode auth bypass wired to a known wallet, mirroring
# tests/test_swap_cross_body_api.py's convention — the established way app.py
# handlers are exercised directly (no full aiohttp TestClient fixture for
# these routes in the repo). require_auth's dev-mode branch always sets
# request["user"] = {"id": "dev", ...}, so every session below is created
# with discord_id="dev" to match.


def _post_request(path, body):
    req = make_mocked_request("POST", path)

    async def _json():
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


class _StatusReq:
    """Minimal GET-status request stand-in (match_info + a settable
    per-request store), mirroring tests/test_service_firehose.py's _Req."""

    headers: dict = {}

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self._store = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


@pytest.fixture
def market_wallet(monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", SELLER)
    # WEBAPP_DEV_MODE=True above is only for require_wallet's dev-mode wallet
    # injection (request["wallet"] = mock_economy.DEV_OWNER); these tests
    # exercise the REAL market handler logic (Task 10 added a mock-market
    # substitution gated on the same flag — see app._use_market_mock's
    # docstring), so pin that substitution off independently.
    monkeypatch.setattr(server, "_use_market_mock", lambda: False)
    server.market_sessions.clear()
    yield
    server.market_sessions.clear()


def _fake_payload(qr="https://qr", url="https://xumm.app/sign/U1", pl_uuid="U1"):
    async def fake(*args, **kwargs):
        return {"qr_url": qr, "xumm_url": url, "uuid": pl_uuid}

    return fake


def _fake_status(*, signed, expired=False, txid=None):
    async def fake(_uuid):
        return {"opened": True, "signed": signed, "expired": expired, "txid": txid}

    return fake


def _sell_offer_meta(nft_id, offer_index, amount_drops):
    return {
        "TransactionResult": "tesSUCCESS",
        "AffectedNodes": [
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenOffer",
                    "LedgerIndex": offer_index,
                    "NewFields": {
                        "NFTokenID": nft_id,
                        "Amount": str(amount_drops),
                        "Flags": 1,
                    },
                }
            }
        ],
    }


# --- POST /api/market/list ---


def test_list_start_success_returns_session(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    conn.commit()
    conn.close()

    monkeypatch.setattr(server.xumm_ops, "create_sell_offer_payload", _fake_payload())
    req = _post_request("/api/market/list", {"nft_id": CHAR1, "price_xrp": "5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == "awaiting_signature"
    assert body["qr_url"] == "https://qr"
    assert body["xumm_url"] == "https://xumm.app/sign/U1"
    assert len(server.market_sessions) == 1


def test_list_start_not_owner_409(onchain_env, market_wallet):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, "rSomeoneElse00000000000000000000", 1)
    conn.commit()
    conn.close()

    req = _post_request("/api/market/list", {"nft_id": CHAR1, "price_xrp": "5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 409


def test_list_start_already_listed_409(onchain_env, market_wallet):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn)
    conn.commit()
    conn.close()

    req = _post_request("/api/market/list", {"nft_id": CHAR1, "price_xrp": "5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 409


def test_list_start_trait_owner_ok(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    conn.commit()
    conn.close()

    monkeypatch.setattr(server.xumm_ops, "create_sell_offer_payload", _fake_payload())
    req = _post_request("/api/market/list", {"nft_id": TRAIT1, "price_xrp": "0.5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 200


@pytest.mark.parametrize("bad_price", ["abc", "0", "-1", "1.1234567", "Infinity", "nan", "-nan"])
def test_list_start_bad_price_400(onchain_env, market_wallet, bad_price):
    req = _post_request("/api/market/list", {"nft_id": CHAR1, "price_xrp": bad_price})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 400


def test_list_start_nonstring_price_400(onchain_env, market_wallet):
    req = _post_request("/api/market/list", {"nft_id": CHAR1, "price_xrp": 5})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 400


def test_list_start_xumm_unreachable_502(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    conn.commit()
    conn.close()

    async def fake_none(*args, **kwargs):
        return None

    monkeypatch.setattr(server.xumm_ops, "create_sell_offer_payload", fake_none)
    req = _post_request("/api/market/list", {"nft_id": CHAR1, "price_xrp": "5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 502


# --- GET /api/market/list/{session_id} (finalize) ---


def _make_list_session(**overrides):
    base = {
        "discord_id": "dev",
        "wallet_address": SELLER,
        "nft_id": CHAR1,
        "listing_kind": "character",
        "amount_drops": 1_000_000,
    }
    base.update(overrides)
    s = server.market_flow.ListSession(**base)
    s.payload_uuid = "U1"
    server.market_sessions[s.id] = s
    return s


def test_list_status_not_found_404(onchain_env, market_wallet):
    resp = _run(server.handle_market_list_status(_StatusReq("nope")))
    assert resp.status == 404


def test_list_status_pending_no_write(onchain_env, market_wallet, monkeypatch):
    s = _make_list_session()
    monkeypatch.setattr(server.xumm_ops, "get_payload_status", _fake_status(signed=False))
    resp = _run(server.handle_market_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "awaiting_signature"

    conn = _reopen(onchain_env)
    count = conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0]
    assert count == 0


def test_list_status_signed_not_validated_pending_no_write(onchain_env, market_wallet, monkeypatch):
    s = _make_list_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": False}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    resp = _run(server.handle_market_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "pending"

    conn = _reopen(onchain_env)
    count = conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0]
    assert count == 0


def test_list_status_tx_lookup_raises_unknown_no_crash(onchain_env, market_wallet, monkeypatch):
    s = _make_list_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def boom(_hash):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(server.xrpl_ops, "get_tx", boom)
    resp = _run(server.handle_market_list_status(_StatusReq(s.id)))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == "unknown"

    conn = _reopen(onchain_env)
    count = conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0]
    assert count == 0


def test_list_status_ten_pending_polls_flips_unknown(onchain_env, market_wallet, monkeypatch):
    s = _make_list_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": False}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    for _ in range(server.market_flow.MAX_FINALIZE_POLLS):
        resp = _run(server.handle_market_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "unknown"


def test_list_status_validated_success_writes_row(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    conn.commit()
    conn.close()

    s = _make_list_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )
    meta = _sell_offer_meta(CHAR1, "A" * 64, 1_000_000)

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": meta}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    resp = _run(server.handle_market_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "done"
    assert body["offer_index"] == "A" * 64

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row is not None
    assert row["nft_id"] == CHAR1
    assert row["kind"] == "character"
    assert row["seller"] == SELLER
    assert row["amount_drops"] == 1_000_000
    assert row["is_live"] == 1


def test_list_status_idempotent_vs_listener_echo(onchain_env, market_wallet, monkeypatch):
    """A listener that already wrote the same offer_index (with real
    created_ledger/created_ts) must converge to exactly one row with the
    finalize write — the finalize side passes None for those fields, which
    upsert_listing's COALESCE preserves."""
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    upsert_listing(
        conn,
        MarketListing(
            offer_index="A" * 64,
            nft_id=CHAR1,
            kind="character",
            seller=SELLER,
            amount_drops=1_000_000,
            created_ledger=555,
            created_ts=9999,
        ),
    )
    conn.commit()
    conn.close()

    s = _make_list_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )
    meta = _sell_offer_meta(CHAR1, "A" * 64, 1_000_000)

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": meta}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    _run(server.handle_market_list_status(_StatusReq(s.id)))

    conn = _reopen(onchain_env)
    count = conn.execute(
        "SELECT COUNT(*) FROM market_listings WHERE offer_index=?", ("A" * 64,)
    ).fetchone()[0]
    assert count == 1
    row = market_get_listing(conn, "A" * 64)
    assert row["created_ledger"] == 555  # preserved from the listener write
    assert row["created_ts"] == 9999


# --- POST /api/market/cancel ---


def test_cancel_start_success(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    monkeypatch.setattr(server.xumm_ops, "create_cancel_offer_payload", _fake_payload())
    req = _post_request("/api/market/cancel", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_cancel_start(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == "awaiting_signature"


def test_cancel_start_foreign_seller_403(onchain_env, market_wallet):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, "rSomeoneElse00000000000000000000", 1)
    _seed_listing(conn, seller="rSomeoneElse00000000000000000000")
    conn.commit()
    conn.close()

    req = _post_request("/api/market/cancel", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_cancel_start(req))
    assert resp.status == 403


def test_cancel_start_unknown_offer_404(onchain_env, market_wallet):
    req = _post_request("/api/market/cancel", {"offer_index": "Z" * 64})
    resp = _run(server.handle_market_cancel_start(req))
    assert resp.status == 404


def test_cancel_start_dead_listing_404(onchain_env, market_wallet):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.close()
    conn = _reopen(onchain_env)
    from lfg_core.market_store import close_listing

    close_listing(conn, "A" * 64, "cancelled")
    conn.commit()
    conn.close()

    req = _post_request("/api/market/cancel", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_cancel_start(req))
    assert resp.status == 404


def _make_cancel_session(**overrides):
    base = {
        "discord_id": "dev",
        "wallet_address": SELLER,
        "offer_index": "A" * 64,
        "network": "testnet",
    }
    base.update(overrides)
    s = server.market_flow.CancelSession(**base)
    s.payload_uuid = "U1"
    server.market_sessions[s.id] = s
    return s


def test_cancel_status_signed_closes_row(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    s = _make_cancel_session()
    monkeypatch.setattr(server.xumm_ops, "get_payload_status", _fake_status(signed=True))
    resp = _run(server.handle_market_cancel_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "done"

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 0
    assert row["closed_reason"] == "cancelled"


# --- POST /api/market/buy ---


def test_buy_start_unknown_offer_404(onchain_env, market_wallet):
    req = _post_request("/api/market/buy", {"offer_index": "Z" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 404


def test_buy_start_dead_listing_410(onchain_env, market_wallet):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()
    conn = _reopen(onchain_env)
    from lfg_core.market_store import close_listing

    close_listing(conn, "A" * 64, "cancelled")
    conn.commit()
    conn.close()

    req = _post_request("/api/market/buy", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 410
    body = _run(_read_json(resp))
    assert body["error"] == "listing_unavailable"


def test_buy_start_closet_required_403(onchain_env, market_wallet, monkeypatch):
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BUYER)
    conn = _reopen(onchain_env)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    _seed_listing(conn, nft_id=TRAIT1, kind="trait", seller=SELLER, slot="Hat", value="Wizard Hat")
    conn.commit()
    conn.close()

    req = _post_request("/api/market/buy", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 403
    body = _run(_read_json(resp))
    assert body["error"] == "closet_required"


def test_buy_start_verify_false_410_and_marks_stale(onchain_env, market_wallet, monkeypatch):
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BUYER)
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    async def fake_verify(*args, **kwargs):
        return False

    monkeypatch.setattr(server.market_ops, "verify_sell_offer", fake_verify)
    req = _post_request("/api/market/buy", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 410
    body = _run(_read_json(resp))
    assert body["error"] == "listing_unavailable"

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 0
    assert row["closed_reason"] == "stale"


def test_buy_start_verify_lookup_failure_503_row_untouched(onchain_env, market_wallet, monkeypatch):
    """Fix #3: a verify LOOKUP failure (RPC/soft-error, verify raises) must NOT
    stale-close a possibly-healthy listing — respond 503 with no DB write. Only
    a successful lookup that finds the offer genuinely absent may stale-close."""
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BUYER)
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    async def boom(*args, **kwargs):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(server.market_ops, "verify_sell_offer", boom)
    req = _post_request("/api/market/buy", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 503

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 1  # untouched — still live
    assert row["closed_reason"] is None


def test_buy_start_self_buy_400_row_untouched(onchain_env, market_wallet, monkeypatch):
    """Fix #2: buying your own listing is rejected up front (400) with no DB
    write and no verify/sign spent."""
    monkeypatch.setattr(mock_economy, "DEV_OWNER", SELLER)  # buyer == seller
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    req = _post_request("/api/market/buy", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 400

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 1
    assert row["closed_reason"] is None


def test_buy_start_happy_path_returns_accept_payload_with_price(
    onchain_env, market_wallet, monkeypatch
):
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BUYER)
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER, amount_drops=2_000_000)
    conn.commit()
    conn.close()

    async def fake_verify(*args, **kwargs):
        return True

    monkeypatch.setattr(server.market_ops, "verify_sell_offer", fake_verify)
    monkeypatch.setattr(server.xumm_ops, "create_accept_offer_payload", _fake_payload())
    req = _post_request("/api/market/buy", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == "awaiting_signature"
    assert "2" in body["instruction"]


def _make_buy_session(**overrides):
    base = {
        "discord_id": "dev",
        "wallet_address": BUYER,
        "offer_index": "A" * 64,
        "nft_id": CHAR1,
        "listing_kind": "character",
        "network": "testnet",
        "amount_drops": 1_000_000,
    }
    base.update(overrides)
    s = server.market_flow.BuySession(**base)
    s.payload_uuid = "U1"
    server.market_sessions[s.id] = s
    return s


def test_buy_status_success_marks_sold(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    s = _make_buy_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    resp = _run(server.handle_market_buy_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "done"

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 0
    assert row["closed_reason"] == "sold"


def test_buy_status_ledger_race_failure_maps_reason(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    s = _make_buy_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": {"TransactionResult": "tecOBJECT_NOT_FOUND"}}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    resp = _run(server.handle_market_buy_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body == {
        "id": s.id,
        "platform": "discord",
        "state": "failed",
        "error": body["error"],
        "reason": "listing_unavailable",
        "qr_url": None,
        "xumm_url": None,
        "instruction": None,
        "offer_index": "A" * 64,
    }

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 0
    assert row["closed_reason"] == "stale"


def test_buy_status_trait_purchase_triggers_settlement_seam(
    onchain_env, market_wallet, monkeypatch
):
    """Task 9: a validated trait buy wires into _settle_trait_sale with the
    buyer/nft_id/offer_index/network — the real settlement behavior (mocked
    EconomyDeps, mark_settled, the sweep) is covered end-to-end in
    tests/test_market_trait_flow.py; this pins the buy-status call site."""
    conn = _reopen(onchain_env)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    _seed_listing(conn, nft_id=TRAIT1, kind="trait", seller=SELLER, slot="Hat", value="Wizard Hat")
    conn.commit()
    conn.close()

    s = _make_buy_session(nft_id=TRAIT1, listing_kind="trait")
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    calls = []

    async def fake_settle(buyer, nft_id, offer_index, network):
        calls.append((buyer, nft_id, offer_index, network))
        return True

    monkeypatch.setattr(server, "_settle_trait_sale", fake_settle)
    _run(server.handle_market_buy_status(_StatusReq(s.id)))
    assert calls == [(s.wallet_address, TRAIT1, "A" * 64, "testnet")]


def test_buy_status_character_purchase_does_not_trigger_settlement(
    onchain_env, market_wallet, monkeypatch
):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    s = _make_buy_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)

    async def boom(buyer, nft_id, offer_index, network):
        raise AssertionError("must not trigger settlement for a character sale")

    monkeypatch.setattr(server, "_settle_trait_sale", boom)
    resp = _run(server.handle_market_buy_status(_StatusReq(s.id)))
    assert resp.status == 200


def test_buy_status_insufficient_funds_fails_session_leaves_row_live(
    onchain_env, market_wallet, monkeypatch
):
    """Fix #2: a buyer-side failure (tecINSUFFICIENT_FUNDS) fails the session
    but must leave the listing live — only offer-consumed/absent codes may
    stale-close (otherwise a broke buyer griefs any listing)."""
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_listing(conn, seller=SELLER)
    conn.commit()
    conn.close()

    s = _make_buy_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": {"TransactionResult": "tecINSUFFICIENT_FUNDS"}}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    resp = _run(server.handle_market_buy_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "failed"

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 1  # still live
    assert row["closed_reason"] is None


# ---------------------------------------------------------------------------
# Fix #1: finalize write must not resurrect a listener-closed listing
# ---------------------------------------------------------------------------


def test_list_status_finalize_after_listener_sold_does_not_resurrect(
    onchain_env, market_wallet, monkeypatch
):
    """The seller's app was backgrounded through the whole buy; the listener
    already closed the row sold/settled=0. A late finalize poll must NOT flip
    it back to live/NULL (phantom listing + broken settlement predicate)."""
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    # Listener path: created the live row, then observed the sale + closed it.
    upsert_listing(
        conn,
        MarketListing(
            offer_index="A" * 64,
            nft_id=CHAR1,
            kind="character",
            seller=SELLER,
            amount_drops=1_000_000,
            created_ledger=555,
            created_ts=9999,
        ),
    )
    from lfg_core.market_store import close_listing

    close_listing(conn, "A" * 64, "sold")
    conn.commit()
    conn.close()

    s = _make_list_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )
    meta = _sell_offer_meta(CHAR1, "A" * 64, 1_000_000)

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": meta}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    _run(server.handle_market_list_status(_StatusReq(s.id)))

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row["is_live"] == 0  # NOT resurrected
    assert row["closed_reason"] == "sold"


def test_list_status_finalize_before_listener_creates_live_row(
    onchain_env, market_wallet, monkeypatch
):
    """No listener echo yet: the finalize write must still create the row,
    live, with its kind (test (b))."""
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    conn.commit()
    conn.close()

    s = _make_list_session()
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )
    meta = _sell_offer_meta(CHAR1, "A" * 64, 1_000_000)

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": meta}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    _run(server.handle_market_list_status(_StatusReq(s.id)))

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "A" * 64)
    assert row is not None
    assert row["is_live"] == 1
    assert row["kind"] == "character"


# ---------------------------------------------------------------------------
# Fix #4: ECONOMY_ENABLED gates trait-kind market ops (character unaffected)
# ---------------------------------------------------------------------------


def test_list_start_trait_blocked_when_economy_disabled(onchain_env, market_wallet, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)
    conn = _reopen(onchain_env)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    conn.commit()
    conn.close()

    req = _post_request("/api/market/list", {"nft_id": TRAIT1, "price_xrp": "5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 403
    body = _run(_read_json(resp))
    assert body["code"] == "economy_disabled"


def test_list_start_character_ok_when_economy_disabled(onchain_env, market_wallet, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    conn.commit()
    conn.close()

    monkeypatch.setattr(server.xumm_ops, "create_sell_offer_payload", _fake_payload())
    req = _post_request("/api/market/list", {"nft_id": CHAR1, "price_xrp": "5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 200


def test_buy_start_trait_blocked_when_economy_disabled(onchain_env, market_wallet, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BUYER)
    conn = _reopen(onchain_env)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    _seed_listing(conn, nft_id=TRAIT1, kind="trait", seller=SELLER, slot="Hat", value="Wizard Hat")
    conn.commit()
    conn.close()

    req = _post_request("/api/market/buy", {"offer_index": "A" * 64})
    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 403
    body = _run(_read_json(resp))
    assert body["code"] == "economy_disabled"


def test_browse_trait_empty_when_economy_disabled(onchain_env, market_wallet, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)
    conn = _reopen(onchain_env)
    upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    _seed_listing(conn, nft_id=TRAIT1, kind="trait", seller=SELLER, slot="Hat", value="Wizard Hat")
    conn.commit()
    conn.close()

    req = _mocked_request("GET", "/api/market/listings?kind=trait")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["rows"] == []


# ---------------------------------------------------------------------------
# Fix #5: market 409s carry the active session dict (mint parity)
# ---------------------------------------------------------------------------


def test_list_start_409_carries_active_session(onchain_env, market_wallet, monkeypatch):
    conn = _reopen(onchain_env)
    _seed_character(conn, CHAR1, SELLER, 1)
    _seed_character(conn, CHAR2, SELLER, 2)
    conn.commit()
    conn.close()

    # First list: leaves an awaiting_signature session in the map.
    existing = _make_list_session(discord_id="dev")
    existing.state = server.market_flow.AWAITING_SIGNATURE

    monkeypatch.setattr(server.xumm_ops, "create_sell_offer_payload", _fake_payload())
    req = _post_request("/api/market/list", {"nft_id": CHAR2, "price_xrp": "5"})
    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 409
    body = _run(_read_json(resp))
    assert body["session"]["id"] == existing.id
    assert body["session"]["state"] == "awaiting_signature"


def test_closet_active_helper_checks_status(onchain_env, market_wallet):
    conn = _reopen(onchain_env)
    set_closet_token(conn, BUYER, "closet-nft-id", "hex", status="active")
    conn.commit()
    conn.close()
    assert server._closet_active("testnet", BUYER) is True
    assert server._closet_active("testnet", "rNoCloset000000000000000000000000") is False
