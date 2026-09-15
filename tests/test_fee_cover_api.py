"""Fee cover wired into the bid flow (spec §Lifecycle, §Fill detection)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from lfg_core import fee_cover_settle, fee_cover_store, history_store
from lfg_core.economy_store import _ECONOMY_SCHEMA
from lfg_core.market_store import (
    BuyOffer,
    MarketListing,
    close_bid,
    init_bid_schema,
    upsert_bid,
    upsert_listing,
)
from lfg_core.market_store import init_db as init_market_db
from lfg_core.nft_index import OnchainNft
from lfg_core.nft_index import init_db as init_onchain_db
from lfg_core.nft_index import upsert as upsert_onchain_nft
from lfg_service import app as server
from webapp import mock_economy

FIXTURE = Path(__file__).parent / "fixtures" / "fee_cover" / "cafe_brokered_accept.json"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"
BIDDER = "rBidderAddress0000000000000000000"
OWNER = "rOwnerAddress000000000000000000000"
CHAR = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000001"
ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
FIX_BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"
FIX_NFT = "00191B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BECADA906904943EEE"
FIX_BUY_OFFER = "37B0856D51DF75221A9140AF8FCD89E1AC3CAC703D36070BA7E2FE85567C4B7D"
ACCEPT_TS = 1_787_421_471


def _run(coro):
    # A private loop: never disturbs (or depends on) the global event loop,
    # which asyncio.run() in other test modules leaves unset on Python 3.10.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _body(resp):
    return json.loads(resp.body.decode())


def _post(path, body):
    req = make_mocked_request("POST", path)

    async def _json():
        return body

    req.json = _json  # type: ignore[method-assign]
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    onchain = str(tmp_path / "onchain_testnet.db")
    conn = init_onchain_db(onchain)
    conn.executescript(_ECONOMY_SCHEMA)
    init_market_db(conn)
    init_bid_schema(conn)
    conn.commit()
    conn.close()
    app_db = str(tmp_path / "app.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain)
    monkeypatch.delenv("BROKER_CLEARING_BUFFER_DROPS", raising=False)
    monkeypatch.delenv("BROKER_ALLOWLIST_PATH", raising=False)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BIDDER)
    monkeypatch.setattr(server, "_use_market_mock", lambda: False)
    monkeypatch.setattr(server, "_fee_cover_db", lambda: app_db)
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()
    yield {"onchain": onchain, "app_db": app_db, "tmp": tmp_path}
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()


def _seed_char_with_cafe_listing(onchain, *, ask=4_990_000, destination=CAFE):
    conn = init_onchain_db(onchain)
    upsert_onchain_nft(
        conn,
        OnchainNft(
            nft_id=CHAR,
            nft_number=1,
            owner=OWNER,
            is_burned=False,
            mutable=True,
            uri_hex="",
            body="Ape",
            attributes=[],
            image="",
            video="",
            ledger_index=1,
        ),
    )
    upsert_listing(
        conn,
        MarketListing(
            offer_index="E" * 64,
            nft_id=CHAR,
            kind="character",
            seller=OWNER,
            amount_drops=ask,
            destination=destination,
            created_ledger=1,
            created_ts=1,
        ),
    )
    conn.commit()
    conn.close()


def _campaign(app_db, now=None):
    conn = fee_cover_store.connect(app_db)
    try:
        knobs = fee_cover_store.Knobs(
            coverage_bps=10_000,
            budget_drops=50_000_000,
            wallet_cap_drops=5_000_000,
            min_bid_drops=1_000_000,
        )
        campaign, _ = fee_cover_store.start_campaign(
            conn, network="testnet", actor="t", knobs=knobs, duration_seconds=None, now=now
        )
        return campaign
    finally:
        conn.close()


def _fake_payload(monkeypatch):
    async def fake(*args, **kwargs):
        return {"qr_url": "https://qr", "xumm_url": "https://xumm.app/sign/U1", "uuid": "U1"}

    monkeypatch.setattr(server.xumm_ops, "create_buy_offer_payload", fake)


def test_bid_start_quotes_the_fee_for_a_clearing_bid_on_a_cafe_listing(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    _fake_payload(monkeypatch)
    resp = _run(
        server.handle_market_bid_start(
            _post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})
        )
    )
    assert resp.status == 200
    assert _body(resp)["fee_cover"] == {
        "state": "quoted",
        "drops": 80_642,
        "xrp": "0.080642",
        "reason": None,
        "payout_tx_hash": None,
    }


def test_bid_start_has_no_fee_cover_without_a_campaign_or_external_listing(env, monkeypatch):
    _fake_payload(monkeypatch)
    _seed_char_with_cafe_listing(env["onchain"])
    resp = _run(
        server.handle_market_bid_start(
            _post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})
        )
    )
    assert _body(resp)["fee_cover"] is None  # no campaign
    server.market_sessions.clear()
    _campaign(env["app_db"])
    conn = init_onchain_db(env["onchain"])
    conn.execute("UPDATE market_listings SET destination = NULL")
    conn.commit()
    conn.close()
    resp = _run(
        server.handle_market_bid_start(
            _post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})
        )
    )
    assert _body(resp)["fee_cover"] is None  # plain listing: an ordinary bid


def test_bid_start_quote_declines_below_clearing(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    _fake_payload(monkeypatch)
    resp = _run(
        server.handle_market_bid_start(_post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5"}))
    )
    assert _body(resp)["fee_cover"]["state"] == "declined"
    assert _body(resp)["fee_cover"]["reason"] == "below_clearing"
    assert _body(resp)["fee_cover"]["drops"] == 0


def test_bid_start_survives_a_fee_cover_failure(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _fake_payload(monkeypatch)

    def boom(*args):
        raise RuntimeError("fee cover db locked")

    monkeypatch.setattr(server, "_fee_cover_quote", boom)
    resp = _run(
        server.handle_market_bid_start(
            _post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})
        )
    )
    assert resp.status == 200
    assert _body(resp)["fee_cover"] is None


def test_bid_finalize_records_the_promise_and_the_poll_reports_it(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    session = server.market_flow.BidSession(
        discord_id="dev",
        wallet_address=BIDDER,
        nft_id=CHAR,
        owner=OWNER,
        amount_drops=5_075_000,
        platform="discord",
    )
    server.market_sessions[session.id] = session

    async def fake_advance(s):
        if s.state == server.market_flow.DONE:
            return None
        s.state = server.market_flow.DONE
        s.offer_index = "D" * 64
        return {
            "offer_index": "D" * 64,
            "nft_id": CHAR,
            "bidder": BIDDER,
            "amount_drops": 5_075_000,
            "expiration": 900_000_000,
        }

    monkeypatch.setattr(server.market_flow, "advance_bid_session", fake_advance)
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    assert body["fill"] == "live"
    assert body["fee_cover"]["state"] == "open" and body["fee_cover"]["drops"] == 80_642
    conn = fee_cover_store.connect(env["app_db"])
    try:
        promise = fee_cover_store.get_promise(conn, "D" * 64)
    finally:
        conn.close()
    assert (
        promise["bid_expiration"] == 900_000_000
        and promise["broker"] == CAFE
        and promise["ask_drops"] == 4_990_000
    )


def test_poll_after_the_fill_settles_the_refund(env, monkeypatch):
    campaign = _campaign(env["app_db"], now=ACCEPT_TS - 600)
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.record_promise(
        conn,
        campaign,
        fee_cover_store.PromiseInput(
            offer_index=FIX_BUY_OFFER,
            network="testnet",
            nft_id=FIX_NFT,
            bidder=FIX_BUYER,
            bid_drops=5_075_000,
            ask_drops=4_990_000,
            broker=CAFE,
            broker_rate=0.01589,
            bid_expiration=900_000_000,
        ),
        promised_drops=80_642,
        decline_reason=None,
        now=ACCEPT_TS - 60,
    )
    conn.close()
    oconn = init_onchain_db(env["onchain"])
    upsert_bid(
        oconn,
        BuyOffer(
            offer_index=FIX_BUY_OFFER, nft_id=FIX_NFT, bidder=FIX_BUYER, amount_drops=5_075_000
        ),
    )
    close_bid(oconn, FIX_BUY_OFFER, "accepted")
    oconn.commit()
    oconn.close()
    tx = json.loads(FIXTURE.read_text())
    history = str(env["tmp"] / "history.db")
    hconn = history_store.init_history_db(history)
    history_store.insert_tx(
        hconn,
        tx_hash=tx["hash"],
        ledger_index=1,
        close_time=ACCEPT_TS,
        tx_type="NFTokenAcceptOffer",
        account=CAFE,
        source_tag=None,
        raw_json=json.dumps(tx),
    )
    history_store.insert_nft_event(
        hconn, {"tx_hash": tx["hash"], "nft_id": FIX_NFT, "event": "sale", "ts": ACCEPT_TS}
    )
    hconn.commit()
    hconn.close()

    async def get_tx(tx_hash):
        return tx

    async def never(*args, **kwargs):
        raise AssertionError("no payout on the poll path")

    deps = fee_cover_settle.FeeCoverDeps(
        network="testnet",
        app_db_path=env["app_db"],
        history_db_path=history,
        payer=ISSUER,
        ledger_margin=40,
        get_tx=get_tx,
        send_refund=never,
        find_refund_payment=never,
        current_ledger=never,
        broker_rate_for=lambda account, nft_id: {CAFE: 0.01589}.get(account),
        linked=lambda a, b: False,
    )
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: deps)
    session = server.market_flow.BidSession(
        discord_id="dev",
        wallet_address=FIX_BUYER,
        nft_id=FIX_NFT,
        owner="rLfbT5Vkbigi4gFUi1QUGu5udijV79i3tF",
        amount_drops=5_075_000,
        platform="discord",
    )
    session.state = server.market_flow.DONE
    session.offer_index = FIX_BUY_OFFER
    session.fee_cover = {
        "state": "open",
        "drops": 80_642,
        "xrp": "0.080642",
        "reason": None,
        "payout_tx_hash": None,
    }
    server.market_sessions[session.id] = session
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    assert body["fill"] == "accepted"
    assert body["fee_cover"]["state"] == "owed" and body["fee_cover"]["drops"] == 80_642


def test_bids_mine_rows_carry_fee_cover(env):
    _seed_char_with_cafe_listing(env["onchain"])
    campaign = _campaign(env["app_db"])
    oconn = init_onchain_db(env["onchain"])
    upsert_bid(
        oconn, BuyOffer(offer_index="D" * 64, nft_id=CHAR, bidder=BIDDER, amount_drops=5_075_000)
    )
    oconn.commit()
    oconn.close()
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.record_promise(
        conn,
        campaign,
        fee_cover_store.PromiseInput(
            offer_index="D" * 64,
            network="testnet",
            nft_id=CHAR,
            bidder=BIDDER,
            bid_drops=5_075_000,
            ask_drops=4_990_000,
            broker=CAFE,
            broker_rate=0.01589,
            bid_expiration=None,
        ),
        promised_drops=80_642,
        decline_reason=None,
    )
    conn.close()
    body = _body(
        _run(server.handle_market_bids_mine(make_mocked_request("GET", "/api/market/bids/mine")))
    )
    assert body["my_bids"][0]["fee_cover"]["state"] == "open"


# --- Task 7: sweep + startup recovery -------------------------------------


class _Stop(BaseException):
    """Escapes the loop's `except Exception` guards to end one iteration."""


