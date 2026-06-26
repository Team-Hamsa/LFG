import importlib

REQUIRED = {
    "DISCORD_BOT_TOKEN": "tok",
    "ADMIN_LOG_CHANNEL_ID": "123",
    "LFG_SERVICE_URL": "http://svc",
    "SERVICE_TOKEN_DISCORD": "stk",
    "XUMM_API_KEY": "k",
    "XUMM_API_SECRET": "s",
    "TOKEN_ISSUER_ADDRESS": "rIssuer",
    "TOKEN_CURRENCY_HEX": "ABC",
}


def test_config_exposes_spine_vars(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    import surfaces.discord_bot.config as cfg

    cfg = importlib.reload(cfg)
    assert cfg.LFG_SERVICE_URL == "http://svc"
    assert cfg.SERVICE_TOKEN_DISCORD == "stk"
    assert cfg.VIEW_TIMEOUT == 600


def test_config_fails_fast_without_service_url(monkeypatch):
    # Neutralize load_dotenv so the reload below can't repopulate LFG_SERVICE_URL
    # from a real local .env (a deployed box has it set there). This test must
    # verify the _require() fast-fail purely from os.environ.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for k, v in REQUIRED.items():
        if k != "LFG_SERVICE_URL":
            monkeypatch.setenv(k, v)
    monkeypatch.delenv("LFG_SERVICE_URL", raising=False)
    import surfaces.discord_bot.config as cfg

    try:
        importlib.reload(cfg)
        raised = False
    except ValueError:
        raised = True
    assert raised
