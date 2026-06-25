def test_bot_main_is_importable(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "tok",
        "ADMIN_LOG_CHANNEL_ID": "123",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "stk",
        "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rIssuer",
        "TOKEN_CURRENCY_HEX": "ABC",
    }.items():
        monkeypatch.setenv(k, v)
    from surfaces.discord_bot.bot import main

    assert callable(main)
