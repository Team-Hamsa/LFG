# surfaces/discord_bot/commands.py
import discord
from discord import Embed

from surfaces.discord_bot.bot import svc, tree
from surfaces.discord_bot.views import MintView


@tree.command(name="register", description="Verify and register your wallet with Xaman")
async def register(interaction: discord.Interaction) -> None:
    from surfaces.discord_bot.register_view import handle_register

    await handle_register(svc, interaction)


@tree.command(name="letsgo", description="Open the NFT minting interface")
async def letsgo(interaction: discord.Interaction) -> None:
    embed = Embed(
        title="🎮 LFG NFT Minting Interface",
        description=(
            "Welcome to the LFG NFT Minting Interface!\n\n"
            "**Requirements:**\n"
            "• XUMM Wallet\n"
            "• LFGO Tokens\n"
            "• XRPL Trustline\n\n"
            "Choose an action below:"
        ),
        color=0x00FF00,
    )
    embed.add_field(
        name="🎨 Mint NFT", value="Create a unique NFT with random traits", inline=False
    )
    embed.add_field(
        name="🔗 Set LFGO Trustline",
        value="Set up your XRPL trustline for LFGO tokens",
        inline=False,
    )
    embed.add_field(name="💰 Buy LFGO", value="Purchase LFGO tokens to mint NFTs", inline=False)
    embed.set_footer(text="Buttons are active for 10 minutes • All actions are ephemeral")
    await interaction.response.send_message(embed=embed, view=MintView(), ephemeral=True)
