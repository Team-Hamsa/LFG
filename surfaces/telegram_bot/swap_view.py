# surfaces/telegram_bot/swap_view.py
# Chat-style trait swapper for Telegram (#88), mirroring the webapp Dressing
# Room. A multi-step inline-keyboard conversation whose state lives in
# context.user_data["swap_session"] (this surface has no persistent View object
# like Discord). ALL XRPL/CDN/fee work happens in lfg_service (which stamps the
# Make Waves SourceTag); this module only orchestrates SDK calls and renders
# Telegram keyboards/photos. Each handler is standalone — (svc, update, context)
# — so tests drive it with fakes. MUST NOT import `discord`.
#
# Flow:
#   /swap or 🔄 button -> handle_swap: load roster, render NFT grid.
#   swap_pick_<id>     -> handle_swap_pick: 1st pick locks gender + re-filters
#                         the grid; 2nd pick shows the trait picker.
#   swap_trait_<Name>  -> handle_swap_trait: toggle a trait in the selected set.
#   swap_confirm       -> handle_swap_confirm: guard (>=1 trait), start_swap,
#                         drive payment QR + terminal results (mirrors mint_view).
#   swap_cancel        -> handle_swap_cancel: clear state.
#   swap_page_<n>      -> handle_swap_page: paginate the grid.
from typing import Any

from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import friendly_error
from surfaces.telegram_bot import render, swap_render

# Terminal swap states (from lfg_core.swap_flow / SDK SWAP_TERMINAL) that mean
# success — every result is ready to claim / already applied.
SWAP_OK_STATES: frozenset[str] = frozenset({"offers_ready", "done"})

# Human-readable messages for known bad terminal states.
BAD_STATE_MESSAGES: dict[str, str] = {
    "payment_timeout": "No swap fee payment was received in time. Your NFTs are untouched.",
    "failed": "The swap could not be completed. Your NFTs are untouched unless noted.",
}


def _key(context: Any) -> dict[str, Any]:
    """The per-user swap conversation state (or None if absent)."""
    return context.user_data.get("swap_session")


def _find(roster: list[dict[str, Any]], nft_id: str) -> dict[str, Any] | None:
    for nft in roster:
        if nft.get("nft_id") == nft_id:
            return nft
    return None


async def handle_swap(svc: Any, update: Any, context: Any) -> None:
    """Entry point (/swap command or 🔄 Swap Traits button). Load the roster
    and render the first-pick grid."""
    user_id = str(update.effective_user.id)

    try:
        data = await svc.nfts(user_id)
    except ServiceError as e:
        await _reply(update, context, render.error_caption(friendly_error(e)))
        return

    roster = data.get("nfts", [])
    if len(roster) < 2:
        await _reply(
            update,
            context,
            "You need at least two avatars to swap traits. Mint a couple more with /mint!",
        )
        return

    context.user_data["swap_session"] = {
        "roster": roster,
        "swappable_traits": data.get("swappable_traits", []),
        "swap_fee": data.get("swap_fee"),
        "nft1_id": None,
        "nft2_id": None,
        "traits": {},  # trait_name -> bool (selected)
        "page": 0,
    }
    await _reply(
        update,
        context,
        "🔄 Trait Swap — pick your first avatar.",
        reply_markup=swap_render.nft_grid_keyboard(roster, page=0),
    )


async def handle_swap_pick(svc: Any, update: Any, context: Any) -> None:
    """A grid pick. The first pick stores nft1 and LOCKS the body type (the
    grid re-renders with only matching avatars pickable). The second pick stores
    nft2 and shows the trait picker."""
    session = _key(context)
    query = update.callback_query
    if not session:
        await query.answer("This swap expired — send /swap to start over.")
        return

    nft_id = query.data[len("swap_pick_") :]
    roster = session["roster"]
    picked = _find(roster, nft_id)
    if picked is None:
        await query.answer("That avatar is no longer available.")
        return

    if session["nft1_id"] is None:
        # First pick — lock gender, re-render the grid filtered to that body type.
        session["nft1_id"] = nft_id
        gender = picked.get("gender")
        await query.answer()  # dismiss the loading spinner on this happy path
        await query.edit_message_text(
            "Now pick a matching body type to swap with.",
            reply_markup=swap_render.nft_grid_keyboard(roster, gender=gender, page=0),
        )
        return

    # Second pick — enforce the gender lock and that it's a different NFT.
    if nft_id == session["nft1_id"]:
        await query.answer("Pick a different avatar for the second slot.")
        return
    nft1 = _find(roster, session["nft1_id"])
    if nft1 is not None and picked.get("gender") != nft1.get("gender"):
        await query.answer("That avatar's body type doesn't match — pick a matching one.")
        return

    session["nft2_id"] = nft_id
    await query.answer()  # dismiss the loading spinner on this happy path
    await _show_trait_picker(update, context)


async def _show_trait_picker(update: Any, context: Any) -> None:
    session = _key(context)
    roster = session["roster"]
    nft1 = _find(roster, session["nft1_id"])
    nft2 = _find(roster, session["nft2_id"])
    if nft1 is None or nft2 is None:
        await update.callback_query.answer("This swap expired — send /swap to start over.")
        return
    selected = {t for t, on in session["traits"].items() if on}
    swappable = session.get("swappable_traits") or swap_render.DEFAULT_SWAPPABLE_TRAITS
    await update.callback_query.edit_message_text(
        swap_render.trait_picker_text(nft1, nft2, session.get("swap_fee")),
        reply_markup=swap_render.trait_picker_keyboard(nft1, nft2, swappable, selected),
    )


