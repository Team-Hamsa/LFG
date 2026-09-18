"""The house wallet (#548) is a project wallet: its BRIX proceeds must not top
the leaderboards, and its setup transactions must not count as a user."""

import importlib

from lfg_core import config
from lfg_service import app as server

stm = importlib.import_module("scripts.sourcetag_metrics")
HOUSE = "rHouseWa11etXXXXXXXXXXXXXXXXXXXXXX"


def test_leaderboards_exclude_the_house(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", HOUSE)
    assert HOUSE in server._lb_system_accounts()


def test_sourcetag_metrics_exclude_the_house(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", HOUSE)
    assert HOUSE in stm.excluded_wallets()


def test_an_unset_house_adds_nothing(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "")
    assert "" not in server._lb_system_accounts()
    assert "" not in stm.excluded_wallets()
