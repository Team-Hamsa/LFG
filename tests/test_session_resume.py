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
from cryptography.fernet import Fernet  # noqa: E402

from lfg_core import (  # noqa: E402
    closet_market_store,
    closet_token,
    economy_store,
    market_flow,
    mint_flow,
    shop_flow,
    swap_flow,
)
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
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


FLOWS = ("mint", "bulk", "swap", "market", "closet", "economy", "shop")


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


# --- Closet Market resume (#503) ---------------------------------------------
# GET /api/sessions/active grows a `closet` entry built from the SAME
# _closet_bid_view/_closet_fill_view app.py already uses for the dedicated
# /api/closet/bid/{id} and /api/closet/fill/{id} status polls, so a resumed
# entry renders and polls through app.js's existing attachMarketResume
# unchanged. Only two things are resume-worthy -- an OPEN book listing is
# normal state, not "in flight":
#  - a bid still waiting on its EscrowCreate signature (state pending_escrow)
#    whose XUMM payload hasn't hit its own DEFAULT_EXPIRE_MINUTES TTL;
#  - a fill (buyer or seller side) that hasn't reached a terminal state
#    (lfg_core.closet_market_store.TERMINAL_FILL_STATES) within the last 30
#    minutes -- past that window (an `indeterminate` fill, whose resolution
#    per closet_market_flow.py's fill-machine docstring can genuinely take a
#    while, is the state most likely to still be sitting there) it reads as
#    stuck for an operator, not a live resume candidate.

CLOSET_OWNER = mock_economy.DEV_OWNER  # what dev-mode require_wallet resolves
CLOSET_SELLER = "rClosetResumeSeller000000000000000"
CLOSET_BUYER = "rClosetResumeBuyer0000000000000000"  # buys FROM CLOSET_OWNER (fix round 2)


@pytest.fixture
def closet_resume_env(dev_auth, tmp_path, monkeypatch):
    """Fresh per-test onchain.db (mirrors tests/test_closet_market_api.py's
    closet_env) with CLOSET_OWNER, CLOSET_SELLER and CLOSET_BUYER all holding
    an active Closet, so the store-level create_pending_bid/create_ask/
    create_take_fill calls below (no HTTP handler involved) don't refuse on
    closet_required. CLOSET_SELLER holds two DIFFERENT traits so a test can
    put two concurrent fills in flight at once (fix round 1, #503) without
    colliding on (owner, slot, value); CLOSET_OWNER holds one too so a test
    can put CLOSET_OWNER on the SELLER side of a fill (fix round 2 -- the
    role-aware tests need CLOSET_OWNER, the dev-mode wallet, playing both
    roles across different tests). CLOSET_MARKET_ENC_KEY is patched truthy
    so config.closet_market_settleable() -- which now gates the closet DB
    lookup in handle_sessions_active -- doesn't skip it (unset by default;
    see test_closet_market_api.py's own closet_env fixture)."""
    path = str(tmp_path / "onchain_testnet.db")
    conn = init_onchain_db(path)
    economy_store.init_economy_schema(conn)
    for owner in (CLOSET_OWNER, CLOSET_SELLER, CLOSET_BUYER):
        economy_store.set_closet_token(conn, owner, f"C-{owner}", "00", status=closet_token.ACTIVE)
    economy_store.set_closet_contents(
        conn, CLOSET_SELLER, [("Hat", "Wizard Hat", 1), ("Eyes", "Laser Eyes", 1)], []
    )
    economy_store.set_closet_contents(conn, CLOSET_OWNER, [("Mouth", "Grin", 1)], [])
    conn.commit()
    conn.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", path)
    monkeypatch.setattr(server.config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())
    return path


def _closet_conn(path):
    conn = init_onchain_db(path)
    economy_store.init_economy_schema(conn)
    return conn


def test_pending_bid_with_live_payload_resumes(dev_auth, closet_resume_env):
    conn = _closet_conn(closet_resume_env)
    closet_market_store.create_pending_bid(
        conn,
        owner=CLOSET_OWNER,
        slot="Hat",
        value="Wizard Hat",
        price_brix="10",
        platform=None,
        condition="COND",
        fulfillment_enc="SEALED",
        cancel_after=9_999_999_999,
        payload_uuid="U1",
        xumm_url="https://xumm.app/sign/u1",
        qr_url="https://xumm.app/qr/u1",
        push=None,
    )
    conn.close()

    body = _active(dev_auth)
    assert body["closet"]["kind"] == "closet_bid"
    assert body["closet"]["state"] == "awaiting_signature"
    assert body["closet"]["slot"] == "Hat"
    assert body["closet"]["value"] == "Wizard Hat"


