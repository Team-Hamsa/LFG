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


# Discord webhook tokens die 15 minutes after the click, so the final follow-up
# must land before then even though the Xaman request itself lives 15 minutes.
# A line approved after the poll gives up still counts: /claim re-checks it
# on-ledger.
TRUSTLINE_POLL_TIMEOUT = 12 * 60
# How long the button under the trustline_required message stays pressable.
TRUSTLINE_VIEW_TIMEOUT = 600


class BrixTrustlineView(discord.ui.View):
    def __init__(self, svc: LFGServiceClient) -> None:
        super().__init__(timeout=TRUSTLINE_VIEW_TIMEOUT)
        self._svc = svc

    @discord.ui.button(label="🔗 Set BRIX Trustline", style=discord.ButtonStyle.primary)
    async def set_brix_trustline(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await handle_brix_trustline(self._svc, interaction)


def trustline_outcome_embed(final: dict[str, Any]) -> Embed:
    """The end of a BRIX trustline request, from the last status the service
    returned (lfg_service.app.handle_brix_trustline_status)."""
    state = final.get("state")
    title = "⚠️ BRIX trustline"
    if state == "signed":
        return Embed(
            title="✅ BRIX trustline set",
            description="Your wallet can now receive BRIX — run `/claim` again to collect it.",
            color=0x00FF00,
        )
    if state == "expired":
        return render.error_embed(
            "The trustline request expired. Run `/claim` again for a new one.", title=title
        )
    if state == "rejected" and final.get("code") == "signer_mismatch":
        return render.error_embed(
            "That was signed by a different wallet — sign it with the wallet you "
            "registered, then run `/claim` again.",
            title=title,
        )
    if state == "rejected":
        return render.error_embed(
            "The trustline transaction did not confirm on the ledger. Run `/claim` again to retry.",
            title=title,
        )
    # Timed out still pending/opened/validating: it may yet land, so claim
    # neither success nor failure.
    return Embed(
        title="⏳ BRIX trustline",
        description=(
            "Still waiting on Xaman. Once you've approved the trustline, run `/claim` again."
        ),
        color=0xFFAA00,
    )


async def handle_brix_trustline(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    username = str(interaction.user)

    try:
        start = await svc.brix_trustline(user_id, username=username)
    except ServiceError as e:
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title="⚠️ BRIX trustline"),
            ephemeral=True,
        )
        return

    if start.get("state") == "already_set":
        await interaction.followup.send(
            embed=Embed(
                title="✅ BRIX trustline",
                description="Your wallet already has a BRIX trustline — run `/claim` again.",
                color=0x00FF00,
            ),
            ephemeral=True,
        )
        return

    uuid = start["uuid"]
    xumm_link = start.get("xumm_url", "")
    try:
        qr_png = await svc.qr_png(xumm_link)
        await interaction.followup.send(
            embed=render.brix_trustline_embed(xumm_link, start.get("push")),
            file=render.file_from_png(qr_png, "brix_trustline_qr.png"),
            ephemeral=True,
        )
        final = await svc.wait_for_brix_trustline(user_id, uuid, timeout=TRUSTLINE_POLL_TIMEOUT)
    except ServiceError as e:
        # Past the start, the request may already be approved (a service
        # restart forgets it and 404s the poll) — /claim re-checks on-ledger.
        logging.error(f"brix trustline {uuid} failed: {e}")
        await _send_late(
            interaction,
            uuid,
            render.error_embed(
                "Couldn't finish checking the trustline request. Run `/claim` again — it "
                "picks up a trustline you already approved, or offers a new request.",
                title="⚠️ BRIX trustline",
            ),
        )
        return

    await _send_late(interaction, uuid, trustline_outcome_embed(final))


async def _send_late(interaction: discord.Interaction, uuid: str, embed: Embed) -> None:
    """A follow-up sent after the Xaman wait, which can outlive the webhook
    token: an undeliverable message is logged, never raised."""
    try:
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.HTTPException as e:  # NotFound included: the webhook token expired
        logging.warning(f"brix trustline {uuid}: message not delivered: {e}")


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
            # Not /letsgo's button: that sets the LFGO line, and payouts are BRIX.
            await interaction.followup.send(
                embed=render.error_embed(
                    "You need a BRIX trustline before you can receive a payout. Press "
                    "**Set BRIX Trustline** below, approve it in Xaman, then run `/claim` "
                    "again. Your balance is safe in the meantime.",
                    title="⚠️ Trustline required",
                ),
                view=BrixTrustlineView(svc),
                ephemeral=True,
            )
            return
        logging.error(f"brix claim failed: {e}")
        await interaction.followup.send(
            embed=render.error_embed(friendly_error(e), title="⚠️ BRIX claim"), ephemeral=True
        )
        return

    await interaction.followup.send(embed=claimed_embed(result), ephemeral=True)
