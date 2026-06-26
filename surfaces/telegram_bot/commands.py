# surfaces/telegram_bot/commands.py
# Telegram command handlers: /mint (interactive), /register (Xaman verified), /start.
# Mirrors surfaces.discord_bot.commands. Handlers import svc and view modules
# lazily so this module is importable before surfaces.telegram_bot.bot exists
# (bot.py is Task B6 and may not be present yet). Tests inject fakes directly
# into the view modules.
#
# IMPORT NOTE: svc and view imports are lazy inside each function so that this
# module is importable before surfaces.telegram_bot.bot exists.
from typing import Any


async def mint(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415
    from surfaces.telegram_bot.mint_view import handle_mint  # noqa: PLC0415

    await handle_mint(svc, update, context)


async def register(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.register_view import handle_register  # noqa: PLC0415

    await handle_register(svc, update, context)


async def start(update: Any, context: Any) -> None:
    await update.message.reply_text(
        "Welcome to LFG! Use /register to verify your wallet with Xaman, then /mint to mint an NFT."
    )
