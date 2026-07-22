"""tests/test_shop_config.py — Shop config defaults and memo action.

Env-guard preamble (copy from test_market_flow.py): importing lfg_core.config
freezes its constants at import time; set the same defaults test_smoke.py uses
so collection order can't strand them.
"""

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

import importlib

import pytest

from lfg_core import config, memos


@pytest.fixture
def _default_env(monkeypatch):
    """Re-parse config with the shop knobs explicitly at their defaults.

    The constants freeze at import, so a deployment .env that tunes them (prod
    currently runs SHOP_BASE_BRIX=10.0) makes a bare `config.SHOP_BASE_BRIX ==
    1.0` red in every checkout and blocks the pre-push gate for unrelated work.
    Set the values, never delenv — load_dotenv() repopulates a deleted var on
    reload (the eb717cb lesson). The module is restored afterward so later test
    modules see the normal posture.
    """
    for name, default in (
        ("SHOP_BASE_BRIX", "1.0"),
        ("SHOP_MIN_BRIX", "5"),
        ("SHOP_MAX_BRIX", "5000"),
        ("SHOP_OFFER_TTL_SECONDS", "900"),
        ("ASSEMBLE_TAXON", "1760"),
        ("TRAIT_TAXON", "176"),
    ):
        monkeypatch.setenv(name, default)
    importlib.reload(config)
    yield config
    importlib.reload(config)


def test_shop_config_defaults(_default_env):
    cfg = _default_env
    assert cfg.SHOP_BASE_BRIX == 1.0
    assert cfg.SHOP_MIN_BRIX == 5
    assert cfg.SHOP_MAX_BRIX == 5000
    assert cfg.SHOP_OFFER_TTL_SECONDS == 900
    assert cfg.ASSEMBLE_TAXON == 1760
    assert cfg.TRAIT_TAXON == 176  # default flipped from 1763


def test_shop_buy_memo_action():
    assert memos.ACTION_SHOP_BUY == "shop-buy"
    # closed enum accepts it (raises on unknown actions)
    m = memos.build_memos_json(
        memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_SHOP_BUY
    )
    assert m
