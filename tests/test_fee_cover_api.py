"""Fee cover wired into the bid flow (spec §Lifecycle, §Fill detection)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from lfg_core import fee_cover_settle, fee_cover_store, history_store
from lfg_core.economy_store import _ECONOMY_SCHEMA
from lfg_core.market_store import (
    BuyOffer,
    MarketListing,
    close_bid,
    close_listing,
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


def _finalizing_advance(offer_index="D" * 64, raise_for=()):
    async def fake_advance(s):
        if s.id in raise_for:
            raise RuntimeError("xumm down")
        if s.state == server.market_flow.DONE:
            return None
        s.state = server.market_flow.DONE
        s.offer_index = offer_index
        return {
            "offer_index": offer_index,
            "nft_id": s.nft_id,
            "bidder": s.wallet_address,
            "amount_drops": s.amount_drops,
            "expiration": 900_000_000,
        }

    return fake_advance


def _start_quoted_bid(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    _fake_payload(monkeypatch)
    body = _body(
        _run(
            server.handle_market_bid_start(
                _post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})
            )
        )
    )
    assert body["fee_cover"]["state"] == "quoted"
    assert "fee_cover_listing" not in body  # server-side only
    return server.market_sessions[body["id"]]


def _close_cafe_listing_sold(onchain, buyer):
    conn = init_onchain_db(onchain)
    try:
        close_listing(conn, "E" * 64, "sold", buyer=buyer)
    finally:
        conn.close()


def _promise_row(app_db, offer_index="D" * 64):
    conn = fee_cover_store.connect(app_db)
    try:
        return fee_cover_store.get_promise(conn, offer_index)
    finally:
        conn.close()


def test_quote_saves_the_listing_on_the_session(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    assert session.fee_cover_listing == {
        "offer_index": "E" * 64,
        "seller": OWNER,
        "ask_drops": 4_990_000,
        "broker": CAFE,
        "broker_rate": 0.01589,
    }


def test_covered_fill_that_closes_the_listing_before_finalize_still_records_the_promise(
    env, monkeypatch
):
    """The broker's bot fills the bid and the listener closes the cafe listing
    `sold` to this bidder before the client's first `done` poll: the promise the
    user was quoted must still be written."""
    session = _start_quoted_bid(env, monkeypatch)
    _close_cafe_listing_sold(env["onchain"], BIDDER)
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    assert body["fee_cover"]["state"] == "open" and body["fee_cover"]["drops"] == 80_642
    promise = _promise_row(env["app_db"])
    assert promise["state"] == "open"
    assert promise["ask_drops"] == 4_990_000 and promise["broker"] == CAFE


def test_listing_sold_to_a_different_buyer_before_finalize_records_no_promise(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    _close_cafe_listing_sold(env["onchain"], "rSomeoneElse000000000000000000000")
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    assert body["fee_cover"] is None
    assert _promise_row(env["app_db"]) is None


def test_finalize_rechecks_the_campaign_against_the_saved_listing(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.stop_campaign(conn, network="testnet", actor="t")
    conn.close()
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    assert body["fee_cover"] is None
    assert _promise_row(env["app_db"]) is None


def test_sweep_advances_a_quoted_bid_session_so_its_promise_is_written(env, monkeypatch):
    """The client stopped polling before the bid validated: the sweep advances
    the in-memory session itself, so the quoted promise is still recorded."""
    session = _start_quoted_bid(env, monkeypatch)
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())
    swept = []

    async def fake_sweep_once(deps, **kwargs):
        swept.append(deps)
        return fee_cover_settle.SweepReport()

    monkeypatch.setattr(server.fee_cover_settle, "sweep_once", fake_sweep_once)
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: "deps")
    _run(server.sweep_fee_cover())
    assert session.state == server.market_flow.DONE
    assert session.fee_cover["state"] == "open"
    assert _promise_row(env["app_db"])["state"] == "open"
    assert swept == ["deps"]


def test_sweep_advances_only_quoted_live_bid_sessions_and_survives_a_failure(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    quoted = {"state": "quoted", "drops": 1, "xrp": "0.000001", "reason": None}

    def bid(fee_cover, state=server.market_flow.AWAITING_SIGNATURE):
        s = server.market_flow.BidSession(
            discord_id="dev",
            wallet_address=BIDDER,
            nft_id=CHAR,
            owner=OWNER,
            amount_drops=5_075_000,
        )
        s.fee_cover = dict(fee_cover) if fee_cover else None
        s.state = state
        server.market_sessions[s.id] = s
        return s

    failing = bid(quoted)
    good = bid(quoted)
    ordinary = bid(None)
    declined = bid({**quoted, "state": "declined", "reason": "below_clearing"})
    finished = bid(quoted, state=server.market_flow.FAILED)
    advanced = []
    inner = _finalizing_advance(raise_for={failing.id})

    async def tracking_advance(s):
        advanced.append(s.id)
        return await inner(s)

    monkeypatch.setattr(server.market_flow, "advance_bid_session", tracking_advance)
    swept = []

    async def fake_sweep_once(deps, **kwargs):
        swept.append(deps)
        return fee_cover_settle.SweepReport()

    monkeypatch.setattr(server.fee_cover_settle, "sweep_once", fake_sweep_once)
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: "deps")
    _run(server.sweep_fee_cover())
    assert sorted(advanced) == sorted([failing.id, good.id])
    assert ordinary.id not in advanced and declined.id not in advanced
    assert finished.id not in advanced
    assert _promise_row(env["app_db"])["state"] == "open"
    assert swept == ["deps"]


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


def test_startup_recovery_is_retired_on_cleanup(monkeypatch):
    """A fire-and-forget recovery still inside an account_tx call or a DB write
    must not outlive aiohttp's teardown (or be abandoned when the loop closes).
    Cancelling is safe: it submits no payment, and a row left `submitted` is
    re-resolved by the next startup recovery or sweep pass."""
    started = asyncio.Event()

    async def never_returns(deps):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "_fee_cover_deps", lambda: object())
    monkeypatch.setattr(server.fee_cover_settle, "recover_refunds", never_returns)

    async def go():
        app: dict = {}
        await server._start_fee_cover_recovery(app)
        await started.wait()
        await asyncio.wait_for(server._stop_startup_recovery(app), timeout=5)
        return app["fee_cover_recovery_task"]

    task = _run(go())
    assert task.done()
    app = server.create_app()
    assert server._stop_startup_recovery in app.on_cleanup
    # The BRIX claim recovery task is retired by the same hook.
    assert server._start_brix_claim_recovery in app.on_startup


def test_stop_startup_recovery_tolerates_tasks_that_never_started(monkeypatch):
    _run(asyncio.wait_for(server._stop_startup_recovery({}), timeout=5))


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
    assert row["fee_cover_coverage_bps"] == 10_000


def test_browse_estimate_carries_partial_coverage(env):
    _seed_char_with_cafe_listing(env["onchain"])
    conn = fee_cover_store.connect(env["app_db"])
    knobs = fee_cover_store.Knobs(
        coverage_bps=5_000, budget_drops=50_000_000, wallet_cap_drops=5_000_000, min_bid_drops=0
    )
    fee_cover_store.start_campaign(
        conn, network="testnet", actor="t", knobs=knobs, duration_seconds=None
    )
    conn.close()
    row = _browse()
    assert row["fee_cover_drops"] == 40_286  # ceil(5_070_572 × 0.01589) × 50%
    assert row["fee_cover_coverage_bps"] == 5_000


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
    row = _browse()
    assert "fee_cover_drops" not in row and "fee_cover_coverage_bps" not in row


# --- linked-counterparty lookup is fail-open ---------------------------------


def _funders(app_db, rows):
    conn = sqlite3.connect(app_db)
    try:
        server.funding.ensure_schema(conn)
        for wallet, funder in rows:
            server.funding.record_funder(conn, wallet, funder, 1)
        conn.commit()
    finally:
        conn.close()


def test_linked_is_answered_by_a_shared_funder(env, monkeypatch):
    monkeypatch.setattr(server.identity_store, "bucket_for_wallet", lambda wallet: None)
    assert server._fee_cover_linked(BIDDER, OWNER) is False
    _funders(env["app_db"], [(BIDDER, "rSharedFunder"), (OWNER, "rSharedFunder")])
    assert server._fee_cover_linked(BIDDER, OWNER) is True


def test_linked_bucket_lookup_error_defers_instead_of_answering(env, monkeypatch):
    """A lookup that cannot answer must not resolve to "unrelated" — that would
    pay a refund on a sale the self-dealing rule might forbid."""

    def bucket_boom(wallet):
        raise RuntimeError("identity db locked")

    monkeypatch.setattr(server.identity_store, "bucket_for_wallet", bucket_boom)
    with pytest.raises(server.fee_cover.LinkageUnavailable):
        server._fee_cover_linked(BIDDER, OWNER)


def test_linked_funder_lookup_error_defers_instead_of_answering(env, monkeypatch):
    monkeypatch.setattr(server.identity_store, "bucket_for_wallet", lambda wallet: None)

    def funder_boom(conn, wallet):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server.funding, "cached_funder", funder_boom)
    with pytest.raises(server.fee_cover.LinkageUnavailable):
        server._fee_cover_linked(BIDDER, OWNER)


# --- Durable quotes: a cover the buyer was shown survives errors and restarts ---


def _quote_row(app_db, session_id):
    conn = fee_cover_store.connect(app_db)
    try:
        return fee_cover_store.get_quote(conn, session_id)
    finally:
        conn.close()


def _age_quote(app_db, session_id, seconds):
    conn = fee_cover_store.connect(app_db)
    try:
        conn.execute(
            "UPDATE fee_cover_quotes SET created_at = created_at - ? WHERE session_id = ?",
            (seconds, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def _wallet_status(monkeypatch, status):
    calls = []

    async def fake(uuid, **kwargs):
        calls.append(uuid)
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(server.xumm_ops, "get_payload_status", fake)
    return calls


def _signed(account=BIDDER, txid="TXHASH"):
    return {"signed": True, "expired": False, "account": account, "txid": txid}


def test_a_covered_quote_is_recorded_durably(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    row = _quote_row(env["app_db"], session.id)
    assert row["state"] == "pending" and row["payload_uuid"] == "U1"
    assert (row["bidder"], row["nft_id"], row["owner"]) == (BIDDER, CHAR, OWNER)
    assert row["bid_drops"] == 5_075_000
    assert json.loads(row["listing_json"])["offer_index"] == "E" * 64


def test_a_quote_that_cannot_be_recorded_is_not_offered(env, monkeypatch):
    """A cover the service can't carry through a restart is never shown."""
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    _fake_payload(monkeypatch)

    def boom(session):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server, "_fee_cover_record_quote", boom)
    body = _body(
        _run(
            server.handle_market_bid_start(
                _post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})
            )
        )
    )
    assert body["fee_cover"] is None
    assert server.market_sessions[body["id"]].fee_cover_listing is None


