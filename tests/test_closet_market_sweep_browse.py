import asyncio

from lfg_core import closet_market_store as cms
from lfg_service import app as server
from tests import test_closet_market_api as api_tests
from tests.test_closet_market_api import ME, _db, _open_bid

closet_env = api_tests.closet_env  # re-export the fixture (a direct import trips ruff F811)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_sweep_advances_bids_then_settles_fills(closet_env, monkeypatch):
    bid = _open_bid(closet_env)
    order_calls, fill_calls = [], []

    async def fake_advance(order_id, deps):
        order_calls.append(order_id)
        return "NEWFILL" if order_id == bid["id"] else None

    async def fake_settle(fill_id, deps):
        fill_calls.append(fill_id)

    monkeypatch.setattr(server.closet_market_flow, "advance_bid", fake_advance)
    monkeypatch.setattr(server.closet_market_flow, "settle_fill", fake_settle)
    monkeypatch.setattr(server, "_closet_market_deps", lambda: None)
    _run(server.sweep_closet_market())
    assert bid["id"] in order_calls
    assert fill_calls == ["NEWFILL"]


def test_sweep_is_a_noop_when_not_settleable(closet_env, monkeypatch):
    monkeypatch.setattr(server.config, "CLOSET_MARKET_ENC_KEY", "")
    called = []

    async def fake_advance(order_id, deps):
        called.append(order_id)

    monkeypatch.setattr(server.closet_market_flow, "advance_bid", fake_advance)
    _open_bid(closet_env)
    _run(server.sweep_closet_market())
    assert called == []


def test_attach_closet_book_keys_by_raw_slot_value():
    raw = [{"slot": "Head", "value": "Crown"}, {"slot": "Eyes", "value": "Laser"}]
    out = [{"nft_id": "A"}, {"nft_id": "B"}]
    summary = [
        {
            "slot": "Head",
            "value": "Crown",
            "best_ask_brix": None,
            "ask_count": 0,
            "best_bid_brix": "10",
            "bid_count": 1,
        }
    ]
    server._attach_closet_book(out, raw, summary)
    assert out[0]["book"]["best_bid_brix"] == "10"
    assert out[1]["book"] is None


def test_mine_closet_assets_report_listed_counts(closet_env):
    c = _db(closet_env)
    cms.create_ask(c, owner=ME, slot="Head", value="Crown", price_brix="5", platform=None)
    c.close()
    data = server._compute_mine_data("testnet", "testnet", ME)
    [asset] = data["closet_assets"]
    assert (asset["count"], asset["listed"]) == (1, 1)
