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

from lfg_core import bulk_mint_flow  # noqa: E402


def _paid_job(tmp_path, monkeypatch, state):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 3, platform="discord")
    j.entitlement = bulk_mint_flow.entitlement.PaymentEntitlement(quantity=3)
    j.quantity = 3
    j.units = [bulk_mint_flow.Unit(index=i) for i in range(3)]
    j.pay_with, j.pay_amount, j.unit_price = "XRP", "30", "10"
    j.state = state
    return j


def test_persist_and_reload_roundtrip(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    j.units[0].state = bulk_mint_flow.OFFERED
    j.units[0].nft_id = "N0"
    bulk_mint_flow.persist(j)
    reloaded = bulk_mint_flow.load_all_resumable()
    assert len(reloaded) == 1
    r = reloaded[0]
    assert r.id == j.id
    assert r.wallet_address == "rUSER"
    assert r.units[0].state == bulk_mint_flow.OFFERED
    assert r.units[0].nft_id == "N0"
    assert r.entitlement.quantity == 3


def test_terminal_jobs_not_resumable(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.DONE)
    bulk_mint_flow.persist(j)
    assert bulk_mint_flow.load_all_resumable() == []


def test_delete_record(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    bulk_mint_flow.persist(j)
    bulk_mint_flow.delete_record(j.id)
    assert bulk_mint_flow.load_all_resumable() == []
