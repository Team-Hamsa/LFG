# tests/test_shop_endpoints.py
# Task 8: service endpoints for the Trait Shop — GET /api/shop/catalog
# (public, cached) and POST/GET /api/shop/buy (authed, drives shop_flow).
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
from dataclasses import dataclass  # noqa: E402

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import economy_store as es  # noqa: E402
from lfg_core import rarity, shop_flow, shop_store  # noqa: E402
from lfg_core.closet_token import ACTIVE  # noqa: E402
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
from lfg_service import app as server  # noqa: E402
from webapp import mock_economy  # noqa: E402

BUYER = "rBuyerAddress000000000000000000000"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mocked_request(method, path):
    return make_mocked_request(method, path, app=web.Application())


async def _read_json(resp):
    return json.loads(resp.body.decode())


def _post_request(path, body):
    req = _mocked_request("POST", path)

    async def json_body():
        return body

    req.json = json_body
    return req


class _StatusReq:
    headers: dict = {}

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self._store = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _init_onchain(path):
    conn = init_onchain_db(path)
    es.init_economy_schema(conn)
    shop_store.ensure_schema(conn)
    conn.commit()
    return conn


def _init_app_db(path):
    conn = sqlite3.connect(path)
    rarity.ensure_schema(conn)
    conn.commit()
    conn.close()