def test_sweep_fee_cover_runs_sweep_once_with_service_deps(monkeypatch):
    seen = {}
    sentinel = object()
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: sentinel)

    async def fake_sweep_once(deps, *, attempts, max_attempts, on_giveup):
        seen.update(deps=deps, attempts=attempts, max_attempts=max_attempts, on_giveup=on_giveup)
        return fee_cover_settle.SweepReport()

    monkeypatch.setattr(server.fee_cover_settle, "sweep_once", fake_sweep_once)
    _run(server.sweep_fee_cover())
    assert seen["deps"] is sentinel
    assert seen["attempts"] is server._fee_cover_attempts
    assert seen["max_attempts"] == server._SWEEP_MAX_ATTEMPTS
    assert seen["on_giveup"] is server._write_fee_cover_giveup


def test_settlement_loop_runs_the_fee_cover_sweep_with_the_economy_off(monkeypatch):
    calls = []
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)

    async def sign_requests():
        calls.append("sign")

    async def fee_cover_sweep():
        calls.append("fee_cover")
        raise _Stop

    monkeypatch.setattr(server, "sweep_sign_requests", sign_requests)
    monkeypatch.setattr(server, "sweep_fee_cover", fee_cover_sweep)
    with pytest.raises(_Stop):
        _run(server._settlement_sweep_loop())
    assert calls == ["sign", "fee_cover"]


