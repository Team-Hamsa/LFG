# tests/test_audit_swap_preconditions.py
# #166 Seam 2: ops audit asserting the NFT issuer holds the BRIX trustline the
# swap replacement offers are priced in (missing -> tecNO_LINE after the burn).
#
# Env-guard preamble (copy from tests/test_swap_offer_recovery.py).
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

import asyncio  # noqa: E402
import sys  # noqa: E402
from decimal import Decimal  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import audit_swap_preconditions  # noqa: E402

from lfg_core import config, xrpl_ops  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _amock(return_value=None):
    async def fn(*a, **k):
        return return_value

    return fn


def _split_issuers(monkeypatch):
    monkeypatch.setattr(config, "SWAP_ISSUER_ADDRESS", "rNFTISSUERxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(config, "SWAP_OFFER_ISSUER", "rBRIXISSUERxxxxxxxxxxxxxxxxxxxxx")


def test_trustline_present_ok(monkeypatch):
    _split_issuers(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(Decimal("0")))
    code, msg = _run(audit_swap_preconditions.check())
    assert code == 0
    assert "OK" in msg


def test_trustline_missing_fails_with_remediation(monkeypatch):
    _split_issuers(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(None))
    code, msg = _run(audit_swap_preconditions.check())
    assert code == 1
    assert "trustline" in msg.lower()
    assert config.SWAP_ISSUER_ADDRESS in msg


def test_same_issuer_config_trivially_ok(monkeypatch):
    # Testnet shape: the NFT issuer IS the BRIX issuer — an account implicitly
    # "holds" its own IOU; no cross-account trustline exists or is needed.
    monkeypatch.setattr(config, "SWAP_ISSUER_ADDRESS", "rSAMExxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(config, "SWAP_OFFER_ISSUER", "rSAMExxxxxxxxxxxxxxxxxxxxxxxxxxx")

    async def boom(*a, **k):  # must not even be consulted
        raise AssertionError("lookup should be skipped")

    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", boom)
    code, msg = _run(audit_swap_preconditions.check())
    assert code == 0