def test_a_failed_promise_write_keeps_the_quote_and_the_sweep_finishes_it(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())
    real = server._fee_cover_record_promise
    state = {"fail": True}

    def flaky(s, bid_row):
        if state["fail"]:
            raise RuntimeError("database is locked")
        return real(s, bid_row)

    monkeypatch.setattr(server, "_fee_cover_record_promise", flaky)
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    # the quote the buyer was shown is not erased by the error...
    assert body["state"] == "done" and body["fee_cover"]["state"] == "quoted"
    assert _promise_row(env["app_db"]) is None
    assert _quote_row(env["app_db"], session.id)["state"] == "pending"
    # ...and once the error clears, the sweep's reconciler writes the promise.
    state["fail"] = False
    _age_quote(env["app_db"], session.id, 120)
    _wallet_status(monkeypatch, _signed())
    _run(server._reconcile_fee_cover_quotes())
    assert _promise_row(env["app_db"])["state"] == "open"
    assert session.fee_cover["state"] == "open"
    row = _quote_row(env["app_db"], session.id)
    assert (row["state"], row["outcome"], row["offer_index"]) == ("closed", "promised", "D" * 64)


def test_a_bid_row_index_failure_still_writes_the_promise(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())

    def boom(network, row):
        raise RuntimeError("onchain db locked")

    monkeypatch.setattr(server, "_write_bid_row", boom)

    async def go():
        await server._advance_market_session("bid", session, asyncio.get_event_loop())

    with pytest.raises(RuntimeError, match="onchain db locked"):
        _run(go())  # the indexing error still surfaces, as before
    assert _promise_row(env["app_db"])["state"] == "open"
    assert _quote_row(env["app_db"], session.id)["outcome"] == "promised"


