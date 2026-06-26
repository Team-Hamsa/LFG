# surfaces/discord_bot/bot.py
import asyncio
import logging
import random
import signal

import discord
from discord.ext import commands

from surfaces._client import LFGServiceClient
from surfaces.discord_bot import config
from user_db import create_users_table

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True


class RetryBot(commands.Bot):
    async def start(self, *args, **kwargs):
        max_retries = config.RETRY_MAX_ATTEMPTS
        base_delay = config.RETRY_BASE_DELAY
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    jitter = random.uniform(0, 2)
                    actual_delay = (base_delay * (2**attempt)) + jitter
                    logging.info(
                        f"Retry attempt {attempt + 1}/{max_retries} after {actual_delay:.2f}s delay"
                    )
                    await asyncio.sleep(actual_delay)
                await super().start(*args, **kwargs)
                return
            except Exception as e:
                logging.error(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise


bot = RetryBot(command_prefix="!", intents=intents)
tree = bot.tree

# One shared client for every handler (constructed here, entered in setup_hook).
svc = LFGServiceClient(config.LFG_SERVICE_URL, config.SERVICE_TOKEN_DISCORD, "discord")

# Background firehose consumer handle (started in setup_hook, cancelled in cleanup).
_events_task: asyncio.Task[None] | None = None


@bot.event
async def setup_hook() -> None:
    global _events_task
    # Enter the SDK's aiohttp session for the bot's lifetime.
    await svc.__aenter__()

    async def _announce(message: str, image_url: str | None) -> None:
        channel = bot.get_channel(config.ADMIN_LOG_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            if image_url:
                embed = discord.Embed(description=message)
                embed.set_image(url=image_url)
                await channel.send(embed=embed)
            else:
                await channel.send(message)

    async def _dm(uid: str, message: str, image_url: str | None) -> None:
        try:
            user = await bot.fetch_user(int(uid))
            if image_url:
                embed = discord.Embed(description=message)
                embed.set_image(url=image_url)
                await user.send(embed=embed)
            else:
                await user.send(message)
        except Exception as e:
            logging.warning(f"DM to {uid} failed: {e}")

    from surfaces.discord_bot.events import run_event_loop

    _events_task = asyncio.create_task(run_event_loop(svc, _announce, _dm))


@bot.event
async def on_ready() -> None:
    create_users_table()
    await tree.sync()
    assert bot.user is not None
    logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


async def cleanup() -> None:
    global _events_task
    logging.info("Performing cleanup before shutdown...")
    # Stop the firehose consumer BEFORE closing svc, so the generator's aclose()
    # can release the WebSocket on a still-live aiohttp session.
    if _events_task is not None:
        _events_task.cancel()
        await asyncio.gather(_events_task, return_exceptions=True)
        _events_task = None
    try:
        await svc.close()
    except Exception as e:
        logging.error(f"Error closing service client: {e}")
    try:
        if not bot.is_closed():
            await bot.close()
    except Exception as e:
        logging.error(f"Error during cleanup: {e}")


def _signal_handler(sig, frame) -> None:
    logging.info(f"Received signal {sig}, initiating shutdown...")
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup())
    loop.stop()


def main() -> None:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    try:
        bot.run(config.DISCORD_BOT_TOKEN)
    except Exception as e:
        logging.error(f"Failed to start bot: {e}")


if __name__ == "__main__":
    main()


# Register handlers (import for side effects: @tree.command + View classes).
# These run after `svc`/`tree` are defined above, so views/commands can import
# them. Listed order is irrelevant: commands imports MintView from views, which
# pulls views in transitively regardless of which line comes first.
from surfaces.discord_bot import admin  # noqa: E402,F401
from surfaces.discord_bot import commands as _cmds  # noqa: E402,F401
from surfaces.discord_bot import views as _views  # noqa: E402,F401
