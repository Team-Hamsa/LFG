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


async def link(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.link_view import handle_link  # noqa: PLC0415

    await handle_link(svc, update, context)


async def swap(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.swap_view import handle_swap  # noqa: PLC0415

    await handle_swap(svc, update, context)


async def start(update: Any, context: Any) -> None:
    # Inline-keyboard menu (#87, #88): the buttons fire the mint/register/swap
    # callbacks below, which reuse the SAME handlers as the slash commands.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎨 Mint NFT", callback_data="mint")],
            [InlineKeyboardButton("🔄 Swap Traits", callback_data="swap")],
            [InlineKeyboardButton("🔐 Register Wallet", callback_data="register")],
        ]
    )
    await update.message.reply_text(
        "Welcome to LFG! Tap a button below — register your wallet with Xaman, then mint an NFT.",
        reply_markup=keyboard,
    )


async def mint_button(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.mint_view import handle_mint  # noqa: PLC0415

    await update.callback_query.answer()
    await handle_mint(svc, update, context)


async def register_button(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.register_view import handle_register  # noqa: PLC0415

    await update.callback_query.answer()
    await handle_register(svc, update, context)


# ---- trait-swapper callbacks (#88) ----------------------------------------
# Each delegates to a swap_view handler that owns answering the query (toasts
# for guards) — the swap flow needs fine-grained control over the toast text,
# so unlike mint/register these do NOT pre-answer here.


async def swap_button(update: Any, context: Any) -> None:
    # 🔄 Swap Traits on the /start menu — same handler as the /swap command.
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.swap_view import handle_swap  # noqa: PLC0415

    await update.callback_query.answer()
    await handle_swap(svc, update, context)


async def swap_pick_button(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.swap_view import handle_swap_pick  # noqa: PLC0415

    await handle_swap_pick(svc, update, context)


async def swap_trait_button(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.swap_view import handle_swap_trait  # noqa: PLC0415

    await handle_swap_trait(svc, update, context)


async def swap_confirm_button(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.swap_view import handle_swap_confirm  # noqa: PLC0415

    await handle_swap_confirm(svc, update, context)


async def swap_cancel_button(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.swap_view import handle_swap_cancel  # noqa: PLC0415

    await handle_swap_cancel(svc, update, context)


async def swap_page_button(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.swap_view import handle_swap_page  # noqa: PLC0415

    await handle_swap_page(svc, update, context)


async def swap_noop_button(update: Any, context: Any) -> None:
    # Dimmed wrong-gender grid buttons emit callback_data="swap_noop". With no
    # handler, tapping one leaves the loading spinner stuck ~10s. Silently
    # dismiss it — answer() with no text shows no toast.
    await update.callback_query.answer()
