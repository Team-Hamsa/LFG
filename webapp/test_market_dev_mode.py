# webapp/test_market_dev_mode.py
# Task 10 (#44): before this task, the marketplace handlers had no
# WEBAPP_DEV_MODE branch at all (unlike /api/economy, /api/equip, etc. —
# see webapp/mock_economy.py) so the panel could not be manually verified
# offline. These tests exercise the dev-mode branches added to
# lfg_service/app.py's market handlers, mirroring webapp/test_smoke.py's
# make_mocked_request + monkeypatch(server.config.WEBAPP_DEV_MODE) pattern
# (see e.g. test_economy_dev_mode_read, test_equip_missing_body_field_returns_400).
import asyncio
import json
import os
import sys

import pytest
from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_service import app as server  # noqa: E402
from webapp import mock_economy, mock_market  # noqa: E402


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    # Fresh mock state per test (both singletons are module-level; MockMarket
    # reads mock_economy.INSTANCE directly, so both must reset together).
    mock_economy.INSTANCE = mock_economy.MockEconomy()
    server.mock_market.INSTANCE = mock_market.MockMarket()
    yield


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_listings_dev_mode_character():
    req = make_mocked_request("GET", "/api/market/listings?kind=character")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["rows"] and all(r["kind"] == "character" for r in body["rows"])
    assert "total" in body


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_listings_dev_mode_trait_filter():
    req = make_mocked_request("GET", "/api/market/listings?kind=trait&trait=Head:Tophat")
    resp = _run(server.handle_market_listings(req))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["rows"]
    assert all(r["slot"] == "Head" and r["value"] == "Tophat" for r in body["rows"])


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_mine_dev_mode_returns_dev_owner_groups():
    req = make_mocked_request("GET", "/api/market/mine")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER
    resp = _run(server.handle_market_mine(req))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["unlisted_characters"]
    assert body["closet_assets"]


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_history_dev_mode_by_slot_value():
    req = make_mocked_request("GET", "/api/market/history?slot=Head&value=Tophat")
    resp = _run(server.handle_market_history(req))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["slot"] == "Head" and body["value"] == "Tophat" and body["sales"] == []


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_list_start_and_poll_to_done_dev_mode():
    econ = mock_economy.INSTANCE
    nft_id = econ.characters[0]["nft_id"]

    req = make_mocked_request("POST", "/api/market/list")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER

    async def body_json():
        return {"nft_id": nft_id, "price_xrp": "10"}

    req.json = body_json  # type: ignore[method-assign]

    resp = _run(server.handle_market_list_start(req))
    assert resp.status == 200
    sid = json.loads(resp.body)["id"]

    status_req = make_mocked_request("GET", f"/api/market/list/{sid}")
    status_req["user"] = {"id": "dev", "name": "dev"}
    status_req.match_info["session_id"] = sid

    state = None
    for _ in range(10):
        resp = _run(server.handle_market_list_status(status_req))
        state = json.loads(resp.body)["state"]
        if state == "done":
            break
    assert state == "done"


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_cancel_start_dev_mode_not_found():
    req = make_mocked_request("POST", "/api/market/cancel")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER

    async def body_json():
        return {"offer_index": "NOPE"}

    req.json = body_json  # type: ignore[method-assign]

    resp = _run(server.handle_market_cancel_start(req))
    assert resp.status == 404


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_buy_start_dev_mode_trait_without_closet_403():
    row = next(
        r for r in server.mock_market.INSTANCE._listings if r["kind"] == "trait" and r["is_live"]
    )

    req = make_mocked_request("POST", "/api/market/buy")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER

    async def body_json():
        return {"offer_index": row["offer_index"]}

    req.json = body_json  # type: ignore[method-assign]

    resp = _run(server.handle_market_buy_start(req))
    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "closet_required"


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_trait_list_start_dev_mode_requires_closet_400():
    req = make_mocked_request("POST", "/api/market/trait/list")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER

    async def body_json():
        return {"slot": "Head", "value": "Halo", "price_brix": "5"}

    req.json = body_json  # type: ignore[method-assign]

    resp = _run(server.handle_market_trait_list_start(req))
    assert resp.status == 400
    assert "Closet" in json.loads(resp.body)["error"]


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_trait_list_wizard_dev_mode_progresses_to_listed():
    econ = mock_economy.INSTANCE
    econ.create_closet(mock_economy.DEV_OWNER)
    econ.create_closet(mock_economy.DEV_OWNER)  # -> active

    req = make_mocked_request("POST", "/api/market/trait/list")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER

    async def body_json():
        return {"slot": "Head", "value": "Halo", "price_brix": "5"}

    req.json = body_json  # type: ignore[method-assign]

    resp = _run(server.handle_market_trait_list_start(req))
    assert resp.status == 200
    sid = json.loads(resp.body)["id"]

    status_req = make_mocked_request("GET", f"/api/market/trait/list/{sid}")
    status_req["user"] = {"id": "dev", "name": "dev"}
    status_req.match_info["session_id"] = sid

    state = None
    for _ in range(10):
        resp = _run(server.handle_market_trait_list_status(status_req))
        state = json.loads(resp.body)["state"]
        if state == "listed":
            break
    assert state == "listed"
