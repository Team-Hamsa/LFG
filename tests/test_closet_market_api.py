import asyncio
import json
from decimal import Decimal

import pytest
from aiohttp.test_utils import make_mocked_request
from cryptography.fernet import Fernet

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core.nft_index import init_db as init_onchain_db
from lfg_service import app as server
from webapp import mock_economy

ME, BIDDER = "rMeSeller", "rBidder"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _json(resp):
    return json.loads(resp.body)


@pytest.fixture
def closet_env(tmp_path, monkeypatch):
    path = str(tmp_path / "onchain_testnet.db")
    c = init_onchain_db(path)
    es.init_economy_schema(c)
    for owner in (ME, BIDDER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, ME, [("Head", "Crown", 1)], [])
    c.commit()
    c.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", path)
    for name, value in {
        "XRPL_NETWORK": "testnet",
        "ECONOMY_NETWORK": "testnet",
        "ECONOMY_ENABLED": True,
        "CLOSET_MARKET_ENABLED": True,
        "CLOSET_MARKET_ENC_KEY": Fernet.generate_key().decode(),
        "CLOSET_MARKET_FEE_BPS": 700,
        "WEBAPP_DEV_MODE": True,
    }.items():
        monkeypatch.setattr(server.config, name, value)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", ME)
    scheduled = []
    monkeypatch.setattr(
        server, "_schedule_closet_settle", lambda fid: scheduled.append(("fill", fid))
    )
    monkeypatch.setattr(server, "_schedule_closet_bid", lambda oid: scheduled.append(("bid", oid)))

    async def no_token(user):
        return None

    monkeypatch.setattr(server, "_push_token", no_token)

    async def brix_line(wallet, currency, issuer):
        return server.xrpl_ops.TrustlineState.PRESENT, Decimal("100")

    monkeypatch.setattr(server.xrpl_ops, "get_trustline_state", brix_line)
    ledger = {"index": 1000}

    async def validated_index():
        return ledger["index"]

    monkeypatch.setattr(server.xrpl_ops, "current_validated_ledger_index", validated_index)
    monkeypatch.setattr(server.config, "CLOSET_USER_TX_LEDGER_WINDOW", 300)
    server._CLOSET_BOOK_CACHE.clear()
    yield {"path": path, "scheduled": scheduled, "ledger": ledger}
    server._CLOSET_BOOK_CACHE.clear()


def _db(env):
    c = init_onchain_db(env["path"])
    es.init_economy_schema(c)
    return c


def _req(method, path, body=None, match_info=None):
    req = make_mocked_request(method, path, match_info=match_info or {})

    async def _body():
        return body or {}

    req.json = _body  # type: ignore[method-assign]
    return req


def _open_bid(env, owner=BIDDER, price="10"):
    c = _db(env)
    bid = cms.create_pending_bid(
        c,
        owner=owner,
        slot="Head",
        value="Crown",
        price_brix=price,
        platform=None,
        condition="C",
        fulfillment_enc="S",
        cancel_after=9,
        payload_uuid=None,
        xumm_url=None,
        qr_url=None,
        push=None,
    )
    bid = cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E" + bid["id"], escrow_owner_seq=3)
    c.close()
    return bid


def test_ask_create_encumbers_and_invalidates_book(closet_env):
    _run(server.handle_closet_book(_req("GET", "/api/closet/book")))
    assert server._CLOSET_BOOK_CACHE
    resp = _run(
        server.handle_closet_ask_create(
            _req(
                "POST", "/api/closet/ask", {"slot": "Head", "value": "Crown", "price_brix": "12.50"}
            )
        )
    )
    assert resp.status == 200
    body = _json(resp)
    assert body["order"]["price_brix"] == "12.5" and body["fill"] is None
    assert "fulfillment_enc" not in body["order"]
    assert not server._CLOSET_BOOK_CACHE
    rows = _json(_run(server.handle_closet_book(_req("GET", "/api/closet/book"))))["rows"]
    assert rows[0]["best_ask_brix"] == "12.5"


def test_ask_create_rejects_bad_price_and_unheld(closet_env):
    bad = _run(
        server.handle_closet_ask_create(
            _req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "0"})
        )
    )
    assert bad.status == 400
    unheld = _run(
        server.handle_closet_ask_create(
            _req("POST", "/", {"slot": "Head", "value": "Tiara", "price_brix": "1"})
        )
    )
    assert unheld.status == 409 and _json(unheld)["code"] == "not_available"


