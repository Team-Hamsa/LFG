# surfaces/discord_bot/register_view.py
# Xaman-verified /register for Discord: signin_start -> QR embed ->
# wait_for_signin -> report the verified wallet (the service stores it on
# 'signed'). Standalone coroutine so tests drive it with a fake interaction.
import logging

import discord

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import friendly_error
from surfaces._shared.signin_result import signin_outcome
from surfaces.discord_bot import render


async def handle_register(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)

    try:
        session = await svc.signin_start(user_id)
    except ServiceError as e:
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title="⚠️ Wallet registration"),
            ephemeral=True,
        )
        return

    uuid = session["uuid"]
    signin_link = session.get("signin_link", "")

    try:
        qr_png = await svc.qr_png(signin_link)
    except ServiceError as e:
        logging.error(f"signin QR render failed: {e}")
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title="⚠️ Wallet registration"),
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        embed=render.signin_embed(signin_link),
        file=render.file_from_png(qr_png, "signin_qr.png"),
        ephemeral=True,
    )

    try:
        final = await svc.wait_for_signin(user_id, uuid)
    except ServiceError as e:
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title="⚠️ Wallet registration"),
            ephemeral=True,
        )
        return

    if final.get("state") == "signed":
        wallet = final.get("wallet", "")
        done = discord.Embed(
            title="✅ Wallet verified and registered",
            description=f"Your registered wallet: **{wallet}**",
            color=0x00FF00,
        )
        await interaction.followup.send(embed=done, ephemeral=True)
        return
    await interaction.followup.send(
        embed=render.error_embed(
            signin_outcome(str(final.get("state") or "")), title="⚠️ Wallet registration"
        ),
        ephemeral=True,
    )
