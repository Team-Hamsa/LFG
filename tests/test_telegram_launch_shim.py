# Guards the Telegram launch contract: the bot MUST be started via the
# run_telegram.py shim (which imports surfaces.telegram_bot.bot canonically),
# NOT via `python -m surfaces.telegram_bot.bot`. Running bot.py as __main__ makes
# it load a SECOND time under its canonical name when commands.py imports `svc`,
# yielding two LFGServiceClient instances — _post_init enters one while the
# command handlers use the other (whose aiohttp session is never opened), so
# /register and /mint fail with "must be used as an async context manager".
import importlib


def test_shim_targets_canonical_bot_main(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)

    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.telegram_bot.bot as bot

    importlib.reload(bot)

    run_telegram = importlib.import_module("run_telegram")
    importlib.reload(run_telegram)

    # The shim's main IS the canonical bot.main — so launching the shim loads
    # bot under its real module name exactly once (no __main__ duplicate).
    assert run_telegram.main is bot.main