async def handle_swap_trait(svc: Any, update: Any, context: Any) -> None:
    """Toggle one trait in the selected set and re-render the picker."""
    session = _key(context)
    query = update.callback_query
    if not session or session.get("nft2_id") is None:
        await query.answer("This swap expired — send /swap to start over.")
        return
    trait = query.data[len("swap_trait_") :]
    session["traits"][trait] = not session["traits"].get(trait, False)
    await query.answer()
    await _show_trait_picker(update, context)


async def handle_swap_confirm(svc: Any, update: Any, context: Any) -> None:
    """Validate (>=1 trait), kick off the swap, then drive the terminal flow
    like mint_view: optional fee QR -> wait -> per-result claim QR / message.
    A real swap burns/modifies NFTs and charges a fee, so the >=1-trait guard
    here is the last line of defense against an empty/accidental swap."""
    session = _key(context)
    query = update.callback_query
    if not session or session.get("nft2_id") is None:
        await query.answer("This swap expired — send /swap to start over.")
        return

    traits = sorted(t for t, on in session["traits"].items() if on)
    if not traits:
        await query.answer("Select at least one trait to swap.")
        return

    await query.answer()
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    nft1_id, nft2_id = session["nft1_id"], session["nft2_id"]
    bot = context.bot

    # Consume the conversation state now: the swap is committed and the keyboard
    # must not be re-fired. Everything below works off locals.
    context.user_data.pop("swap_session", None)
    await query.edit_message_text("🔄 Starting the swap…")

    # 1. start the session (service composes, detects the fee path, may need an
    #    upfront payment for in-place modifies).
    try:
        sess = await svc.start_swap(user_id, nft1_id, nft2_id, traits)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    session_id = sess["id"]

    # 2. fee QR (only when an upfront modify fee is due — the session enters
    #    awaiting_payment with a payment_link).
    payment_link = sess.get("payment_link")
    if sess.get("state") == "awaiting_payment" and payment_link:
        try:
            qr_png = await svc.qr_png(payment_link)
        except ServiceError as e:
            await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
            return
        await bot.send_photo(
            chat_id,
            photo=render.photo_input(qr_png, "swap_fee_qr.png"),
            caption=swap_render.swap_payment_caption(
                str(sess.get("fee_amount") or ""), str(sess.get("pay_with") or "")
            ),
        )

    # 3. wait for a terminal state.
    try:
        final = await svc.wait_for_swap(user_id, session_id)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    state = str(final.get("state") or "")
    if state not in SWAP_OK_STATES:
        reason = final.get("error") or BAD_STATE_MESSAGES.get(
            state, "The swap did not complete. Please try again."
        )
        await bot.send_message(chat_id, render.error_caption(reason))
        return

    await _send_results(svc, bot, chat_id, final.get("results", []))


async def _send_results(svc: Any, bot: Any, chat_id: int, results: list[dict[str, Any]]) -> None:
    """Per result: modified NFTs need no action; reminted ones get a claim QR
    (hosted accept_qr_url preferred, else render the accept deeplink)."""
    for result in results:
        caption = swap_render.swap_result_caption(result)
        if result.get("modified"):
            await bot.send_message(chat_id, caption)
            continue

        hosted_qr = result.get("accept_qr_url")
        if hosted_qr:
            await bot.send_photo(chat_id, photo=hosted_qr, caption=caption)
            continue

        accept_link = result.get("accept_deeplink", "")
        try:
            qr_png = await svc.qr_png(accept_link)
        except ServiceError:
            # Swap succeeded; only the QR render failed. Surface the link.
            await bot.send_message(chat_id, f"{caption}\nOpen in Xaman: {accept_link}")
            continue
        await bot.send_photo(
            chat_id, photo=render.photo_input(qr_png, "swap_offer_qr.png"), caption=caption
        )


async def handle_swap_cancel(svc: Any, update: Any, context: Any) -> None:
    context.user_data.pop("swap_session", None)
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Swap cancelled.")


async def handle_swap_page(svc: Any, update: Any, context: Any) -> None:
    """Paginate the (first-pick) grid in place."""
    session = _key(context)
    query = update.callback_query
    if not session:
        await query.answer("This swap expired — send /swap to start over.")
        return
    try:
        page = int(query.data[len("swap_page_") :])
    except ValueError:
        await query.answer()
        return
    session["page"] = page
    await query.answer()
    # Honour a first-pick gender lock if one is set.
    gender = None
    if session.get("nft1_id"):
        nft1 = _find(session["roster"], session["nft1_id"])
        gender = nft1.get("gender") if nft1 else None
    await query.edit_message_text(
        "Now pick a matching body type to swap with."
        if gender
        else "🔄 Trait Swap — pick your first avatar.",
        reply_markup=swap_render.nft_grid_keyboard(session["roster"], gender=gender, page=page),
    )


async def _reply(update: Any, context: Any, text: str, *, reply_markup: Any = None) -> None:
    """Reply to either a command (update.message) or a callback-entry (the
    🔄 button on /start, where update.message is None)."""
    if update.message is not None:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=reply_markup)
