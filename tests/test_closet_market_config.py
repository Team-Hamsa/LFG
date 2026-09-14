import asyncio
import json

import pytest
from aiohttp.test_utils import make_mocked_request

from lfg_core import config, memos
from lfg_service import app as server


def test_closet_market_ships_off():
    assert config.env_flag("CLOSET_MARKET_ENABLED", config.CLOSET_MARKET_ENABLED_DEFAULT) is False


def test_enabled_needs_economy_flag_and_key(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "")
    assert config.closet_market_enabled() is False
    assert config.closet_market_settleable() is False
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "k")
    assert config.closet_market_enabled() is True
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", False)
    assert config.closet_market_enabled() is False
    assert config.closet_market_settleable() is True  # kill switch keeps settlement alive
    monkeypatch.setattr(config, "ECONOMY_ENABLED", False)
    assert config.closet_market_settleable() is False


@pytest.mark.parametrize("bps", [-1, 10000, 25000])
def test_fee_bps_out_of_range_rejected(bps):
    with pytest.raises(ValueError):
        config.validate_closet_market_fee_bps(bps)


@pytest.mark.parametrize("bps", [0, 700, 9999])
def test_fee_bps_in_range_ok(bps):
    config.validate_closet_market_fee_bps(bps)


@pytest.mark.parametrize(
    "action", ["closet-bid", "closet-buy", "closet-fill", "closet-forward", "closet-refund"]
)
def test_closet_memo_actions_are_in_the_closed_enum(action):
    assert memos.build_memos_json(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, action)
    assert memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, action)


def test_config_endpoint_exposes_closet_market(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "k")
    monkeypatch.setattr(config, "CLOSET_MARKET_FEE_BPS", 250)
    resp = asyncio.new_event_loop().run_until_complete(
        server.handle_config(make_mocked_request("GET", "/api/config"))
    )
    body = json.loads(resp.body)
    assert body["closet_market_enabled"] is True
    assert body["closet_market_fee_bps"] == 250
    assert body["closet_bid_ttl_seconds"] == config.CLOSET_BID_TTL_SECONDS