def test_a_quote_lost_to_a_restart_is_reconciled_from_the_ledger(env, monkeypatch):
    """The bid validated but the service restarted before observing it: the
    in-memory session is gone, and the durable quote rebuilds it."""
    session = _start_quoted_bid(env, monkeypatch)
    server.market_sessions.clear()  # the restart
    _age_quote(env["app_db"], session.id, 120)
    rebuilt = []
    advance = _finalizing_advance()

    async def recording_advance(s):
        rebuilt.append((s.id, s.state, s.txid, s.wallet_address, s.fee_cover_listing))
        return await advance(s)

    monkeypatch.setattr(server.market_flow, "advance_bid_session", recording_advance)
    _wallet_status(monkeypatch, _signed())
    _run(server._reconcile_fee_cover_quotes())
    [(sid, st, txid, wallet, listing)] = rebuilt
    assert (sid, st, txid, wallet) == (session.id, server.market_flow.PENDING, "TXHASH", BIDDER)
    assert listing["offer_index"] == "E" * 64
    assert _promise_row(env["app_db"])["state"] == "open"
    row = _quote_row(env["app_db"], session.id)
    assert row["outcome"] == "promised" and row["txid"] == "TXHASH"


def test_a_persisted_txid_goes_straight_to_the_ledger(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    server._fee_cover_note_txid(session.id, "TXHASH")
    server.market_sessions.clear()
    _age_quote(env["app_db"], session.id, 120)
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())
    calls = _wallet_status(monkeypatch, RuntimeError("must not ask the wallet"))
    _run(server._reconcile_fee_cover_quotes())
    assert calls == []
    assert _promise_row(env["app_db"])["state"] == "open"


