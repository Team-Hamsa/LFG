# surfaces/telegram_bot/bot.py
# python-telegram-bot v21+ application lifecycle for the Telegram surface. One
# shared LFGServiceClient drives every handler. The firehose consumer runs as a
# cancellable task started in post_init and stopped BEFORE svc.close() in
# post_shutdown, so the generator's aclose() releases the WebSocket on a live
# aiohttp session (mirrors the Discord adapter's cleanup ordering).
import asyncio
import logging

from telegram.ext import Application, CommandHandler

from surfaces._client import LFGServiceClient
from surfaces.telegram_bot import config
from surfaces.telegram_bot.events import run_event_loop

svc = LFGServiceClient(config.LFG_SERVICE_URL, config.SERVICE_TOKEN_TELEGRAM, "telegram")

_events_task: asyncio.Task[None] | None = None


async def _post_init(application: Application) -> None:  # type: ignore[type-arg]
    global _events_task
    await svc.__aenter__()

    async def _announce(message: str) -> None:
        await application.bot.send_message(chat_id=config.TELEGRAM_ANNOUNCE_CHAT_ID, text=message)

    async def _dm(uid: str, message: str) -> None:
        try:
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
    application.add_handler(CommandHandler(["start", "help"], cmds.start))
    return application


def main() -> None:
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
