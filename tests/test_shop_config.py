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

from lfg_core import config, memos


def test_shop_config_defaults():
    assert config.SHOP_BASE_BRIX == 1.0
    assert config.SHOP_MIN_BRIX == 5
    assert config.SHOP_MAX_BRIX == 5000
    assert config.SHOP_OFFER_TTL_SECONDS == 900
    assert config.ASSEMBLE_TAXON == 1760
    assert config.TRAIT_TAXON == 176  # default flipped from 1763


def test_shop_buy_memo_action():
    assert memos.ACTION_SHOP_BUY == "shop-buy"
    # closed enum accepts it (raises on unknown actions)
    m = memos.build_memos_json(
        memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_SHOP_BUY
    )
    assert m