def test_a_crashing_fee_cover_sweep_does_not_kill_the_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)

    async def sign_requests():
        calls.append("sign")
        if len(calls) > 1:
            raise _Stop

    async def fee_cover_sweep():
        calls.append("fee_cover")
        raise RuntimeError("db locked")

    monkeypatch.setattr(server, "sweep_sign_requests", sign_requests)
    monkeypatch.setattr(server, "sweep_fee_cover", fee_cover_sweep)
    monkeypatch.setattr(server, "_SWEEP_PERIOD_SECONDS", 0)
    with pytest.raises(_Stop):
        _run(server._settlement_sweep_loop())
    assert calls == ["sign", "fee_cover", "sign"]


def test_giveup_journal_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_RECORDS_DIR", str(tmp_path))
    server._write_fee_cover_giveup(
        {"accept_tx_hash": "ACC", "offer_index": "OFF", "bidder": "rB", "refund_drops": 80_642}
    )
    record = json.loads((tmp_path / "fee-cover-giveup-ACC.json").read_text())
    assert record["status"] == "abandoned" and record["refund_drops"] == 80_642


def test_startup_recovery_resolves_submitted_refunds(monkeypatch):
    called = []
    sentinel = object()
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: sentinel)

    async def fake_recover(deps):
        called.append(deps)
        return {"ACC": "confirmed"}

    monkeypatch.setattr(server.fee_cover_settle, "recover_refunds", fake_recover)

    async def go():
        app: dict = {}
        await server._start_fee_cover_recovery(app)
        await app["fee_cover_recovery_task"]

    _run(go())
    assert called == [sentinel]
    assert server._start_fee_cover_recovery in server.create_app().on_startup


