# surfaces/telegram_bot/mint_view.py
# Inverted mint handler for Telegram: start_mint -> payment QR -> wait_for_mint
# -> offer-accept QR. ALL XRPL/CDN work happens in lfg_service (which stamps the
# Make Waves SourceTag); this module only orchestrates SDK calls and sends
# Telegram photos/messages. handle_mint(svc, update, context) is standalone so
# tests can drive it with fakes.
# The mint-result helpers (friendly_error / MINT_OK_STATES / BAD_STATE_MESSAGES)
# come from the surface-agnostic surfaces._shared.mint_result (Task BS) — shared
# with the Discord adapter, no `discord` import pulled in.
import logging
from typing import Any

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import BAD_STATE_MESSAGES, MINT_OK_STATES, friendly_error
from surfaces.telegram_bot import render


async def handle_mint(svc: LFGServiceClient, update: Any, context: Any) -> None:
    bot = context.bot
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or getattr(user, "full_name", "") or ""

    # 1. start the session (service detects payment path + builds the XUMM
    #    sign request; raises on no-wallet / already-in-progress)
    try:
        session = await svc.start_mint(user_id, username=username)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    session_id = session["id"]
    payment_link = session.get("payment_link", "")

    # 2. payment step. A newcomer free mint has no payment and no QR — just
    #    confirm the freebie. Otherwise render the payment QR locally from the
    #    deeplink (the service exposes no hosted payment-QR url, only the link).
    if session.get("free"):
        await bot.send_message(chat_id, render.free_mint_caption())
    else:
        try:
            qr_png = await svc.qr_png(payment_link)
        except ServiceError as e:
            logging.error(f"payment QR render failed: {e}")
            # Cancel the in-flight session so it doesn't hold open until timeout
            # and block a retry (CodeRabbit #209).
            try:
                await svc.cancel_mint(user_id, session_id)
            except ServiceError:
                logging.warning("mint cancel after QR-render failure failed", exc_info=True)
            await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
            return
        await bot.send_photo(
            chat_id,
            photo=render.photo_input(qr_png, "payment_qr.png"),
            caption=render.payment_caption(payment_link, push=session.get("payment_push")),
        )

    # 3. wait for a terminal state (SDK polls /api/mint/<id> + backs off)
    try:
        final = await svc.wait_for_mint(user_id, session_id)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    state = str(final.get("state") or "")
    if state not in MINT_OK_STATES:
        reason = BAD_STATE_MESSAGES.get(state, "Mint did not complete. Please try again.")
        await bot.send_message(chat_id, render.error_caption(reason))
        return

    # 3b. show the minter their artwork first (large), then the claim QR.
    # Animated mints carry a video_url (MP4) next to the PNG poster frame —
    # prefer it so the animation actually plays.
    artwork_url = final.get("video_url") or final.get("image_url")
    if artwork_url:
        await render.send_media(bot, chat_id, artwork_url, render.artwork_caption(final))

    # 4. offer-accept QR. Prefer the service-hosted accept_qr_url (no extra
    #    round-trip); otherwise render the accept deeplink ourselves.
    hosted_qr = final.get("accept_qr_url")
    if hosted_qr:
        await bot.send_photo(chat_id, photo=hosted_qr, caption=render.offer_caption(final))
        return

    accept_link = final.get("accept_deeplink", "")
    try:
        qr_png = await svc.qr_png(accept_link)
    except ServiceError:
        # Mint succeeded; only the QR render failed. Still surface the offer link.
        await bot.send_message(chat_id, render.offer_caption(final, with_qr=False))
        return
    await bot.send_photo(
        chat_id,
        photo=render.photo_input(qr_png, "offer_qr.png"),
        caption=render.offer_caption(final),
    )
