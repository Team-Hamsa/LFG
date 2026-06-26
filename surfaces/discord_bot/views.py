# surfaces/discord_bot/views.py
# The MintView shell: three buttons (mint / trustline / buy). Mint delegates to
# the inverted handle_mint (all XRPL work in the service). Trustline keeps its
# bot-local XUMM flow (D2=A). Buy is a static URL button.
from typing import Any

import discord
from discord import Embed
from discord.ui import Button, View

from surfaces.discord_bot import config, trustline
from surfaces.discord_bot.bot import svc
from surfaces.discord_bot.mint_view import handle_mint
from surfaces.discord_bot.register_view import handle_register
from surfaces.discord_bot.trustline import safe_followup
from user_db import get_user


class MintView(View):
    def __init__(self) -> None:
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.buy_button: Button[Any] = Button(
            label="💰 Buy Token",
            style=discord.ButtonStyle.success,
            url=config.EXTERNAL_WEBSITE_URL,
        )
        self.add_item(self.buy_button)

    @discord.ui.button(label="🎨 Mint NFT", style=discord.ButtonStyle.primary)
    async def mint_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await handle_mint(svc, interaction)

    @discord.ui.button(label="🔐 Register Wallet", style=discord.ButtonStyle.primary)
    async def register_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await handle_register(svc, interaction)

    @discord.ui.button(label="🔗 Set LFGO Trustline", style=discord.ButtonStyle.secondary)
    async def trustline_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.defer(ephemeral=True)

        # UX guard (relocated from legacy main.py): must register a wallet first.
        user_data = get_user(str(interaction.user.id))
        if not user_data or not user_data.get("address"):
            await safe_followup(
                interaction, "Please register your wallet first using /register", ephemeral=True
            )
            return

        data = await trustline.create_trustline_request()
        if not data:
            await safe_followup(
                interaction,
                "Failed to create trustline request. Please try again.",
                ephemeral=True,
            )
            return
        embed = Embed(
            title="🔗 Set Up LFGO Token Trustline",
            description=(
                "Please set up a trustline for the LFGO token.\n\n"
                "**Steps:**\n"
                "1. Scan the QR code with your XUMM app\n"
                "2. Review and approve the trustline\n"
                "3. Wait for confirmation\n\n"
                f"[Open in XUMM]({data['xumm_url']})"
            ),
            color=0x00FF00,
        )
        embed.set_image(url=data["qr_url"])
        embed.set_footer(text="Trustline request expires in 5 minutes")
        await safe_followup(interaction, embed=embed, ephemeral=True)
        await trustline.poll_trustline_status(interaction, data)
