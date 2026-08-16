# tests/test_session_resume.py
# Issue #221: session resume for swap/economy/market/shop flows after a
# Discord-mobile Activity webview relaunch. Mirrors #216's mint resume:
# one consolidated GET /api/sessions/active endpoint returns every live
# (non-terminal) flow for the caller in one payload, and the market/economy
# session dicts gain an additive `kind` key so the relaunched client can
# route each resumed session to the right poller.
#
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants at import time (copy of tests/test_mint_active_resume.py's block).
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
import time  # noqa: E402

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import market_flow, mint_flow, shop_flow, swap_flow  # noqa: E402
from lfg_service import app as server  # noqa: E402
from webapp import economy_api, mock_economy  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _PlainRequest:
    """Mutable handler request double without aiohttp app-key warnings
    (copy of tests/test_mint_active_resume.py's)."""

    headers: dict[str, str] = {}

    def __init__(self):
        self._store = {}

    async def json(self):
        return {}

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value


def _request():
    return _PlainRequest()


async def _read_json(resp):
    return json.loads(resp.body.decode())


FLOWS = ("mint", "bulk", "swap", "market", "economy", "shop")


@pytest.fixture
def dev_auth(monkeypatch):
    """Dev-mode auth (user id 'dev', platform discord, wallet DEV_OWNER) with
    every session dict isolated."""
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "mint_sessions", {})
    monkeypatch.setattr(server, "bulk_sessions", {})
    monkeypatch.setattr(server, "swap_sessions", {})
    monkeypatch.setattr(server, "market_sessions", {})
    monkeypatch.setattr(server, "economy_sessions", {})
    monkeypatch.setattr(server, "shop_sessions", {})
    monkeypatch.setattr(server, "_shop_session_created", {})
    return server


# --- additive `kind` in the routable payloads --------------------------------


def _buy_session(**kw):
    defaults = {
        "discord_id": "dev",
        "wallet_address": "rBuyer",
        "offer_index": "OI",
        "nft_id": "00080000AA",
        "listing_kind": "character",
        "network": "testnet",
        "amount_drops": 1000,
    }
    defaults.update(kw)
    return market_flow.BuySession(**defaults)


def test_market_sessions_emit_kind():
    assert _buy_session().to_dict()["kind"] == "buy"
    ls = market_flow.ListSession(
        discord_id="dev",
        wallet_address="rS",
        nft_id="00080000AA",
        listing_kind="character",
        amount_drops=1000,
    )
    assert ls.to_dict()["kind"] == "list"
    cs = market_flow.CancelSession(
        discord_id="dev", wallet_address="rS", offer_index="OI", network="testnet"
    )
    assert cs.to_dict()["kind"] == "cancel"
    bs = market_flow.BidSession(
        discord_id="dev",
        wallet_address="rB",
        nft_id="00080000AA",
        owner="rOwner",
        amount_drops=1000,
    )
    assert bs.to_dict()["kind"] == "bid"
    ba = market_flow.BidAcceptSession(
        discord_id="dev",
        wallet_address="rS",
        offer_index="OI",
        nft_id="00080000AA",
        network="testnet",
        amount_drops=1000,
    )
    assert ba.to_dict()["kind"] == "bid_accept"


def test_buy_session_emits_listing_kind():
    """The client's resume path needs the LISTING kind to pick the right
    render (marketBuyRender is a per-listing-kind factory)."""
    d = _buy_session(listing_kind="trait").to_dict()
    assert d["listing_kind"] == "trait"


class _InnerOp:
    def __init__(self, state="running"):
        self.id = "inner1"
        self.state = state
        self.error = None
        self.slot = "Hat"
        self.value = "Wizard Hat"
        self.moved_assets = []
        self.accept = None
        self.new_nft_id = None


def test_economy_session_dict_emits_kind():
    d = economy_api.economy_session_dict("deposit", _InnerOp())
    assert d["kind"] == "deposit"
    inner = _InnerOp()
    inner.displaced = {}
    inner.resolution = None
    assert economy_api.economy_session_dict("equip", inner)["kind"] == "equip"


# --- GET /api/sessions/active ------------------------------------------------


def _mint_session(discord_id="dev", platform="discord", state=mint_flow.AWAITING_PAYMENT):
    s = mint_flow.MintSession(discord_id=discord_id, wallet_address="rTest", platform=platform)
    s.state = state
    return s


