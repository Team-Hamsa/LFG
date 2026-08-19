# surfaces/discord_bot/claim_view.py
# /claim for Discord: read the caller's accrued BRIX, then pay it out.
# Standalone coroutine so tests can drive it with a fake interaction.
import logging
from typing import Any

import discord
from discord import Embed

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import friendly_error
from surfaces.discord_bot import render


def balance_embed(status: dict[str, Any]) -> Embed:
    claimable = status.get("claimable", 0)
    embed = Embed(
        title="🧱 Your BRIX",
        description=(
            f"**{claimable} BRIX** ready to claim."
            if claimable
            else "Nothing to claim yet — BRIX accrues daily for each NFT you hold that "
            "is **not** listed for sale."
        ),
        color=0x00FF00 if claimable else 0x888888,
    )
    if status.get("unlisted_last_epoch"):
        embed.add_field(
            name="Earning yesterday",
            value=f"{status['unlisted_last_epoch']} unlisted NFT(s)",
            inline=True,
        )
    if status.get("claimed_total"):
        embed.add_field(
            name="Claimed to date", value=f"{status['claimed_total']} BRIX", inline=True
        )
    return embed


def claimed_embed(result: dict[str, Any]) -> Embed:
    amount = result.get("amount", 0)
    if result.get("state") == "confirmed":
        embed = Embed(
            title="✅ BRIX claimed",
            description=f"**{amount} BRIX** is on its way to your wallet.",
            color=0x00FF00,
        )
        if result.get("tx_hash"):
            embed.add_field(name="Transaction", value=f"`{result['tx_hash']}`", inline=False)
        return embed
    if result.get("state") == "failed":
        return render.error_embed(
            "The payout did not go through, so your balance is untouched — try again.",
            title="⚠️ Claim failed",
        )
    # "submitted": the payout may or may not have landed. Never imply either
    # way, and never suggest retrying — the balance stays bound until recovery
    # reconciles it against the ledger.
    return Embed(
        title="⏳ Claim in progress",
        description=(
            f"Your claim for **{amount} BRIX** was submitted and is being confirmed "
            "on-ledger. Check back shortly — no need to claim again."
        ),
        color=0xFFAA00,
    )


async def handle_claim(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    username = str(interaction.user)

    try:
        status = await svc.brix_status(user_id, username=username)
    except ServiceError as e:
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title="⚠️ BRIX claim"), ephemeral=True
        )
        return

    if status.get("open_claim"):
        await interaction.followup.send(
            embed=claimed_embed(
                {"state": "submitted", "amount": status["open_claim"].get("amount", 0)}
            ),
            ephemeral=True,
        )
        return

    if not status.get("claimable"):
        await interaction.followup.send(embed=balance_embed(status), ephemeral=True)
        return

    try:
        result = await svc.brix_claim(user_id, username=username)
    except ServiceError as e:
        if e.code == "trustline_required":
            await interaction.followup.send(
                embed=render.error_embed(
                    "You need a BRIX trustline before you can receive a payout. Use the "
                    "**Set Trustline** button in `/letsgo`, then run `/claim` again. Your "
                    "balance is safe in the meantime.",
                    title="⚠️ Trustline required",
                ),
                ephemeral=True,
            )
            return
        logging.error(f"brix claim failed: {e}")
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title="⚠️ BRIX claim"), ephemeral=True
        )
        return

    await interaction.followup.send(embed=claimed_embed(result), ephemeral=True)