def test_ask_create_refuses_none_value(closet_env):
    """(#516 D5) "None" is an empty slot, not a real asset — refused even
    though the store never checked whether ME actually holds one."""
    resp = _run(
        server.handle_closet_ask_create(
            _req("POST", "/", {"slot": "Head", "value": "None", "price_brix": "1"})
        )
    )
    assert resp.status == 400 and _json(resp)["code"] == "invalid_asset"


def test_ask_crossing_a_bid_schedules_settlement(closet_env):
    _open_bid(closet_env, price="10")
    body = _json(
        _run(
            server.handle_closet_ask_create(
                _req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "9"})
            )
        )
    )
    assert body["fill"]["state"] == "pending" and body["fill"]["role"] == "seller"
    assert closet_env["scheduled"] == [("fill", body["fill"]["id"])]


def test_kill_switch_blocks_new_orders_but_not_cancel(closet_env, monkeypatch):
    order = _json(
        _run(
            server.handle_closet_ask_create(
                _req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "3"})
            )
        )
    )["order"]
    monkeypatch.setattr(server.config, "CLOSET_MARKET_ENABLED", False)
    blocked = _run(
        server.handle_closet_ask_create(
            _req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "3"})
        )
    )
    assert blocked.status == 403 and _json(blocked)["code"] == "closet_market_disabled"
    resp = _run(
        server.handle_closet_ask_cancel(_req("DELETE", "/", match_info={"order_id": order["id"]}))
    )
    assert resp.status == 200 and _json(resp)["order"]["state"] == "cancelled"


def test_orders_mine_lists_bids_on_my_traits(closet_env):
    bid = _open_bid(closet_env)
    body = _json(_run(server.handle_closet_orders_mine(_req("GET", "/api/closet/orders/mine"))))
    assert [b["id"] for b in body["bids_on_my_traits"]] == [bid["id"]]
    assert body["bids_on_my_traits"][0]["source"] == "closet"


def _stub_trait_art(monkeypatch):
    calls = []

    def resolve(cfg, slot, value):
        calls.append((slot, value))
        return f"/api/layer?trait={slot}&value={value}"

    monkeypatch.setattr(server, "_trait_image_url", resolve)
    return calls


def test_closet_book_rows_carry_trait_image_url(closet_env, monkeypatch):
    """Prod 2026-09-18: /api/closet/book rows were slot/value only, so the
    Wanted and Closet listings chips guessed the art under the active
    character's body (mineTraitImgSrc) and rendered blank tiles. Trait art
    rarely lives there — the server picks the display body, as it does for
    every other trait tile."""
    _stub_trait_art(monkeypatch)
    _open_bid(closet_env)
    rows = _json(_run(server.handle_closet_book(_req("GET", "/api/closet/book"))))["rows"]
    assert [(r["slot"], r["value"], r["image_url"]) for r in rows] == [
        ("Head", "Crown", "/api/layer?trait=Head&value=Crown")
    ]


