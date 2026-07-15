# Env guard: set before lfg_core imports so frozen config constants are sane
# when this file runs first (see test-env-guard convention).
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

from lfg_core import bulk_mint_flow, config, mint_credits  # noqa: E402


def test_cap_hit_mid_fulfillment_becomes_credit(monkeypatch, tmp_path):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    monkeypatch.setattr(bulk_mint_flow.db_path, "app_db_path", lambda net: str(tmp_path / "app.db"))
    # headroom exists at request time (clamp) ...
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    # ... but the cap is fully consumed by the time fulfillment runs:
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 10000)

    calls = {"mint": 0}

    async def _count_mint(**kw):
        calls["mint"] += 1
        raise AssertionError("mint_one_unit must not be called when the cap is hit")

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 2, platform="discord")
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    # no unit could mint; both converted to credit, none lost
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", j.network) == 2
    assert all(u.state == bulk_mint_flow.UNIT_FAILED for u in j.units)
    # the cap re-check prevents any mint attempt, rather than relying on
    # unmocked network code to incidentally fail
    assert calls["mint"] == 0
