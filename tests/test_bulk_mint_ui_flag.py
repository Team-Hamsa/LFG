# tests/test_bulk_mint_ui_flag.py
# BULK_MINT_UI_ENABLED flag (#215 UI): default off; surfaced via /api/config
# so the no-build client can gate the quantity stepper without a deploy.
#
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants at import time; set the same defaults test_bulk_mint_flow.py /
# test_smoke.py use so collection order can't strand them.
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402
import json  # noqa: E402

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import config  # noqa: E402
from lfg_service import app as server  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_flag_defaults_off():
    assert config.BULK_MINT_UI_ENABLED is False


def test_config_endpoint_carries_bulk_fields(monkeypatch):
    monkeypatch.setattr(server.config, "BULK_MINT_UI_ENABLED", True)
    resp = _run(server.handle_config(make_mocked_request("GET", "/api/config")))
    body = json.loads(resp.body)
    assert body["bulk_mint_ui"] is True
    assert body["bulk_mint_max"] == server.config.BULK_MINT_MAX


def test_config_endpoint_bulk_ui_off_by_default():
    resp = _run(server.handle_config(make_mocked_request("GET", "/api/config")))
    body = json.loads(resp.body)
    assert body["bulk_mint_ui"] is False
