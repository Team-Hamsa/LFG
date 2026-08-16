# surfaces/telegram_bot/link_view.py
# Cross-surface /link for Telegram (#90): link_start -> QR photo ->
# wait_for_link -> confirm "Linked to your account" listing the OTHER surfaces
# the proven wallet is on. Mirrors register_view; account-aware. This package
# must NEVER import discord — cross-surface text lives in surfaces._shared.
import logging
from typing import Any

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.account_result import linked_summary
from surfaces._shared.mint_result import friendly_error
from surfaces._shared.signin_result import signin_outcome
from surfaces.telegram_bot import render


async def handle_link(svc: LFGServiceClient, update: Any, context: Any) -> None:
    bot = context.bot
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or getattr(user, "full_name", "") or ""

    try:
        session = await svc.link_start(user_id, username=username)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    uuid = session["uuid"]
    signin_link = session.get("signin_link", "")

    try:
        qr_png = await svc.qr_png(signin_link)
    except ServiceError as e:
        logging.error(f"link QR render failed: {e}")
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return
    await bot.send_photo(
        chat_id,
        photo=render.photo_input(qr_png, "signin_qr.png"),
        caption=render.signin_caption(signin_link),
    )

    try:
        final = await svc.wait_for_link(user_id, uuid)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    if final.get("state") == "signed":
        account = final.get("account") or {"wallet": final.get("wallet", ""), "identities": []}
        summary = linked_summary(account, current_platform="telegram", current_user_id=user_id)
        await bot.send_message(chat_id, render.linked_caption(summary))
        return
    await bot.send_message(chat_id, signin_outcome(str(final.get("state") or "")))
