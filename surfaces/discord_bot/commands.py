# surfaces/discord_bot/commands.py
import discord
from discord import Embed

from surfaces._client.errors import ServiceError
from surfaces.discord_bot.bot import svc, tree
from surfaces.discord_bot.views import MintView


async def _register_impl(
    interaction: discord.Interaction,
    wallet: str,
    *,
    _svc=None,
) -> None:
    """Register the caller's wallet via the shared service (dual-writes
    identities + Users). Extracted so tests can inject a fake _svc."""
    client = _svc if _svc is not None else svc
    discord_id = str(interaction.user.id)
    discord_name = str(interaction.user)
    try:
        await client.register(discord_id, discord_name, wallet)
    except ServiceError as e:
        msg = e.message or "There was an error registering your wallet."
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message("Your wallet has been registered!", ephemeral=True)


@tree.command(name="register", description="Register your wallet")
async def register(interaction: discord.Interaction, wallet: str) -> None:
    await _register_impl(interaction, wallet)


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