def test_pending_bid_with_expired_payload_omitted(dev_auth, closet_resume_env):
    """Past xumm_ops.DEFAULT_EXPIRE_MINUTES (15) the QR itself is dead in
    Xaman; resuming straight to it would strand the user on a spinner that
    can never complete."""
    conn = _closet_conn(closet_resume_env)
    closet_market_store.create_pending_bid(
        conn,
        owner=CLOSET_OWNER,
        slot="Hat",
        value="Wizard Hat",
        price_brix="10",
        platform=None,
        condition="COND",
        fulfillment_enc="SEALED",
        cancel_after=9_999_999_999,
        payload_uuid="U1",
        xumm_url="https://xumm.app/sign/u1",
        qr_url="https://xumm.app/qr/u1",
        push=None,
        now=int(time.time()) - 20 * 60,
    )
    conn.close()

    body = _active(dev_auth)
    assert body["closet"] is None


def test_unfinished_fill_within_30min_resumes(dev_auth, closet_resume_env):
    conn = _closet_conn(closet_resume_env)
    ask = closet_market_store.create_ask(
        conn, owner=CLOSET_SELLER, slot="Hat", value="Wizard Hat", price_brix="10", platform=None
    )
    closet_market_store.create_take_fill(
        conn, ask["id"], CLOSET_OWNER, fee_bps=0, platform=None, now=int(time.time()) - 300
    )
    conn.close()

    body = _active(dev_auth)
    assert body["closet"]["kind"] == "closet_fill"
    assert body["closet"]["state"] == "awaiting_signature"
    assert body["closet"]["role"] == "buyer"


def test_indeterminate_fill_older_than_30min_excluded(dev_auth, closet_resume_env):
    conn = _closet_conn(closet_resume_env)
    ask = closet_market_store.create_ask(
        conn, owner=CLOSET_SELLER, slot="Hat", value="Wizard Hat", price_brix="10", platform=None
    )
    fill = closet_market_store.create_take_fill(
        conn, ask["id"], CLOSET_OWNER, fee_bps=0, platform=None, now=int(time.time()) - 2400
    )
    closet_market_store.update_fill(conn, fill["id"], state=closet_market_store.INDETERMINATE)
    conn.close()

    body = _active(dev_auth)
    assert body["closet"] is None


# --- Closet Market resume: ranking among several candidates (fix round 1) ---
# has_live_bid / a take-fill's ask scope uniqueness per (owner, slot, value),
# so a wallet can genuinely hold several concurrent non-terminal bids or
# fills. The envelope keeps its single `closet` slot -- the fix is WHICH one
# wins: a row still needing the user's signature outranks one that's merely
# settling, and among equals the OLDEST wins (closest to expiring).


def test_older_awaiting_signature_fill_outranks_newer_settling_fill(dev_auth, closet_resume_env):
    """Greptile P1 / task review: unfinished_fills_for_wallet used to collapse
    to newest-created, which could hide an older fill still waiting on the
    buyer's Payment signature behind a newer one that's merely settling."""
    conn = _closet_conn(closet_resume_env)
    ask1 = closet_market_store.create_ask(
        conn, owner=CLOSET_SELLER, slot="Hat", value="Wizard Hat", price_brix="10", platform=None
    )
    ask2 = closet_market_store.create_ask(
        conn, owner=CLOSET_SELLER, slot="Eyes", value="Laser Eyes", price_brix="10", platform=None
    )
    older_awaiting = closet_market_store.create_take_fill(
        conn, ask1["id"], CLOSET_OWNER, fee_bps=0, platform=None, now=int(time.time()) - 600
    )
    newer_settling = closet_market_store.create_take_fill(
        conn, ask2["id"], CLOSET_OWNER, fee_bps=0, platform=None, now=int(time.time()) - 120
    )
    closet_market_store.update_fill(conn, newer_settling["id"], state=closet_market_store.FUNDED)
    conn.close()

    body = _active(dev_auth)
    assert body["closet"]["id"] == older_awaiting["id"]
    assert body["closet"]["state"] == "awaiting_signature"


def test_fill_outranks_bid_when_both_eligible(dev_auth, closet_resume_env):
    """The fill-outranks-bid branch in _closet_resume_view had no test
    exercising both a live bid AND a live fill for the same wallet at once
    (fix round 1 item 4) -- a reorder or deletion of that branch would have
    passed every other test silently."""
    conn = _closet_conn(closet_resume_env)
    closet_market_store.create_pending_bid(
        conn,
        owner=CLOSET_OWNER,
        slot="Hat",
        value="Wizard Hat",
        price_brix="10",
        platform=None,
        condition="COND",
        fulfillment_enc="SEALED",
        cancel_after=9_999_999_999,
        payload_uuid="U1",
        xumm_url="https://xumm.app/sign/u1",
        qr_url="https://xumm.app/qr/u1",
        push=None,
    )
    ask = closet_market_store.create_ask(
        conn, owner=CLOSET_SELLER, slot="Eyes", value="Laser Eyes", price_brix="10", platform=None
    )
    fill = closet_market_store.create_take_fill(
        conn, ask["id"], CLOSET_OWNER, fee_bps=0, platform=None
    )
    conn.close()

    body = _active(dev_auth)
    assert body["closet"]["kind"] == "closet_fill"
    assert body["closet"]["id"] == fill["id"]


