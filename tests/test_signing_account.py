# Mainnet issuer ops sign via a regular key: the signing wallet is built from
# SEED (the regkey seed) but the tx Account must be the issuer address, NOT the
# address derived from the seed. config.SIGNING_ACCOUNT carries that override
# (default: the SEED-derived address, i.e. legacy/testnet behavior) and every
# tx builder in xrpl_ops must use it as Account.

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import lfg_core.xrpl_ops as xrpl_ops  # noqa: E402
from lfg_core import config  # noqa: E402

ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, result: dict) -> None:
        self.result = result


def _patch(monkeypatch, captured):
    def fake_submit(tx, client, wallet):
        captured["tx"] = tx
        return _Resp(
            {
                "hash": "HASH",
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "nftoken_id": "NFTID",
                    "offer_id": "OFFERID",
                },
            }
        )

    def fake_request(self, req):
        return _Resp(
            {
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "nftoken_id": "NFTID",
                    "offer_id": "OFFERID",
                }
            }
        )

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", ISSUER)


def test_config_rejects_invalid_signing_account(monkeypatch):
    # A typo'd SIGNING_ACCOUNT must fail fast at config load, not surface as an
    # opaque temMALFORMED/actNotFound on every tx.
    import importlib

    import pytest

    monkeypatch.setenv("SIGNING_ACCOUNT", "rNotAValidAddress!!!")
    try:
        with pytest.raises(ValueError, match="SIGNING_ACCOUNT"):
            importlib.reload(config)
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_config_strips_signing_account_whitespace(monkeypatch):
    import importlib

    monkeypatch.setenv("SIGNING_ACCOUNT", f"  {ISSUER}  ")
    try:
        cfg = importlib.reload(config)
        assert cfg.SIGNING_ACCOUNT == ISSUER
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_config_default_is_seed_address():
    # With no SIGNING_ACCOUNT env override, behavior is unchanged: the account
    # is the one derived from SEED.
    from xrpl.wallet import Wallet

    assert config.SIGNING_ACCOUNT == Wallet.from_seed(config.SEED).classic_address


def test_mint_account_is_signing_account(monkeypatch):
    captured: dict = {}
    _patch(monkeypatch, captured)
    _run(xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=ISSUER))
    assert captured["tx"].account == ISSUER


def test_mint_omits_issuer_field_when_issuer_is_signing_account(monkeypatch):
    # With Account == issuer the NFTokenMint Issuer field must be omitted —
    # setting it would require NFTokenMinter authorization on the issuer.
    captured: dict = {}
    _patch(monkeypatch, captured)
    _run(xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=ISSUER))
    assert captured["tx"].issuer is None


def test_create_offer_account_is_signing_account(monkeypatch):
    captured: dict = {}
    _patch(monkeypatch, captured)
    _run(xrpl_ops.create_nft_offer("NFTID", "rDest", amount="0"))
    assert captured["tx"].account == ISSUER


def test_burn_account_is_signing_account(monkeypatch):
    captured: dict = {}
    _patch(monkeypatch, captured)
    _run(xrpl_ops.burn_nft("NFTID", owner="rOwner"))
    assert captured["tx"].account == ISSUER
    assert captured["tx"].owner == "rOwner"


def test_burn_omits_owner_when_held_by_signing_account(monkeypatch):
    captured: dict = {}
    _patch(monkeypatch, captured)
    _run(xrpl_ops.burn_nft("NFTID", owner=ISSUER))
    assert captured["tx"].owner is None


def test_modify_account_is_signing_account(monkeypatch):
    captured: dict = {}
    _patch(monkeypatch, captured)
    _run(xrpl_ops.modify_nft("NFTID", "rOwner", "https://x/new.json"))
    assert captured["tx"].account == ISSUER


def test_buy_and_burn_account_is_signing_account(monkeypatch):
    captured: dict = {}
    _patch(monkeypatch, captured)
    _run(
        xrpl_ops.buy_and_burn(
            config.SWAP_OFFER_CURRENCY_HEX,
            "rDifferentIssuerXXXXXXXXXXXXXXXXXXX",
            "10",
            max_xrp="10",
        )
    )
    assert captured["tx"].account == ISSUER


def test_buy_and_burn_self_issuer_noop_keys_on_signing_account(monkeypatch):
    # The "wallet IS the issuer" no-op guard must compare against the signing
    # account, not the seed-derived address.
    captured: dict = {}
    _patch(monkeypatch, captured)

    def boom(*a, **k):
        raise AssertionError("submit_and_wait must not be called in the self-issuer no-op")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", boom)
    result = _run(xrpl_ops.buy_and_burn(config.SWAP_OFFER_CURRENCY_HEX, ISSUER, "10"))
    assert result  # truthy sentinel


def test_bot_wallet_address_returns_signing_account(monkeypatch):
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", ISSUER)
    assert xrpl_ops.bot_wallet_address() == ISSUER


def test_admin_burn_account_is_signing_account(monkeypatch):
    # The Discord admin burn builds its NFTokenBurn inline; its Account must
    # also honor SIGNING_ACCOUNT (regkey signing on mainnet).
    # NB: plain import, no reload — reloading re-registers @tree.command.
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.discord_bot.admin as admin

    monkeypatch.setattr(config, "SIGNING_ACCOUNT", ISSUER)
    captured: dict = {}

    class _BurnResp:
        result = {"meta": {"TransactionResult": "tesSUCCESS"}}

    def fake_submit(tx, client, wallet):
        captured["tx"] = tx
        return _BurnResp()

    monkeypatch.setattr(admin, "submit_and_wait", fake_submit)
    assert _run(admin.burn_nft("00080000ABCD")) is True
    assert captured["tx"].account == ISSUER
