# surfaces/discord_bot/mint_view.py
# The inverted mint handler: start_mint -> payment QR -> wait_for_mint ->
# offer-accept QR. ALL XRPL/CDN work happens in lfg_service (which stamps the
# Make Waves SourceTag); this module only orchestrates SDK calls and renders
# embeds. Written as a standalone coroutine handle_mint(svc, interaction) so the
# test can drive it with a fake svc + fake interaction (no View plumbing).
import logging

import discord

from surfaces._client import LFGServiceClient
from surfaces._client.errors import BadRequest, ServiceError
from surfaces.discord_bot import render

# Success end-states from lfg_core.mint_flow (offer_ready is the success state;
# done is the post-accept state). failed / payment_timeout are the bad ones.
MINT_OK_STATES = frozenset({"offer_ready", "done"})

_BAD_STATE_MESSAGES = {
    "payment_timeout": "Payment request timed out. Please try again.",
    "failed": "The mint failed. Please try again or contact an admin.",
}


def _friendly(err: ServiceError) -> str:
    code = (err.code or "").lower()
    message = (err.message or "").lower()
    if isinstance(err, BadRequest) and ("wallet" in code or "wallet" in message):
        return "Please register your wallet first using /register."
    if err.status == 409 or "in_progress" in code or "already" in message:
        return "You already have a mint in progress — finish or wait for it to time out."
    return err.message or "The mint service is unavailable. Please try again shortly."


async def handle_mint(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    username = str(interaction.user)

    # 1. start the session (service detects payment path + builds the XUMM
    #    sign request; raises on no-wallet / already-in-progress)
    try:
        session = await svc.start_mint(user_id, username=username)
    except ServiceError as e:
        await interaction.followup.send(embed=render.error_embed(_friendly(e)), ephemeral=True)
        return

    session_id = session["id"]
    payment_link = session.get("payment_link", "")

    # 2. payment QR (rendered locally from the deeplink — the service exposes no
    #    hosted payment-QR url, only the link)
    try:
        qr_png = await svc.qr_png(payment_link)
    except ServiceError as e:
        logging.error(f"payment QR render failed: {e}")
        await interaction.followup.send(embed=render.error_embed(_friendly(e)), ephemeral=True)
        return
    await interaction.followup.send(
        embed=render.payment_embed(payment_link),
        file=render.file_from_png(qr_png, "payment_qr.png"),
        ephemeral=True,
    )

    # 3. wait for a terminal state (SDK polls /api/mint/<id> + backs off)
    try:
        final = await svc.wait_for_mint(user_id, session_id)
    except ServiceError as e:
        await interaction.followup.send(embed=render.error_embed(_friendly(e)), ephemeral=True)
        return

    state = str(final.get("state") or "")
    if state not in MINT_OK_STATES:
        reason = _BAD_STATE_MESSAGES.get(state, "Mint did not complete. Please try again.")
        await interaction.followup.send(embed=render.error_embed(reason), ephemeral=True)
        return

    # 4. offer-accept QR. Prefer the service-hosted accept_qr_url (no extra
    #    round-trip); otherwise render the accept deeplink ourselves.
    hosted_qr = final.get("accept_qr_url")
    if hosted_qr:
        await interaction.followup.send(embed=render.offer_embed(final, hosted_qr), ephemeral=True)
        return

    accept_link = final.get("accept_deeplink", "")
    try:
        qr_png = await svc.qr_png(accept_link)
    except ServiceError:
        # The mint succeeded; only the QR render failed. Still surface the offer
        # with the deeplink so the user can claim it.
        await interaction.followup.send(embed=render.offer_embed(final, ""), ephemeral=True)
        return
    await interaction.followup.send(
        embed=render.offer_embed(final, "attachment://offer_qr.png"),
        file=render.file_from_png(qr_png, "offer_qr.png"),
        ephemeral=True,
    )
