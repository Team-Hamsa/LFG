# Launch shim for the Telegram surface — mirrors main.py (Discord).
# Importing surfaces.telegram_bot.bot here (rather than `python -m
# surfaces.telegram_bot.bot`) ensures bot.py loads under its canonical module
# name exactly once. Running it as `-m` would execute it as __main__ AND import
# it again as surfaces.telegram_bot.bot (via commands.py), creating TWO
# LFGServiceClient instances — _post_init enters one while the command handlers
# use the other (whose aiohttp session is never opened → RuntimeError on
# /register, /mint).
from surfaces.telegram_bot.bot import main

if __name__ == "__main__":
    main()
