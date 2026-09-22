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
from surfaces.telegram_bot import delivered, render


async def _send_artwork(
    bot: Any, chat_id: Any, user_id: str, artwork_url: str, final: dict[str, Any]
) -> bool:
    """Show the minter their artwork. Returns whether it actually landed.

    Claiming the session (#591) means the firehose has already skipped its DM
    for this mint, and the bus does not replay a consumed event — so a failed
    send here is the minter's last chance at the image, and it must not take
    the claim QR down with it either. In a group chat the DM is a different
    chat and usually still works; in the bot's own DM the retry is the same
    media to the same chat, so it is skipped (the firehose DM it replaced
    would have failed for the same reason).
    """
    caption = render.artwork_caption(final)
    try:
        await render.send_media(bot, chat_id, artwork_url, caption)
        return True
    except Exception as e:
        logging.warning(f"artwork send to chat {chat_id} failed: {e}")
    if str(chat_id) == user_id:
        return False
    try:
        await render.send_media(bot, int(user_id), artwork_url, caption)
        return True
    except Exception as e:
        logging.warning(f"artwork DM to {user_id} failed: {e}")
        return False


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

    # This handler owns the minter's artwork delivery for this session from here
    # on, so the firehose consumer (events.py) skips its DM and the same image
    # doesn't land twice (#591). Claimed now, before any await that could let the
    # terminal event overtake us; handed back below if we never show the artwork.
    delivered.mark(session_id)
    artwork_shown = False
    try:
        # 2. payment QR (rendered locally from the deeplink — the service exposes no
        #    hosted payment-QR url, only the link).
        #    A sponsored session (#free-mint) skips payment entirely, so it carries
        #    payment_link=None. Rendering a QR from that raises TypeError out of the
        #    SDK — NOT ServiceError, so the handler below would not catch it, and the
        #    mint would run to completion server-side while this surface died before
        #    ever delivering the accept-offer QR. Admission is surface-agnostic, so
        #    this branch is reachable from /mint here just as it is in the Activity.
        if session.get("sponsored"):
            await bot.send_message(chat_id, render.sponsored_caption())
        else:
            try:
                qr_png = await svc.qr_png(payment_link)
            except ServiceError as e:
                logging.error(f"payment QR render failed: {e}")
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
            artwork_shown = await _send_artwork(bot, chat_id, user_id, artwork_url, final)

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
    finally:
        # Never showed it (error, bad terminal state, imageless mint, crash) —
        # the DM is the minter's only delivery left, so stop suppressing it.
        if not artwork_shown:
            delivered.release(session_id)