def test_closet_orders_mine_rows_carry_trait_image_url(closet_env, monkeypatch):
    """Same blank-tile bug on Mine: my orders, bids on my traits and recent
    fills are all trait chips, so every row needs the server's image_url."""
    calls = _stub_trait_art(monkeypatch)
    bidder2 = "rBidder2"
    c = _db(closet_env)
    es.set_closet_token(c, bidder2, f"C-{bidder2}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, ME, [("Head", "Crown", 2), ("Eyes", "Laser", 1)], [])
    c.commit()
    c.close()
    filled = _open_bid(closet_env)
    _open_bid(closet_env, owner=bidder2)  # stays open: ME still holds a second Crown
    fill = _run(
        server.handle_closet_bid_fill(_req("POST", "/", match_info={"order_id": filled["id"]}))
    )
    assert fill.status == 200
    ask = _run(
        server.handle_closet_ask_create(
            _req("POST", "/", {"slot": "Eyes", "value": "Laser", "price_brix": "5"})
        )
    )
    assert ask.status == 200
    calls.clear()
    body = _json(_run(server.handle_closet_orders_mine(_req("GET", "/api/closet/orders/mine"))))
    for group in ("orders", "fills", "bids_on_my_traits"):
        assert body[group], group
        for row in body[group]:
            want = f"/api/layer?trait={row['slot']}&value={row['value']}"
            assert row["image_url"] == want, group
    # PR #549 review: the resolver probes the layer tree, and bids_on_my_traits
    # is unbounded — Head: Crown is both a fill and an open bid here, so each
    # distinct (slot, value) must be resolved once per request, not per row.
    assert sorted(calls) == [("Eyes", "Laser"), ("Head", "Crown")]


def test_fill_bid_and_status_are_party_only(closet_env, monkeypatch):
    bid = _open_bid(closet_env)
    resp = _run(
        server.handle_closet_bid_fill(_req("POST", "/", match_info={"order_id": bid["id"]}))
    )
    assert resp.status == 200
    view = _json(resp)
    assert (view["kind"], view["state"], view["role"]) == ("closet_fill", "pending", "seller")
    status = _run(
        server.handle_closet_fill_status(_req("GET", "/", match_info={"fill_id": view["id"]}))
    )
    assert status.status == 200
    monkeypatch.setattr(mock_economy, "DEV_OWNER", "rStranger")
    denied = _run(
        server.handle_closet_fill_status(_req("GET", "/", match_info={"fill_id": view["id"]}))
    )
    assert denied.status == 404


def test_fill_view_state_mapping():
    base = {
        "id": "F",
        "seller": "rS",
        "buyer": "rB",
        "slot": "Head",
        "value": "Crown",
        "price_brix": "1",
        "fee_brix": "0",
        "overshoot_brix": "0",
        "error": None,
        "qr_url": None,
        "xumm_url": None,
        "push": None,
        "funds_source": "payment",
        "signed_txid": None,
    }
    assert (
        server._closet_fill_view({**base, "state": "funds_pending"}, "rB")["state"]
        == "awaiting_signature"
    )
    # (#503 fix round 2) only the BUYER owes this Payment signature -- the
    # seller's view of the exact same row must not also read as something
    # they need to sign.
    assert server._closet_fill_view({**base, "state": "funds_pending"}, "rS")["state"] == "pending"
    assert (
        server._closet_fill_view({**base, "state": "funds_pending", "signed_txid": "T"}, "rB")[
            "state"
        ]
        == "pending"
    )
    # (#443 final review I1) the seller is not paid at asset_moved — only the buyer is done
    assert server._closet_fill_view({**base, "state": "asset_moved"}, "rB")["state"] == "done"
    assert server._closet_fill_view({**base, "state": "asset_moved"}, "rS")["state"] == "pending"
    for settled in ("paid", "mirrored"):
        assert server._closet_fill_view({**base, "state": settled}, "rS")["state"] == "done"
        assert server._closet_fill_view({**base, "state": settled}, "rB")["state"] == "done"
    assert server._closet_fill_view({**base, "state": "refunded"}, "rB")["state"] == "failed"


def test_bid_view_state_mapping():
    """(#443 final review I1) a match can still abort — only open and filled are done."""
    base = {
        "id": "O",
        "side": "bid",
        "slot": "Head",
        "value": "Crown",
        "price_brix": "1",
        "created_ts": 1,
        "cancel_after": 9,
        "error": None,
        "signed_txid": "T",
        "qr_url": None,
        "xumm_url": None,
        "push": None,
    }
    expected = {
        "open": "done",
        "filled": "done",
        "matched": "pending",
        "cancelling": "pending",
        "cancelled": "failed",
        "expired": "failed",
    }
    for order_state, view_state in expected.items():
        view = server._closet_bid_view({**base, "state": order_state})
        assert (view["state"], view["order_state"]) == (view_state, order_state)


def _no_brix_line(monkeypatch, state="ABSENT"):
    seen = []

    async def fake(wallet, currency, issuer):
        seen.append((wallet, currency, issuer))
        return getattr(server.xrpl_ops.TrustlineState, state), None

    monkeypatch.setattr(server.xrpl_ops, "get_trustline_state", fake)
    return seen


def test_ask_create_requires_a_brix_trustline(closet_env, monkeypatch):
    """(#443 final review C1) A seller without a BRIX line would get a forward
    payment that fails forever — refuse the ask up front."""
    body = {"slot": "Head", "value": "Crown", "price_brix": "3"}
    seen = _no_brix_line(monkeypatch)
    resp = _run(server.handle_closet_ask_create(_req("POST", "/", body)))
    assert resp.status == 409 and _json(resp) == {
        "error": "a BRIX trustline is required",
        "code": "trustline_required",
    }
    assert seen == [(ME, server.config.BRIX_CURRENCY_HEX, server.config.BRIX_ISSUER)]
    c = _db(closet_env)
    assert c.execute("SELECT COUNT(*) FROM closet_orders").fetchone()[0] == 0
    c.close()
    _no_brix_line(monkeypatch, state="UNKNOWN")
    resp = _run(server.handle_closet_ask_create(_req("POST", "/", body)))
    assert resp.status == 503 and _json(resp)["code"] == "balance_unavailable"


def test_bid_fill_requires_a_brix_trustline(closet_env, monkeypatch):
    bid = _open_bid(closet_env)
    _no_brix_line(monkeypatch)
    resp = _run(
        server.handle_closet_bid_fill(_req("POST", "/", match_info={"order_id": bid["id"]}))
    )
    assert resp.status == 409 and _json(resp)["code"] == "trustline_required"
    c = _db(closet_env)
    assert c.execute("SELECT COUNT(*) FROM closet_fills").fetchone()[0] == 0
    c.close()
    _no_brix_line(monkeypatch, state="UNKNOWN")
    resp = _run(
        server.handle_closet_bid_fill(_req("POST", "/", match_info={"order_id": bid["id"]}))
    )
    assert resp.status == 503 and _json(resp)["code"] == "balance_unavailable"
    assert closet_env["scheduled"] == []


def test_ask_buy_requires_a_brix_trustline(closet_env, monkeypatch):
    """(#443 final review C1) An XRP (SendMax) buyer without a BRIX line could
    never receive a refund if the ask is gone by the time the payment lands."""
    c = _db(closet_env)
    ask = cms.create_ask(c, owner=ME, slot="Head", value="Crown", price_brix="5", platform=None)
    c.close()
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BIDDER)
    detected = []

    async def detect(wallet, amount, **kwargs):
        detected.append(wallet)
        return "XRP", "2.5"

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", detect)
    _no_brix_line(monkeypatch)
    resp = _run(server.handle_closet_ask_buy(_req("POST", "/", match_info={"order_id": ask["id"]})))
    assert resp.status == 409 and _json(resp)["code"] == "trustline_required"
    _no_brix_line(monkeypatch, state="UNKNOWN")
    resp = _run(server.handle_closet_ask_buy(_req("POST", "/", match_info={"order_id": ask["id"]})))
    assert resp.status == 503 and _json(resp)["code"] == "balance_unavailable"
    assert detected == []
    c = _db(closet_env)
    assert c.execute("SELECT COUNT(*) FROM closet_fills").fetchone()[0] == 0
    c.close()