def _seed_rarity(app_db_path, network, category, trait, live, enabled=1, shop_count=0):
    conn = sqlite3.connect(app_db_path)
    conn.execute(
        "INSERT INTO trait_rarity (network, body, category, trait, live_count,"
        " floor_weight, enabled, shop_count) VALUES (?,?,?,?,?,0.005,?,?)",
        (network, "male", category, trait, live, enabled, shop_count),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def onchain_env(tmp_path, monkeypatch):
    onchain_path = str(tmp_path / "onchain_testnet.db")
    conn = _init_onchain(onchain_path)
    conn.commit()
    conn.close()
    app_db = str(tmp_path / "lfg_nfts_testnet.db")
    _init_app_db(app_db)
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(server.db_path, "app_db_path", lambda net=None: app_db)
    server._SHOP_CACHE.clear()
    server._SHOP_CACHE_GEN.clear()
    server.shop_sessions.clear()
    yield onchain_path, app_db
    server._SHOP_CACHE.clear()
    server._SHOP_CACHE_GEN.clear()
    server.shop_sessions.clear()


@pytest.fixture
def shop_wallet(monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BUYER)

    # #238: payment-path detection runs inside handle_shop_buy_start — stub it
    # to the BRIX path by default so endpoint tests never touch the network.
    async def _fake_detect(wallet, amount, **kw):
        return ("BRIX", amount)

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", _fake_detect)
    server.shop_sessions.clear()
    yield
    server.shop_sessions.clear()


def _activate_closet(onchain_path, owner):
    conn = sqlite3.connect(onchain_path)
    conn.row_factory = sqlite3.Row
    es.init_economy_schema(conn)
    es.set_closet_token(conn, owner, "CLOSETNFT" + "0" * 55, "uri_hex_placeholder", status=ACTIVE)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# GET /api/shop/catalog
# ---------------------------------------------------------------------------


def test_catalog_empty_when_economy_disabled(onchain_env, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)
    req = _mocked_request("GET", "/api/shop/catalog")
    resp = _run(server.handle_shop_catalog(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body == {"items": []}


def test_catalog_shape(onchain_env):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    req = _mocked_request("GET", "/api/shop/catalog")
    resp = _run(server.handle_shop_catalog(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["slot"] == "Head"
    assert item["value"] == "Wizard Hat"
    assert isinstance(item["price_brix"], int)
    assert isinstance(item["image_url"], str) and item["image_url"]


def test_catalog_excludes_disabled_traits(onchain_env):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Disabled Hat", live=4, enabled=0)
    req = _mocked_request("GET", "/api/shop/catalog")
    resp = _run(server.handle_shop_catalog(req))
    body = _run(_read_json(resp))
    assert body["items"] == []


def test_catalog_cache_reused_within_ttl(onchain_env, monkeypatch):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    req = _mocked_request("GET", "/api/shop/catalog")
    resp1 = _run(server.handle_shop_catalog(req))
    body1 = _run(_read_json(resp1))
    assert len(body1["items"]) == 1

    # Seed a second trait directly in the DB without invalidating the cache;
    # a cached read must not see it within the TTL window.
    _seed_rarity(app_db, "testnet", "Eyes", "Laser", live=4)
    resp2 = _run(server.handle_shop_catalog(req))
    body2 = _run(_read_json(resp2))
    assert len(body2["items"]) == 1  # still cached


# ---------------------------------------------------------------------------
# POST /api/shop/buy
# ---------------------------------------------------------------------------


def test_buy_economy_disabled_403(onchain_env, shop_wallet, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)
    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 403
    body = _run(_read_json(resp))
    assert body["code"] == "economy_disabled"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"slot": "Head"},
        {"value": "Wizard Hat"},
        {"slot": "", "value": "Wizard Hat"},
        {"slot": "Head", "value": ""},
        {"slot": 5, "value": "Wizard Hat"},
    ],
)
def test_buy_malformed_400(onchain_env, shop_wallet, body):
    req = _post_request("/api/shop/buy", body)
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 400


def test_buy_unknown_trait_404(onchain_env, shop_wallet):
    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Nonexistent"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 404
    body = _run(_read_json(resp))
    assert body["error"] == "unknown_trait"


def test_buy_not_purchasable_403(onchain_env, shop_wallet):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Disabled Hat", live=4, enabled=0)
    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Disabled Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 403
    body = _run(_read_json(resp))
    assert body["error"] == "not_purchasable"


def test_buy_closet_required_403(onchain_env, shop_wallet):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 403
    body = _run(_read_json(resp))
    assert body["error"] == "closet_required"


def test_buy_concurrent_session_409(onchain_env, shop_wallet, monkeypatch):
    """Two racing POST /api/shop/buy from the same wallet must not both start
    a session (duplicate-mint race, review fix #217): once the first has
    reached awaiting_accept, a second concurrent buy is rejected 409 with
    session_active and the existing session_id, mirroring the market buy
    endpoint's concurrent-session guard."""
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    _activate_closet(onchain_path, BUYER)

    async def fake_start_shop_buy(session, deps):
        session.state = shop_flow.AWAITING_ACCEPT
        session.accept = {"qr_url": "q", "xumm_url": "x", "uuid": "U1"}

    monkeypatch.setattr(server.shop_flow, "start_shop_buy", fake_start_shop_buy)

    async def scenario():
        req1 = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
        resp1 = await server.handle_shop_buy_start(req1)
        assert resp1.status == 200
        # Let the background task (asyncio.create_task in the handler) run to
        # completion so the first session is frozen in awaiting_accept before
        # the second request races in.
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if pending:
            await asyncio.gather(*pending)

        body1 = json.loads(resp1.body.decode())
        session = server.shop_sessions[body1["id"]]
        assert session.state == shop_flow.AWAITING_ACCEPT

        req2 = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
        resp2 = await server.handle_shop_buy_start(req2)
        return body1["id"], resp2

    existing_id, resp2 = _run(scenario())
    assert resp2.status == 409
    body2 = _run(_read_json(resp2))
    assert body2["code"] == "session_active"
    assert body2["session_id"] == existing_id
    # No duplicate session was created.
    assert len(server.shop_sessions) == 1


def test_buy_new_session_after_terminal_not_blocked(onchain_env, shop_wallet, monkeypatch):
    """A prior session that already reached a terminal state (done/failed)
    must not block a fresh buy from the same wallet."""
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    _activate_closet(onchain_path, BUYER)

    prior = _make_session(state=shop_flow.FAILED)
    server.shop_sessions[prior.id] = prior
    # Recent creation timestamp so _prune_shop_sessions doesn't sweep it away
    # before the guard gets a chance to look at it (it wasn't created via the
    # real POST path, so it has no entry by default).
    server._shop_session_created[prior.id] = time.time()

    async def fake_start_shop_buy(session, deps):
        session.state = shop_flow.AWAITING_ACCEPT
        session.accept = {"qr_url": "q", "xumm_url": "x", "uuid": "U1"}

    monkeypatch.setattr(server.shop_flow, "start_shop_buy", fake_start_shop_buy)

    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["id"] != prior.id
    assert len(server.shop_sessions) == 2


def test_buy_malformed_and_disabled_economy_disabled_takes_precedence(onchain_env, shop_wallet):
    """Regression lock (#217 review): when the body is malformed AND the
    economy is disabled, the economy_disabled check must fire first — 403,
    not 400 — since the fail-closed order in the handler checks
    ECONOMY_ENABLED before parsing/validating the request body."""
    server.config.ECONOMY_ENABLED = False
    try:
        req = _post_request("/api/shop/buy", {})  # malformed: missing slot/value
        resp = _run(server.handle_shop_buy_start(req))
        assert resp.status == 403
        body = _run(_read_json(resp))
        assert body["code"] == "economy_disabled"
    finally:
        server.config.ECONOMY_ENABLED = True


def test_buy_happy_path_returns_session(onchain_env, shop_wallet, monkeypatch):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    _activate_closet(onchain_path, BUYER)

    async def fake_start_shop_buy(session, deps):
        session.state = shop_flow.AWAITING_ACCEPT
        session.accept = {"qr_url": "q", "xumm_url": "x", "uuid": "U1"}

    monkeypatch.setattr(server.shop_flow, "start_shop_buy", fake_start_shop_buy)

    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert "id" in body
    assert isinstance(body["price_brix"], int)
    assert body["price_brix"] > 0
    assert len(server.shop_sessions) == 1

    # Background task hasn't necessarily run yet at this point (it's created
    # via create_task); give the loop a beat inside the same _run call chain
    # by awaiting the pending task directly is unnecessary here since we only
    # assert on the *response* shape, matching the brief.


def test_buy_detect_uses_shop_offer_currency_pair(onchain_env, shop_wallet, monkeypatch):
    """The balance check must run against the same currency pair the shop
    offer is denominated in (BRIX_CURRENCY_HEX/BRIX_ISSUER, per
    shop_flow.brix_amount) — not detect_payment_path's SWAP_OFFER_* defaults,
    or a buyer holding the swap-fee currency passes detection and then fails
    the accept on-ledger."""
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    _activate_closet(onchain_path, BUYER)

    seen = {}

    async def spy_detect(wallet, amount, **kw):
        seen.update(kw)
        return ("BRIX", amount)

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", spy_detect)

    async def fake_start_shop_buy(session, deps):
        session.state = shop_flow.AWAITING_ACCEPT

    monkeypatch.setattr(server.shop_flow, "start_shop_buy", fake_start_shop_buy)

    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 200
    assert seen.get("currency") == server.config.BRIX_CURRENCY_HEX
    assert seen.get("issuer") == server.config.BRIX_ISSUER


# ---------------------------------------------------------------------------
# GET /api/shop/buy/{session_id}
# ---------------------------------------------------------------------------


@dataclass
class _FakeDeps:
    pass


def _make_session(**overrides):
    base = {
        "buyer": BUYER,
        "slot": "Head",
        "value": "Wizard Hat",
        "price_brix": 10,
        "platform": "discord",
    }
    base.update(overrides)
    return shop_flow.ShopBuySession(**base)


def test_status_foreign_session_404(onchain_env, shop_wallet):
    other = _make_session(buyer="rSomeoneElse000000000000000000000")
    server.shop_sessions[other.id] = other
    resp = _run(server.handle_shop_buy_status(_StatusReq(other.id)))
    assert resp.status == 404


def test_status_not_found_404(onchain_env, shop_wallet):
    resp = _run(server.handle_shop_buy_status(_StatusReq("nope")))
    assert resp.status == 404


def test_status_advances_session(onchain_env, shop_wallet, monkeypatch):
    session = _make_session(state=shop_flow.AWAITING_ACCEPT)
    session.accept = {"qr_url": "q", "xumm_url": "x", "uuid": "U1"}
    session.nft_id = "NFT123"
    server.shop_sessions[session.id] = session

    async def fake_advance(sess, deps):
        sess.state = shop_flow.DONE

    monkeypatch.setattr(server.shop_flow, "advance_shop_buy", fake_advance)

    resp = _run(server.handle_shop_buy_status(_StatusReq(session.id)))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == shop_flow.DONE


def test_status_running_session_not_advanced(onchain_env, shop_wallet, monkeypatch):
    session = _make_session(state=shop_flow.RUNNING)
    server.shop_sessions[session.id] = session

    async def boom(sess, deps):
        raise AssertionError("must not advance a RUNNING (not-yet-awaiting-accept) session")

    monkeypatch.setattr(server.shop_flow, "advance_shop_buy", boom)

    resp = _run(server.handle_shop_buy_status(_StatusReq(session.id)))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == shop_flow.RUNNING


# ---------------------------------------------------------------------------
# #238 XRP payment fallback: detection + surfaced fields
# ---------------------------------------------------------------------------


def test_buy_response_includes_payment_path_fields(onchain_env, shop_wallet, monkeypatch):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    _activate_closet(onchain_path, BUYER)

    async def fake_detect(wallet, amount, **kw):
        assert wallet == BUYER
        return ("XRP", "0.123456")

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", fake_detect)

    async def fake_start_shop_buy(session, deps):
        session.state = shop_flow.AWAITING_ACCEPT
        session.accept = {"qr_url": "q", "xumm_url": "x", "uuid": "U1"}

    monkeypatch.setattr(server.shop_flow, "start_shop_buy", fake_start_shop_buy)

    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["pay_with"] == "XRP"
    assert body["price_xrp"] == "0.123456"

    # And the status poll surfaces the same fields (via to_dict).
    resp2 = _run(server.handle_shop_buy_status(_StatusReq(body["id"])))
    body2 = _run(_read_json(resp2))
    assert body2["pay_with"] == "XRP" and body2["price_xrp"] == "0.123456"


def test_buy_brix_path_fields(onchain_env, shop_wallet, monkeypatch):
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    _activate_closet(onchain_path, BUYER)

    async def fake_start_shop_buy(session, deps):
        session.state = shop_flow.AWAITING_ACCEPT
        session.accept = {"qr_url": "q", "xumm_url": "x", "uuid": "U1"}

    monkeypatch.setattr(server.shop_flow, "start_shop_buy", fake_start_shop_buy)

    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    body = _run(_read_json(resp))
    assert body["pay_with"] == "BRIX"
    assert body["price_xrp"] is None


def test_buy_pricing_unavailable_503_before_any_session(onchain_env, shop_wallet, monkeypatch):
    """AMM can't quote and the wallet holds no BRIX: fail cleanly BEFORE a
    session/order/mint exists."""
    onchain_path, app_db = onchain_env
    _seed_rarity(app_db, "testnet", "Head", "Wizard Hat", live=4)
    _activate_closet(onchain_path, BUYER)

    async def fake_detect(wallet, amount, **kw):
        raise RuntimeError("no quote")

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", fake_detect)

    async def boom(session, deps):
        raise AssertionError("start_shop_buy must not run when pricing is unavailable")

    monkeypatch.setattr(server.shop_flow, "start_shop_buy", boom)

    req = _post_request("/api/shop/buy", {"slot": "Head", "value": "Wizard Hat"})
    resp = _run(server.handle_shop_buy_start(req))
    assert resp.status == 503
    body = _run(_read_json(resp))
    assert body["code"] == "pricing_unavailable"
    assert server.shop_sessions == {}