# --- Task 10: browse estimate -------------------------------------------------


def _browse():
    req = make_mocked_request("GET", "/api/market/listings?include_external=1")
    rows = _body(_run(server.handle_market_listings(req)))["rows"]
    return next(r for r in rows if r.get("source") == "external")


def test_browse_external_row_carries_the_fee_cover_estimate(env):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    row = _browse()
    assert row["clearing_drops"] == 5_070_572
    assert row["fee_cover_drops"] == 80_572  # ceil(5_070_572 × 0.01589) at 100% coverage
    assert row["fee_cover_xrp"] == "0.080572"


def test_browse_estimate_follows_the_campaign_not_the_cache(env):
    _seed_char_with_cafe_listing(env["onchain"])
    assert "fee_cover_drops" not in _browse()  # no campaign; cache now warm
    _campaign(env["app_db"])
    assert _browse()["fee_cover_drops"] == 80_572  # same cached rows, fresh campaign read
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.stop_campaign(conn, network="testnet", actor="t")
    conn.close()
    assert "fee_cover_drops" not in _browse()


def test_browse_estimate_is_hidden_without_budget_headroom(env):
    _seed_char_with_cafe_listing(env["onchain"])
    conn = fee_cover_store.connect(env["app_db"])
    knobs = fee_cover_store.Knobs(
        coverage_bps=10_000, budget_drops=80_571, wallet_cap_drops=5_000_000, min_bid_drops=0
    )
    fee_cover_store.start_campaign(
        conn, network="testnet", actor="t", knobs=knobs, duration_seconds=None
    )
    conn.close()
    assert "fee_cover_drops" not in _browse()