def test_price_beyond_15_significant_digits_is_refused(closet_env):
    """(#443 final review I5) an XRPL IOU amount carries at most 15
    significant digits; a longer price can never be escrowed or paid."""
    resp = _run(
        server.handle_closet_ask_create(
            _req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "1000000000.000001"})
        )
    )
    assert resp.status == 400 and _json(resp)["code"] == "bad_request"
    assert server._parse_brix_price("100000000.000001") == "100000000.000001"  # exactly 15
    assert server._parse_brix_price("1000000000000000") == "1000000000000000"  # trailing zeros


def test_closet_book_cache_is_bounded_and_drops_stale_entries(closet_env):
    """PR #502 C2: every distinct (slot, value) query adds a key, so the cache
    must evict — stale entries first, then the oldest past the max."""
    assert server._CLOSET_BOOK_CACHE_MAX == 64
    for i in range(server._CLOSET_BOOK_CACHE_MAX + 20):
        server._closet_book_cache_put(("testnet", "Head", f"V{i}"), {"i": i}, 1000.0 + i)
        assert len(server._CLOSET_BOOK_CACHE) <= server._CLOSET_BOOK_CACHE_MAX
    newest = ("testnet", "Head", f"V{server._CLOSET_BOOK_CACHE_MAX + 19}")
    assert newest in server._CLOSET_BOOK_CACHE
    assert ("testnet", "Head", "V0") not in server._CLOSET_BOOK_CACHE
    later = 1000.0 + server._CLOSET_BOOK_CACHE_MAX + 20 + server._CLOSET_BOOK_TTL
    server._closet_book_cache_put(("testnet", None, None), {"fresh": True}, later)
    assert list(server._CLOSET_BOOK_CACHE) == [("testnet", None, None)]


def test_closet_book_handler_uses_the_bounded_cache(closet_env, monkeypatch):
    monkeypatch.setattr(server, "_CLOSET_BOOK_CACHE_MAX", 3)
    for i in range(6):
        _run(server.handle_closet_book(_req("GET", f"/api/closet/book?slot=Head&value=V{i}")))
    assert len(server._CLOSET_BOOK_CACHE) == 3
