# surfaces/discord_bot/link_view.py
# Cross-surface /link for Discord (#90): link_start -> QR embed ->
# wait_for_link -> confirm "Linked to your account" listing the OTHER surfaces
# the proven wallet is on. Functionally /register, but account-aware: signing
# the SAME wallet on a 2nd surface is the link. Standalone coroutine so tests
# drive it with a fake interaction.
import logging

import discord

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.account_result import linked_summary
from surfaces._shared.mint_result import friendly_error
from surfaces._shared.signin_result import signin_outcome
from surfaces.discord_bot import render

_TITLE = "⚠️ Link wallet"


async def handle_link(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    username = str(interaction.user)

    try:
        session = await svc.link_start(user_id, username=username)
    except ServiceError as e:
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title=_TITLE), ephemeral=True
        )
        return

    uuid = session["uuid"]
    signin_link = session.get("signin_link", "")

    try:
        qr_png = await svc.qr_png(signin_link)
    except ServiceError as e:
        logging.error(f"link QR render failed: {e}")
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title=_TITLE), ephemeral=True
        )
        return
    await interaction.followup.send(
        embed=render.signin_embed(signin_link),
        file=render.file_from_png(qr_png, "signin_qr.png"),
        ephemeral=True,
    )

    try:
        final = await svc.wait_for_link(user_id, uuid)
    except ServiceError as e:
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title=_TITLE), ephemeral=True
        )
        return

    if final.get("state") == "signed":
        account = final.get("account") or {"wallet": final.get("wallet", ""), "identities": []}
        summary = linked_summary(account, current_platform="discord", current_user_id=user_id)
        await interaction.followup.send(embed=render.linked_embed(summary), ephemeral=True)
        return
    await interaction.followup.send(
        embed=render.error_embed(signin_outcome(str(final.get("state") or "")), title=_TITLE),
        ephemeral=True,
    )
