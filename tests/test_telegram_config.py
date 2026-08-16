import importlib


def test_config_reads_env(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "tg-tok",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "12345",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    assert cfg.TELEGRAM_BOT_TOKEN == "tg-tok"
    assert cfg.SERVICE_TOKEN_TELEGRAM == "s"
    assert cfg.TELEGRAM_ANNOUNCE_CHAT_ID == 12345
