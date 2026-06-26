# tests/test_config_telegram_miniapp.py
# Config plumbing for the Telegram Mini App (#89, Part A). All three vars are
# optional; their empty defaults are the feature-off sentinels.
import importlib


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
    monkeypatch.delenv("TELEGRAM_MINI_APP_URL", raising=False)
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
