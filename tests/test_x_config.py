# tests/test_x_config.py
# Config plumbing for the X (Twitter) poster surface (#41, PR1-T1). All five
# vars are optional; X_ENABLED is a composite flag — true only when the master
# switch is set AND all four OAuth 1.0a credentials are non-empty (mirrors the
# ECONOMY_ENABLED/MARKET_ENABLED boolean-flag convention, config.py:159/220).
#
# Env-guard preamble (copy from tests/test_seasons.py): importing lfg_core.config
# freezes its constants at import time; set the same defaults test_smoke.py uses
# so collection order can't strand them.
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

import importlib  # noqa: E402

import pytest  # noqa: E402

from lfg_core import config  # noqa: E402

_CREDS = ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")


@pytest.fixture(autouse=True)
def _reset_config():
    # Every test here monkeypatches X_* env vars and reloads the shared
    # lfg_core.config module in-process; restore real config state afterward
    # (monkeypatch itself only undoes the env vars, not the already-reloaded
    # module) so later test modules see the module's normal default posture.
    yield
    importlib.reload(config)


def _set_all_creds(monkeypatch, value="tok"):
    for name in _CREDS:
        monkeypatch.setenv(name, value)


def test_x_enabled_false_when_flag_set_but_a_cred_missing(monkeypatch):
    monkeypatch.setenv("X_ENABLED", "1")
    _set_all_creds(monkeypatch)
    monkeypatch.delenv("X_ACCESS_SECRET", raising=False)
    importlib.reload(config)
    assert config.X_ENABLED is False


def test_x_enabled_true_when_flag_and_all_creds_set(monkeypatch):
    monkeypatch.setenv("X_ENABLED", "1")
    _set_all_creds(monkeypatch)
    importlib.reload(config)
    assert config.X_ENABLED is True
    assert config.X_CONSUMER_KEY == "tok"
    assert config.X_CONSUMER_SECRET == "tok"
    assert config.X_ACCESS_TOKEN == "tok"
    assert config.X_ACCESS_SECRET == "tok"


def test_x_enabled_false_when_creds_set_but_flag_unset(monkeypatch):
    monkeypatch.delenv("X_ENABLED", raising=False)
    _set_all_creds(monkeypatch)
    importlib.reload(config)
    assert config.X_ENABLED is False


def test_x_monthly_post_budget_default_and_override(monkeypatch):
    monkeypatch.delenv("X_MONTHLY_POST_BUDGET", raising=False)
    importlib.reload(config)
    assert config.X_MONTHLY_POST_BUDGET == 100

    monkeypatch.setenv("X_MONTHLY_POST_BUDGET", "250")
    importlib.reload(config)
    assert config.X_MONTHLY_POST_BUDGET == 250


def test_x_state_db_path_default(monkeypatch):
    monkeypatch.delenv("X_STATE_DB_PATH", raising=False)
    importlib.reload(config)
    assert config.X_STATE_DB_PATH == "x_state.db"
