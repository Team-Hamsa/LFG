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


@bot.event
async def setup_hook() -> None:
    # Enter the SDK's aiohttp session for the bot's lifetime.
    await svc.__aenter__()


@bot.event
async def on_ready() -> None:
    create_users_table()
    await tree.sync()
    assert bot.user is not None
    logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


async def cleanup() -> None:
    logging.info("Performing cleanup before shutdown...")
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
from surfaces.discord_bot import admin  # noqa: E402,F401
