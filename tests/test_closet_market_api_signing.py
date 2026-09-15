from decimal import Decimal

from lfg_core import closet_market_store as cms
from lfg_core import crypto_condition
from lfg_service import app as server
from tests import test_closet_market_api as api_tests
from tests.test_closet_market_api import BIDDER, ME, _db, _json, _open_bid, _req, _run

closet_env = api_tests.closet_env  # re-export the fixture (a direct import trips ruff F811)


def _trustline(monkeypatch, state="PRESENT", balance="50"):
    async def fake(wallet, currency, issuer):
        return getattr(server.xrpl_ops.TrustlineState, state), (
            Decimal(balance) if balance is not None else None
        )

    monkeypatch.setattr(server.xrpl_ops, "get_trustline_state", fake)


def _capture_bid_payload(monkeypatch, result=None):
    seen = {}

    async def fake(account, amount, destination, condition, cancel_after, **kwargs):
        seen.update(
            account=account,
            amount=amount,
            destination=destination,
            condition=condition,
            cancel_after=cancel_after,
        )
        return (
            result
            if result is not None
            else {
                "uuid": "BU",
                "xumm_url": "https://xumm.app/sign/BU",
                "qr_url": "q",
                "push": "sent",
            }
        )

    monkeypatch.setattr(server.xumm_ops, "create_closet_bid_payload", fake)
    return seen


def test_bid_create_builds_escrow_payload_and_seals_fulfillment(closet_env, monkeypatch):
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: True)
    _trustline(monkeypatch)
    seen = _capture_bid_payload(monkeypatch)
    resp = _run(
        server.handle_closet_bid_create(
            _req("POST", "/api/closet/bid", {"slot": "Head", "value": "Tiara", "price_brix": "10"})
        )
    )
    assert resp.status == 200
    view = _json(resp)
    assert (view["kind"], view["state"], view["xumm_url"]) == (
        "closet_bid",
        "awaiting_signature",
        "https://xumm.app/sign/BU",
    )
    assert seen["account"] == ME and seen["destination"] == server.config.SIGNING_ACCOUNT
    assert seen["amount"]["value"] == "10"
    c = _db(closet_env)
    row = cms.get_order(c, view["id"])
    c.close()
    assert row["condition"] == seen["condition"] and row["cancel_after"] == seen["cancel_after"]
    fulfillment = crypto_condition.unseal(row["fulfillment_enc"])
    assert crypto_condition.condition_hex(bytes.fromhex(fulfillment[8:])) == seen["condition"]


def test_bid_create_refusals(closet_env, monkeypatch):
    body = {"slot": "Head", "value": "Tiara", "price_brix": "10"}
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: False)
    assert _run(server.handle_closet_bid_create(_req("POST", "/", body))).status == 404
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: True)
    _trustline(monkeypatch, state="ABSENT", balance=None)
    resp = _run(server.handle_closet_bid_create(_req("POST", "/", body)))
    assert resp.status == 409 and _json(resp)["code"] == "trustline_required"
    _trustline(monkeypatch, state="UNKNOWN", balance=None)
    assert _run(server.handle_closet_bid_create(_req("POST", "/", body))).status == 503
    _trustline(monkeypatch, balance="9.99")
    resp = _run(server.handle_closet_bid_create(_req("POST", "/", body)))
    assert resp.status == 409 and _json(resp)["code"] == "insufficient_brix"


def test_bid_create_refuses_a_second_live_bid(closet_env, monkeypatch):
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: True)
    _trustline(monkeypatch)
    _capture_bid_payload(monkeypatch)
    _open_bid(closet_env, owner=ME)
    resp = _run(
        server.handle_closet_bid_create(
            _req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "10"})
        )
    )
    assert resp.status == 409 and _json(resp)["code"] == "bid_exists"


def test_bid_status_advances_pending_bids_inline(closet_env, monkeypatch):
    c = _db(closet_env)
    bid = cms.create_pending_bid(
        c,
        owner=ME,
        slot="Head",
        value="Tiara",
        price_brix="1",
        platform=None,
        condition="C",
        fulfillment_enc="S",
        cancel_after=9,
        payload_uuid="BU",
        xumm_url="x",
        qr_url="q",
        push=None,
    )
    c.close()
    calls = []

    async def fake_advance(order_id, deps):
        calls.append(order_id)
        return "FILL1"

    monkeypatch.setattr(server.closet_market_flow, "advance_bid", fake_advance)
    monkeypatch.setattr(server, "_closet_market_deps", lambda: None)
    resp = _run(
        server.handle_closet_bid_status(_req("GET", "/", match_info={"order_id": bid["id"]}))
    )
    assert resp.status == 200 and calls == [bid["id"]]
    assert ("fill", "FILL1") in closet_env["scheduled"]


def test_bid_cancel_moves_to_cancelling_and_schedules(closet_env):
    bid = _open_bid(closet_env, owner=ME)
    resp = _run(
        server.handle_closet_bid_cancel(_req("DELETE", "/", match_info={"order_id": bid["id"]}))
    )
    assert resp.status == 200
    assert _json(resp)["order_state"] == "cancelling"
    assert ("bid", bid["id"]) in closet_env["scheduled"]
    other = _open_bid(closet_env, owner=BIDDER)
    denied = _run(
        server.handle_closet_bid_cancel(_req("DELETE", "/", match_info={"order_id": other["id"]}))
    )
    assert denied.status == 404


def test_ask_buy_builds_invoice_payment(closet_env, monkeypatch):
    c = _db(closet_env)
    ask = cms.create_ask(c, owner=ME, slot="Head", value="Crown", price_brix="5", platform=None)
    c.close()
    monkeypatch.setattr(server.mock_economy, "DEV_OWNER", BIDDER)

    async def xrp_path(wallet, amount, **kwargs):
        return "XRP", "2.5"

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", xrp_path)
    seen = {}

    async def fake_buy(account, amount, destination, invoice_id, **kwargs):
        seen.update(account=account, invoice_id=invoice_id, send_max=kwargs.get("send_max_drops"))
        return {"uuid": "PU", "xumm_url": "https://xumm.app/sign/PU", "qr_url": "q", "push": None}

    monkeypatch.setattr(server.xumm_ops, "create_closet_buy_payload", fake_buy)
    resp = _run(server.handle_closet_ask_buy(_req("POST", "/", match_info={"order_id": ask["id"]})))
    assert resp.status == 200
    view = _json(resp)
    assert (view["state"], view["role"], view["pay_with"]) == ("awaiting_signature", "buyer", "XRP")
    assert seen["account"] == BIDDER and seen["send_max"] == "2500000"
    assert seen["invoice_id"] == cms.invoice_id(view["id"])


def test_ask_buy_own_listing_is_refused(closet_env, monkeypatch):
    c = _db(closet_env)
    ask = cms.create_ask(c, owner=ME, slot="Head", value="Crown", price_brix="5", platform=None)
    c.close()

    async def brix_path(wallet, amount, **kwargs):
        return "BRIX", amount

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", brix_path)
    resp = _run(server.handle_closet_ask_buy(_req("POST", "/", match_info={"order_id": ask["id"]})))
    assert resp.status == 400 and _json(resp)["code"] == "self_cross"
