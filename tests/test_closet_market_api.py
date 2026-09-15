import asyncio
import json

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
    server._CLOSET_BOOK_CACHE.clear()
    yield {"path": path, "scheduled": scheduled}
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
    assert (
        server._closet_fill_view({**base, "state": "funds_pending", "signed_txid": "T"}, "rB")[
            "state"
        ]
        == "pending"
    )
    assert server._closet_fill_view({**base, "state": "asset_moved"}, "rB")["state"] == "done"
    assert server._closet_fill_view({**base, "state": "refunded"}, "rB")["state"] == "failed"
