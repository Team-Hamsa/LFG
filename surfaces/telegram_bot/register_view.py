# surfaces/telegram_bot/register_view.py
# Xaman-verified /register for Telegram: signin_start -> QR photo ->
# wait_for_signin -> report the verified wallet (the service stores it on
# 'signed'). The bot never sends an address itself — ownership is proven in
# Xaman. Standalone coroutine so tests drive it with fakes.
import logging
from typing import Any

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import friendly_error
from surfaces._shared.signin_result import signin_outcome
from surfaces.telegram_bot import render


async def handle_register(svc: LFGServiceClient, update: Any, context: Any) -> None:
    bot = context.bot
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)

    try:
        session = await svc.signin_start(user_id)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    uuid = session["uuid"]
    signin_link = session.get("signin_link", "")

    try:
        qr_png = await svc.qr_png(signin_link)
    except ServiceError as e:
        logging.error(f"signin QR render failed: {e}")
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return
    await bot.send_photo(
        chat_id,
        photo=render.photo_input(qr_png, "signin_qr.png"),
        caption=render.signin_caption(signin_link),
    )

    try:
        final = await svc.wait_for_signin(user_id, uuid)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    if final.get("state") == "signed":
        wallet = final.get("wallet", "")
        await bot.send_message(chat_id, f"✅ Wallet verified and registered: {wallet}")
        return
    await bot.send_message(chat_id, signin_outcome(str(final.get("state") or "")))
