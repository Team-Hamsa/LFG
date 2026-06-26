# surfaces/telegram_bot/bot.py
# python-telegram-bot v21+ application lifecycle for the Telegram surface. One
# shared LFGServiceClient drives every handler. The firehose consumer runs as a
# cancellable task started in post_init and stopped BEFORE svc.close() in
# post_shutdown, so the generator's aclose() releases the WebSocket on a live
# aiohttp session (mirrors the Discord adapter's cleanup ordering).
import asyncio
import logging

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from surfaces._client import LFGServiceClient
from surfaces.telegram_bot import config
from surfaces.telegram_bot.events import run_event_loop

svc = LFGServiceClient(config.LFG_SERVICE_URL, config.SERVICE_TOKEN_TELEGRAM, "telegram")

_events_task: asyncio.Task[None] | None = None


async def _post_init(application: Application) -> None:  # type: ignore[type-arg]
    global _events_task
    await svc.__aenter__()

    # Mini App (#89): pin a persistent BotFather chat menu button pointing at the
    # public HTTPS URL — only when configured, so the feature stays dormant until
    # the ops step provisions hosting. Setting it programmatically here survives
    # redeploys without a manual BotFather step.
    if config.TELEGRAM_MINI_APP_URL:
        from telegram import MenuButtonWebApp, WebAppInfo  # noqa: PLC0415

        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Open App", web_app=WebAppInfo(url=config.TELEGRAM_MINI_APP_URL)
                )
            )
        except Exception as e:
            logging.warning(f"set_chat_menu_button failed: {e}")

    async def _announce(message: str, image_url: str | None) -> None:
        if image_url:
            await application.bot.send_photo(
                chat_id=config.TELEGRAM_ANNOUNCE_CHAT_ID, photo=image_url, caption=message
            )
        else:
            await application.bot.send_message(
                chat_id=config.TELEGRAM_ANNOUNCE_CHAT_ID, text=message
            )

    async def _dm(uid: str, message: str, image_url: str | None) -> None:
        try:
            if image_url:
                await application.bot.send_photo(chat_id=int(uid), photo=image_url, caption=message)
            else:
                await application.bot.send_message(chat_id=int(uid), text=message)
        except Exception as e:
            logging.warning(f"DM to {uid} failed: {e}")

    _events_task = asyncio.create_task(run_event_loop(svc, _announce, _dm))


async def _post_shutdown(application: Application) -> None:  # type: ignore[type-arg]
    global _events_task
    if _events_task is not None:
        _events_task.cancel()
        await asyncio.gather(_events_task, return_exceptions=True)
        _events_task = None
    try:
        await svc.close()
    except Exception as e:
        logging.error(f"Error closing service client: {e}")


def build_application() -> Application:  # type: ignore[type-arg]
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    from surfaces.telegram_bot import commands as cmds  # noqa: PLC0415

    application.add_handler(CommandHandler("mint", cmds.mint))
    application.add_handler(CommandHandler("register", cmds.register))
    application.add_handler(CommandHandler("link", cmds.link))
    application.add_handler(CommandHandler("swap", cmds.swap))
    application.add_handler(CommandHandler(["start", "help"], cmds.start))
    # Inline-keyboard buttons from /start reuse the same handlers (#87).
    application.add_handler(CallbackQueryHandler(cmds.mint_button, pattern="^mint$"))
    application.add_handler(CallbackQueryHandler(cmds.register_button, pattern="^register$"))
    # Trait-swapper inline keyboards (#88). Register the specific swap_* patterns
    # so the multi-step conversation routes to the right handler.
    application.add_handler(CallbackQueryHandler(cmds.swap_button, pattern="^swap$"))
    application.add_handler(CallbackQueryHandler(cmds.swap_pick_button, pattern="^swap_pick_"))
    application.add_handler(CallbackQueryHandler(cmds.swap_trait_button, pattern="^swap_trait_"))
    application.add_handler(
        CallbackQueryHandler(cmds.swap_confirm_button, pattern="^swap_confirm$")
    )
    application.add_handler(CallbackQueryHandler(cmds.swap_cancel_button, pattern="^swap_cancel$"))
    application.add_handler(CallbackQueryHandler(cmds.swap_page_button, pattern="^swap_page_"))
    # Dimmed wrong-gender grid buttons fire swap_noop — answer silently so the
    # loading spinner doesn't hang.
    application.add_handler(CallbackQueryHandler(cmds.swap_noop_button, pattern="^swap_noop$"))
    return application


def main() -> None:
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    # Prefer the run_telegram.py shim. If this module is run directly
    # (`python -m surfaces.telegram_bot.bot`), it executes as __main__ and would
    # be imported a SECOND time under its canonical name when commands.py does
    # `from surfaces.telegram_bot.bot import svc` — two LFGServiceClient
    # instances, only one of which is entered (broke /register, /mint). Re-enter
    # through the canonical module so the same instance is used everywhere,
    # disarming that footgun even when the shim is bypassed.
    from surfaces.telegram_bot.bot import main as _canonical_main

    _canonical_main()
