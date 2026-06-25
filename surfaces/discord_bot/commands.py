# surfaces/discord_bot/commands.py
import discord

from surfaces._client.errors import ServiceError
from surfaces.discord_bot.bot import svc, tree


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
