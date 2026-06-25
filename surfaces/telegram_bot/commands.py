# surfaces/telegram_bot/commands.py
# Telegram command handlers: /mint (interactive), /register <wallet>, /start.
# Mirrors surfaces.discord_bot.commands. _register_impl takes an injectable _svc
# so tests can drive it without the real shared client.
#
# IMPORT NOTE: svc and handle_mint are imported lazily inside each function so
# that this module is importable before surfaces.telegram_bot.bot exists (bot.py
# is Task B6 and may not be present yet). Tests inject _svc and never touch the
# real bot.svc.
from typing import Any

from surfaces._client.errors import ServiceError


async def mint(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # type: ignore[import-not-found]  # noqa: PLC0415
    from surfaces.telegram_bot.mint_view import handle_mint  # noqa: PLC0415

    await handle_mint(svc, update, context)


async def _register_impl(update: Any, context: Any, *, _svc: Any = None) -> None:
    """Register the caller's wallet via the shared service.

    Extracted so tests can inject a fake _svc without bot.py existing.
    """
    if _svc is None:
        from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py is Task B6

        client: Any = svc
    else:
        client = _svc

    user = update.effective_user
    uid = str(user.id)
    name = user.username or getattr(user, "full_name", "") or ""
    args = getattr(context, "args", None) or []
    if not args:
        await update.message.reply_text("Usage: /register <wallet>")
        return
    wallet = args[0]
    try:
        await client.register(uid, name, wallet)
    except ServiceError as e:
        await update.message.reply_text(e.message or "There was an error registering your wallet.")
        return
    await update.message.reply_text("Your wallet has been registered!")


async def register(update: Any, context: Any) -> None:
    await _register_impl(update, context)


async def start(update: Any, context: Any) -> None:
    await update.message.reply_text(
        "Welcome to LFG! Register your wallet with /register <wallet>, then /mint to mint an NFT."
    )