def _swap_session(discord_id="dev", platform="discord", state=swap_flow.AWAITING_PAYMENT):
    nft = {"name": "LFG #1", "image": "https://cdn/img.png"}
    s = swap_flow.SwapSession(
        discord_id=discord_id,
        wallet_address="rTest",
        nft1=dict(nft),
        nft2=dict(nft),
        traits_to_swap=["Hat"],
        platform=platform,
    )
    s.state = state
    return s


def _economy_session(discord_id="dev", platform="discord", state="running"):
    return economy_api.EconomyWebSession(
        discord_id=discord_id, kind="harvest", inner=_InnerOp(state=state), platform=platform
    )


def _shop_session(buyer=mock_economy.DEV_OWNER, platform="discord", state=shop_flow.RUNNING):
    s = shop_flow.ShopBuySession(
        buyer=buyer, slot="Hat", value="Wizard Hat", price_brix=5, platform=platform
    )
    s.state = state
    return s


def _active(dev_auth):
    resp = _run(server.handle_sessions_active(_request()))
    assert resp.status == 200
    return _run(_read_json(resp))


def test_route_registered(dev_auth):
    app = server.create_app()
    req = make_mocked_request("GET", "/api/sessions/active", app=web.Application())
    match = _run(app.router.resolve(req))
    assert getattr(match, "http_exception", None) is None
    assert match.handler is server.handle_sessions_active


def test_all_null_when_no_sessions(dev_auth):
    body = _active(dev_auth)
    assert set(body) == set(FLOWS)
    assert all(body[f] is None for f in FLOWS)


def test_returns_each_live_flow_under_its_key(dev_auth):
    m = _mint_session()
    server.mint_sessions[m.id] = m
    sw = _swap_session()
    server.swap_sessions[sw.id] = sw
    mk = _buy_session()
    server.market_sessions[mk.id] = mk
    ec = _economy_session()
    server.economy_sessions[ec.id] = ec
    sh = _shop_session()
    server.shop_sessions[sh.id] = sh
    server._shop_session_created[sh.id] = time.time()

    body = _active(dev_auth)
    assert body["mint"]["id"] == m.id
    assert body["swap"]["id"] == sw.id
    assert body["swap"]["state"] == swap_flow.AWAITING_PAYMENT
    assert body["market"]["id"] == mk.id
    assert body["market"]["kind"] == "buy"
    assert body["economy"]["id"] == ec.id
    assert body["economy"]["kind"] == "harvest"
    assert body["shop"]["id"] == sh.id
    assert body["bulk"] is None


def test_terminal_sessions_omitted(dev_auth):
    for state in swap_flow.TERMINAL_STATES:
        s = _swap_session(state=state)
        server.swap_sessions[s.id] = s
    mk = _buy_session()
    mk.state = market_flow.DONE
    server.market_sessions[mk.id] = mk
    ec = _economy_session(state="done")
    server.economy_sessions[ec.id] = ec
    sh = _shop_session(state=shop_flow.DONE)
    server.shop_sessions[sh.id] = sh
    server._shop_session_created[sh.id] = time.time()

    body = _active(dev_auth)
    assert all(body[f] is None for f in FLOWS)


def test_other_users_and_platforms_isolated(dev_auth):
    s1 = _swap_session(discord_id="someone-else")
    server.swap_sessions[s1.id] = s1
    s2 = _swap_session(platform="telegram")
    server.swap_sessions[s2.id] = s2
    mk = _buy_session(discord_id="someone-else")
    server.market_sessions[mk.id] = mk
    ec = _economy_session(platform="web")
    server.economy_sessions[ec.id] = ec
    sh = _shop_session(buyer="rSomeoneElse00000000000000000000")
    server.shop_sessions[sh.id] = sh
    server._shop_session_created[sh.id] = time.time()
    sh2 = _shop_session(platform="telegram")
    server.shop_sessions[sh2.id] = sh2
    server._shop_session_created[sh2.id] = time.time()

    body = _active(dev_auth)
    assert all(body[f] is None for f in FLOWS)


def test_swap_payload_carries_payment_link(dev_auth):
    """The real repro: a relaunched client must be able to re-render the
    swap fee QR straight from the resumed payload."""
    s = _swap_session()
    s.payment_link = "https://xumm.app/sign/abc"
    server.swap_sessions[s.id] = s
    body = _active(dev_auth)
    assert body["swap"]["payment_link"] == "https://xumm.app/sign/abc"
    assert body["swap"]["state"] not in swap_flow.TERMINAL_STATES
