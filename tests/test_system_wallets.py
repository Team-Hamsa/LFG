"""Project wallets stay excluded after a config repoint (#413, #414).

The bug both issues describe is the same one: archived rows keep the address
that signed them, so an exclusion resolved purely from config lapses the
moment the config var is repointed. These tests pin the durable half.
"""

from __future__ import annotations

import importlib

from lfg_core import system_wallets

RETIRED_DISTRIBUTOR = "rnqvoyrWAP95mqssc9yBu6oBeayQUbrteu"
CURRENT_DISTRIBUTOR = "rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ"


def test_both_distributors_are_durable():
    assert RETIRED_DISTRIBUTOR in system_wallets.DURABLE_SYSTEM_ACCOUNTS
    assert CURRENT_DISTRIBUTOR in system_wallets.DURABLE_SYSTEM_ACCOUNTS


def test_with_durable_preserves_the_caller_set():
    out = system_wallets.with_durable(frozenset({"rSomeConfiguredWallet"}))
    assert "rSomeConfiguredWallet" in out
    assert system_wallets.DURABLE_SYSTEM_ACCOUNTS <= out


def test_leaderboard_excludes_a_repointed_distributor(monkeypatch):
    """#414: 60,732 historical payouts must not become user activity when
    BRIX_DISTRIBUTOR_ADDRESS moves elsewhere."""
    from lfg_service import app

    monkeypatch.setattr(app.config, "BRIX_DISTRIBUTOR_ADDRESS", "rSomeBrandNewWallet")
    excluded = app._lb_system_accounts()
    assert RETIRED_DISTRIBUTOR in excluded
    assert "rSomeBrandNewWallet" in excluded


def test_accrual_never_pays_a_retired_distributor(monkeypatch):
    """A wallet dropped from a config slot is still ours — it must not earn."""
    accrue_brix = importlib.import_module("scripts.accrue_brix")

    monkeypatch.setattr(accrue_brix.config, "BRIX_DISTRIBUTOR_ADDRESS", "rSomeBrandNewWallet")
    assert RETIRED_DISTRIBUTOR in accrue_brix.system_accounts()


def test_sourcetag_unique_wallets_excludes_both_distributors(monkeypatch):
    """#413: every claim payout has Account = the distributor and carries our
    SourceTag, so an unexcluded distributor inflates unique_wallets nightly."""
    sourcetag_metrics = importlib.import_module("scripts.sourcetag_metrics")

    monkeypatch.setattr(sourcetag_metrics.config, "BRIX_DISTRIBUTOR_ADDRESS", CURRENT_DISTRIBUTOR)
    excluded = sourcetag_metrics.excluded_wallets()
    assert CURRENT_DISTRIBUTOR in excluded
    assert RETIRED_DISTRIBUTOR in excluded

    # …and still, once the configured address moves on again.
    monkeypatch.setattr(sourcetag_metrics.config, "BRIX_DISTRIBUTOR_ADDRESS", "rNextOne")
    later = sourcetag_metrics.excluded_wallets()
    assert {CURRENT_DISTRIBUTOR, RETIRED_DISTRIBUTOR, "rNextOne"} <= set(later)
