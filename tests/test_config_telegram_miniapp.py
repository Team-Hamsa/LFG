# tests/test_config_telegram_miniapp.py
# Config plumbing for the Telegram Mini App (#89, Part A). All three vars are
# optional; their empty defaults are the feature-off sentinels.
import importlib

import pytest


def test_lfg_core_config_rejects_nonpositive_max_age(monkeypatch):
    # Freshness is the only initData replay guard, so 0/negative would disable it.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    import lfg_core.config as cfg

    for bad in ("0", "-1"):
        monkeypatch.setenv("TELEGRAM_INITDATA_MAX_AGE", bad)
        with pytest.raises(ValueError):
            importlib.reload(cfg)
    # Restore a valid module state for tests sharing the import.
    monkeypatch.delenv("TELEGRAM_INITDATA_MAX_AGE", raising=False)
    importlib.reload(cfg)


def test_telegram_surface_config_rejects_non_https_miniapp_url(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.telegram_bot.config as cfg

    monkeypatch.setenv("TELEGRAM_MINI_APP_URL", "http://x")
    with pytest.raises(ValueError):
        importlib.reload(cfg)
    # https:// is fine.
    monkeypatch.setenv("TELEGRAM_MINI_APP_URL", "https://x")
    importlib.reload(cfg)
    assert cfg.TELEGRAM_MINI_APP_URL == "https://x"
    # Unset is fine (feature dormant). Set "" explicitly (not delenv): a
    # populated .env would otherwise repopulate it via load_dotenv() on reload.
    monkeypatch.setenv("TELEGRAM_MINI_APP_URL", "")
    importlib.reload(cfg)
    assert cfg.TELEGRAM_MINI_APP_URL == ""


def test_lfg_core_config_feature_off_when_token_empty(monkeypatch):
    # An empty TELEGRAM_BOT_TOKEN is the feature-off sentinel (→ 503). We set it
    # empty explicitly because a populated .env would otherwise repopulate it via
    # load_dotenv() on reload.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.delenv("TELEGRAM_INITDATA_MAX_AGE", raising=False)
    import lfg_core.config as cfg

    importlib.reload(cfg)
    assert cfg.TELEGRAM_BOT_TOKEN == ""  # unset/empty → /api/telegram/auth returns 503
    assert cfg.TELEGRAM_INITDATA_MAX_AGE == 3600  # max-age default


def test_lfg_core_config_reads_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_INITDATA_MAX_AGE", "900")
    import lfg_core.config as cfg

    importlib.reload(cfg)
    assert cfg.TELEGRAM_BOT_TOKEN == "123:abc"
    assert cfg.TELEGRAM_INITDATA_MAX_AGE == 900
    # Restore module-level defaults for later tests sharing the import.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_INITDATA_MAX_AGE", raising=False)
    importlib.reload(cfg)


def test_telegram_surface_config_miniapp_url_default(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)
    # "" not delenv: load_dotenv() on reload would repopulate a deleted var
    # from a populated .env ("" is the same feature-off sentinel as unset).
    monkeypatch.setenv("TELEGRAM_MINI_APP_URL", "")
    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    assert cfg.TELEGRAM_MINI_APP_URL == ""  # unset → launch button omitted


def test_telegram_surface_config_miniapp_url_set(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
        "TELEGRAM_MINI_APP_URL": "https://lfg.example.com",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    assert cfg.TELEGRAM_MINI_APP_URL == "https://lfg.example.com"