@pytest.mark.parametrize(
    "status, outcome",
    [
        ({"signed": False, "expired": True}, "expired"),
        (_signed(account="rSomeoneElse000000000000000000000"), "failed"),
    ],
)
def test_reconcile_closes_quotes_that_never_became_a_valid_bid(env, monkeypatch, status, outcome):
    session = _start_quoted_bid(env, monkeypatch)
    server.market_sessions.clear()
    _age_quote(env["app_db"], session.id, 120)

    async def never(s):
        raise AssertionError("no ledger check for a bid that was never validly signed")

    monkeypatch.setattr(server.market_flow, "advance_bid_session", never)
    _wallet_status(monkeypatch, status)
    _run(server._reconcile_fee_cover_quotes())
    assert _quote_row(env["app_db"], session.id)["outcome"] == outcome
    assert _promise_row(env["app_db"]) is None


def test_reconcile_leaves_unresolved_quotes_pending(env, monkeypatch):
    """Unsigned-but-live, an unreachable wallet provider, or a bid not yet
    validated: all retry next sweep."""
    session = _start_quoted_bid(env, monkeypatch)
    server.market_sessions.clear()
    _age_quote(env["app_db"], session.id, 120)

    async def not_validated(s):
        return None

    monkeypatch.setattr(server.market_flow, "advance_bid_session", not_validated)
    for status in ({"signed": False, "expired": False}, None, _signed()):
        _wallet_status(monkeypatch, status)
        _run(server._reconcile_fee_cover_quotes())
        assert _quote_row(env["app_db"], session.id)["state"] == "pending"


def test_reconcile_skips_a_fresh_quote_and_a_live_unfinished_session(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    calls = _wallet_status(monkeypatch, _signed())
    _run(server._reconcile_fee_cover_quotes())  # fresh: inside the grace period
    _age_quote(env["app_db"], session.id, 120)
    _run(server._reconcile_fee_cover_quotes())  # aged, but its live session owns it
    assert calls == []
    assert _quote_row(env["app_db"], session.id)["state"] == "pending"


def test_reconcile_abandons_a_quote_past_its_max_age(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    server.market_sessions.clear()
    _age_quote(env["app_db"], session.id, server._FEE_COVER_QUOTE_MAX_AGE_SECONDS + 1)
    calls = _wallet_status(monkeypatch, _signed())
    _run(server._reconcile_fee_cover_quotes())
    assert calls == []
    assert _quote_row(env["app_db"], session.id)["outcome"] == "abandoned"


def test_an_uncovered_finalize_closes_its_quote(env, monkeypatch):
    session = _start_quoted_bid(env, monkeypatch)
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.stop_campaign(conn, network="testnet", actor="t")
    conn.close()
    monkeypatch.setattr(server.market_flow, "advance_bid_session", _finalizing_advance())
    _run(server.handle_market_bid_status(_StatusReq(session.id)))
    assert _quote_row(env["app_db"], session.id)["outcome"] == "uncovered"