# --- Closet Market resume: role-aware actionability (fix round 2) ---------
# _closet_pick_view (fix round 1) ranks candidates by whether they need the
# wallet's signature, but that only works if every candidate is genuinely
# actionable for THIS wallet first. Two related bugs the bots found:
# (Greptile P1) _closet_fill_view's awaiting_signature branch didn't check
# role, so a seller could be shown "you need to pay" for the buyer's own
# payment; (CodeRabbit Major) a role-terminal fill (done for this wallet,
# but not yet in the persisted TERMINAL_FILL_STATES) could win the pick or
# be the only fill row, silently swallowing a live bid that should have
# resumed instead.


def test_seller_not_shown_awaiting_signature_for_buyer_owed_payment(dev_auth, closet_resume_env):
    """Greptile P1: the seller side of a fill still awaiting the BUYER's
    Payment signature must never read as awaiting_signature -- that's not a
    bill the seller can pay."""
    conn = _closet_conn(closet_resume_env)
    ask = closet_market_store.create_ask(
        conn, owner=CLOSET_OWNER, slot="Mouth", value="Grin", price_brix="10", platform=None
    )
    closet_market_store.create_take_fill(conn, ask["id"], CLOSET_BUYER, fee_bps=0, platform=None)
    conn.close()

    body = _active(dev_auth)
    assert body["closet"]["kind"] == "closet_fill"
    assert body["closet"]["role"] == "seller"
    assert body["closet"]["state"] != "awaiting_signature"
    assert body["closet"]["state"] == "pending"


def test_role_terminal_fill_does_not_suppress_bid_fallback(dev_auth, closet_resume_env):
    """CodeRabbit Major: unfinished_fills_for_wallet excludes the PERSISTED
    TERMINAL_FILL_STATES (mirrored/refunded/failed) but not the raw 'paid'
    state, which _closet_fill_view reads as done for either role. Before the
    fix, a wallet with ONLY this fill got it back as `closet` (the client's
    own TERMINAL.closet would then reject it as non-resumable) and the
    function returned before ever checking for a live bid -- so a perfectly
    resumable bid was silently lost. This proves the fallback runs."""
    conn = _closet_conn(closet_resume_env)
    ask = closet_market_store.create_ask(
        conn, owner=CLOSET_OWNER, slot="Mouth", value="Grin", price_brix="10", platform=None
    )
    done_fill = closet_market_store.create_take_fill(
        conn, ask["id"], CLOSET_BUYER, fee_bps=0, platform=None, now=int(time.time()) - 300
    )
    closet_market_store.update_fill(conn, done_fill["id"], state=closet_market_store.PAID)
    bid = closet_market_store.create_pending_bid(
        conn,
        owner=CLOSET_OWNER,
        slot="Hat",
        value="Wizard Hat",
        price_brix="10",
        platform=None,
        condition="COND",
        fulfillment_enc="SEALED",
        cancel_after=9_999_999_999,
        payload_uuid="U1",
        xumm_url="https://xumm.app/sign/u1",
        qr_url="https://xumm.app/qr/u1",
        push=None,
    )
    conn.close()

    body = _active(dev_auth)
    assert body["closet"]["kind"] == "closet_bid"
    assert body["closet"]["id"] == bid["id"]


def test_role_terminal_fill_does_not_outrank_active_fill(dev_auth, closet_resume_env):
    """CodeRabbit Major, narrower form: with a genuinely active fill also in
    play, an OLDER role-terminal 'done' fill must not win _closet_pick_view's
    age tiebreak just for being older -- it has to be dropped before ranking
    even starts, not merely placed in the same tier and out-aged."""
    conn = _closet_conn(closet_resume_env)
    ask1 = closet_market_store.create_ask(
        conn, owner=CLOSET_SELLER, slot="Hat", value="Wizard Hat", price_brix="10", platform=None
    )
    done_fill = closet_market_store.create_take_fill(
        conn, ask1["id"], CLOSET_OWNER, fee_bps=0, platform=None, now=int(time.time()) - 600
    )
    closet_market_store.update_fill(conn, done_fill["id"], state=closet_market_store.PAID)
    ask2 = closet_market_store.create_ask(
        conn, owner=CLOSET_SELLER, slot="Eyes", value="Laser Eyes", price_brix="10", platform=None
    )
    active_fill = closet_market_store.create_take_fill(
        conn, ask2["id"], CLOSET_OWNER, fee_bps=0, platform=None, now=int(time.time()) - 120
    )
    closet_market_store.update_fill(conn, active_fill["id"], state=closet_market_store.FUNDED)
    conn.close()

    body = _active(dev_auth)
    assert body["closet"]["id"] == active_fill["id"]
    assert body["closet"]["state"] == "pending"
