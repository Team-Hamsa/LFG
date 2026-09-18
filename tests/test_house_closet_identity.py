"""The house wallet (#548) is validated before anything signs for it."""

import pytest
from xrpl.wallet import Wallet

from lfg_core import config
from lfg_core import house_closet as hc


def test_house_address_requires_the_env(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "")
    with pytest.raises(hc.HouseConfigError, match="CLOSET_HOUSE_WALLET is not set"):
        hc.house_address()


def test_house_address_rejects_a_malformed_address(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "not-an-address")
    with pytest.raises(hc.HouseConfigError, match="not a classic address"):
        hc.house_address()


def test_house_address_rejects_system_wallets(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", config.SIGNING_ACCOUNT)
    with pytest.raises(hc.HouseConfigError, match="system wallet"):
        hc.house_address()
    distributor = Wallet.create().classic_address
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_ADDRESS", distributor)
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", distributor)
    with pytest.raises(hc.HouseConfigError, match="system wallet"):
        hc.house_address()


def test_house_wallet_needs_a_seed_that_derives_the_address(monkeypatch):
    house = Wallet.create()
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", house.classic_address)
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", "")
    with pytest.raises(hc.HouseConfigError, match="CLOSET_HOUSE_SEED is not set"):
        hc.house_wallet()
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", Wallet.create().seed)
    with pytest.raises(hc.HouseConfigError, match="different account"):
        hc.house_wallet()
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", house.seed)
    assert hc.house_wallet().classic_address == house.classic_address


def test_house_wallet_never_echoes_a_bad_seed(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", Wallet.create().classic_address)
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", "sBADSEEDxyz")
    with pytest.raises(hc.HouseConfigError) as err:
        hc.house_wallet()
    assert "sBADSEEDxyz" not in str(err.value)
    assert err.value.__cause__ is None
