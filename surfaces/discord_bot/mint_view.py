# surfaces/discord_bot/mint_view.py
# The inverted mint handler: start_mint -> payment QR -> wait_for_mint ->
# offer-accept QR. ALL XRPL/CDN work happens in lfg_service (which stamps the
# Make Waves SourceTag); this module only orchestrates SDK calls and renders
# embeds. Written as a standalone coroutine handle_mint(svc, interaction) so the
# test can drive it with a fake svc + fake interaction (no View plumbing).
import logging

import discord

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import BAD_STATE_MESSAGES, MINT_OK_STATES, friendly_error
from surfaces.discord_bot import render


async def handle_mint(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    username = str(interaction.user)

    # 1. start the session (service detects payment path + builds the XUMM
    #    sign request; raises on no-wallet / already-in-progress)
    try:
        session = await svc.start_mint(user_id, username=username)
    except ServiceError as e:
        await interaction.followup.send(embed=render.error_embed(friendly_error(e)), ephemeral=True)
        return

    session_id = session["id"]
    payment_link = session.get("payment_link", "")

    # 2. payment step. A newcomer free mint has no payment and no QR — just
    #    confirm the freebie. Otherwise render the payment QR locally from the
    #    deeplink (the service exposes no hosted payment-QR url, only the link).
    if session.get("free"):
        await interaction.followup.send(embed=render.free_mint_embed(), ephemeral=True)
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
            await interaction.followup.send(
                embed=render.error_embed(friendly_error(e)), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=render.payment_embed(payment_link, push=session.get("payment_push")),
            file=render.file_from_png(qr_png, "payment_qr.png"),
            ephemeral=True,
        )

    # 3. wait for a terminal state (SDK polls /api/mint/<id> + backs off)
    try:
        final = await svc.wait_for_mint(user_id, session_id)
    except ServiceError as e:
        await interaction.followup.send(embed=render.error_embed(friendly_error(e)), ephemeral=True)
        return

    state = str(final.get("state") or "")
    if state not in MINT_OK_STATES:
        reason = BAD_STATE_MESSAGES.get(state, "Mint did not complete. Please try again.")
        await interaction.followup.send(embed=render.error_embed(reason), ephemeral=True)
        return

    # Large standalone artwork embed shown to the minter alongside the offer (#86).
    art = render.artwork_embed(final)

    # 4. offer-accept QR. Prefer the service-hosted accept_qr_url (no extra
    #    round-trip); otherwise render the accept deeplink ourselves.
    hosted_qr = final.get("accept_qr_url")
    if hosted_qr:
        await interaction.followup.send(
            embeds=[render.offer_embed(final, hosted_qr)] + ([art] if art else []),
            ephemeral=True,
        )
        return

    accept_link = final.get("accept_deeplink", "")
    try:
        qr_png = await svc.qr_png(accept_link)
    except ServiceError:
        # The mint succeeded; only the QR render failed. Still surface the offer
        # with the deeplink so the user can claim it.
        await interaction.followup.send(
            embeds=[render.offer_embed(final, "")] + ([art] if art else []),
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        embeds=[render.offer_embed(final, "attachment://offer_qr.png")] + ([art] if art else []),
        file=render.file_from_png(qr_png, "offer_qr.png"),
        ephemeral=True,
    )
