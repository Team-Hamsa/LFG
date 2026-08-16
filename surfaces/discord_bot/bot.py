# surfaces/discord_bot/bot.py
import asyncio
import logging
import random
import signal
from typing import Any

import discord
from discord.ext import commands

from lfg_core.user_db import create_users_table
from surfaces._client import LFGServiceClient
from surfaces.discord_bot import config

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
                embed = discord.Embed(description=message, color=0x00FF00)
                embed.set_image(url=image_url)
                await channel.send(embed=embed)
            else:
                await channel.send(message)

    async def _dm(uid: str, message: str, image_url: str | None) -> None:
        try:
            user = await bot.fetch_user(int(uid))
            if image_url:
                embed = discord.Embed(description=message, color=0x00FF00)
                embed.set_image(url=image_url)
                await user.send(embed=embed)
            else:
                await user.send(message)
        except Exception as e:
            logging.warning(f"DM to {uid} failed: {e}")

    from surfaces.discord_bot.events import run_event_loop

    _events_task = asyncio.create_task(run_event_loop(svc, _announce, _dm))


# Discord rejects a bulk command overwrite that drops an app's Entry Point
# command (apps with an Activity have one). discord.py <=2.7 doesn't model
# type-4 (PRIMARY_ENTRY_POINT) commands, so tree.sync() always omits it (#236).
_ENTRY_POINT_ERROR_CODE = 50240
_ENTRY_POINT_COMMAND_TYPE = 4


async def _sync_global_including_entry_point(
    command_tree: "discord.app_commands.CommandTree[Any]",
    registered: "list[dict[str, Any]] | None" = None,
) -> None:
    """Re-run the global bulk upsert with the app's existing Entry Point
    command(s) appended, since tree.sync() can't include them itself.
    `registered` lets the caller reuse a moments-old GET /commands result
    (the unchanged-gate fetches one right before syncing); None fetches."""
    client = command_tree.client
    app_id = client.application_id
    if app_id is None:
        raise discord.app_commands.MissingApplicationID
    if registered is None:
        registered = await client.http.get_global_commands(app_id)
    entry_points = [dict(cmd) for cmd in registered if cmd.get("type") == _ENTRY_POINT_COMMAND_TYPE]
    # _get_all_commands is private, but it is exactly how tree.sync() builds its
    # own bulk-upsert payload — there is no public equivalent that includes
    # context menus. Revisit if a discord.py upgrade grows Entry Point support.
    payload = [cmd.to_dict(command_tree) for cmd in command_tree._get_all_commands(guild=None)]  # noqa: SLF001
    payload.extend(entry_points)  # keep by id — Discord preserves matched ids
    await client.http.bulk_upsert_global_commands(app_id, payload=payload)


# The fallback above bulk-PUTs every global command, and Discord rewrites each
# command's server-assigned `version` snowflake on every PUT — including the
# Entry Point's — even when nothing changed. Running Discord clients cache
# command versions, so an Entry Point version bump breaks every Activity
# launch ("Failed to Launch Activity") until the client fully relaunches, and
# the #223 deployer restarts this bot on every deploy (#241). So before
# syncing, diff the registered global commands against the tree's would-be
# payload and skip the whole global sync when nothing actually changed.

# Server-assigned keys that GET /commands returns but to_dict() never emits.
_SERVER_ASSIGNED_KEYS = frozenset({"id", "application_id", "version", "guild_id"})
# For these two fields the server echoes resolved defaults (e.g. contexts
# [0, 1, 2]) that are unknowable client-side, while to_dict() emits None when
# unset (this codebase never sets them). A tree-side None therefore matches
# ANY registered value; explicit tree-side values must match exactly.
_SERVER_RESOLVED_DEFAULT_KEYS = ("contexts", "integration_types")


def _normalized_option(option: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize one option/choice dict (recursively) for comparison."""
    out = dict(option)
    out["description"] = out.get("description") or ""
    out["required"] = bool(out.get("required"))  # NotRequired on the wire; missing means False
    # Keys either side emits only when set/truthy; drop the empty/false/null
    # variants so absence compares equal to them.
    for key in (
        "options",
        "choices",
        "channel_types",
        "autocomplete",
        "name_localizations",
        "description_localizations",
    ):
        if not out.get(key):
            out.pop(key, None)
    if "options" in out:
        out["options"] = [_normalized_option(o) for o in out["options"]]
    if "choices" in out:
        out["choices"] = [_normalized_option(c) for c in out["choices"]]
    return out


def _normalized_command(cmd: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize a command dict — from either the GET /commands wire shape
    or the tree's to_dict() shape — so the two sides compare equal exactly
    when they describe the same command."""
    out = {k: v for k, v in cmd.items() if k not in _SERVER_ASSIGNED_KEYS}
    out["type"] = int(out.get("type", 1))
    # GET omits nsfw when false; to_dict() always emits it.
    out["nsfw"] = bool(out.get("nsfw"))
    # GET may omit dm_permission OR send explicit null — both mean True.
    dm_permission = out.get("dm_permission")
    out["dm_permission"] = True if dm_permission is None else bool(dm_permission)
    # GET returns the permissions bitfield as a string; to_dict() emits an int.
    permissions = out.get("default_member_permissions")
    out["default_member_permissions"] = None if permissions is None else int(permissions)
    # ContextMenu.to_dict() emits neither description nor options; GET returns
    # description "" (and may return options) for the same command.
    out["description"] = out.get("description") or ""
    out["options"] = [_normalized_option(o) for o in (out.get("options") or [])]
    # No translator is configured in this codebase, so localizations are never
    # ours; GET may echo them as null or {} — treat missing/null/{} alike.
    for key in ("name_localizations", "description_localizations"):
        if not out.get(key):
            out.pop(key, None)
    # Sorted int lists (order-insensitive), or None (wildcard — see compare).
    for key in _SERVER_RESOLVED_DEFAULT_KEYS:
        value = out.get(key)
        out[key] = None if value is None else sorted(int(v) for v in value)
    return out


def _command_sort_key(cmd: dict[str, Any]) -> tuple[int, str]:
    return (int(cmd.get("type", 1)), str(cmd.get("name", "")))


def _global_payloads_match(payload: list[dict[str, Any]], registered: list[dict[str, Any]]) -> bool:
    """Whether the tree's would-be global payload already matches the
    registered global commands. Entry Point (type-4) entries are excluded
    from the registered side — the tree can never contain them, and leaving
    them untouched is the whole point of skipping."""
    ours = sorted((_normalized_command(c) for c in payload), key=_command_sort_key)
    theirs = sorted(
        (_normalized_command(c) for c in registered if c.get("type") != _ENTRY_POINT_COMMAND_TYPE),
        key=_command_sort_key,
    )
    if len(ours) != len(theirs):
        return False
    for mine, remote in zip(ours, theirs, strict=True):
        # Tree-side None means "let the server resolve the default" — the
        # resolved value is unknowable client-side, so it matches any. Compare
        # filtered views instead of mutating the normalized dicts in place.
        wildcards = {k for k in _SERVER_RESOLVED_DEFAULT_KEYS if mine.get(k) is None}
        mine_view = {k: v for k, v in mine.items() if k not in wildcards}
        remote_view = {k: v for k, v in remote.items() if k not in wildcards}
        if mine_view != remote_view:
            return False
    return True


async def _global_commands_unchanged(
    command_tree: "discord.app_commands.CommandTree[Any]",
) -> "tuple[bool, list[dict[str, Any]] | None]":
    """(unchanged, registered): unchanged is True only when the registered
    global commands provably match what a sync would upsert; registered is
    the fetched command list for the 50240 fallback to reuse (None when the
    fetch itself failed). Fail-open by design: on any doubt (no application
    id, fetch failure, unexpected payload shapes) return False so the normal
    sync + fallback path runs — the worst case must always be today's
    behavior, never a silently skipped genuine change."""
    try:
        client = command_tree.client
        app_id = client.application_id
        if app_id is None:
            logging.info("No application_id available; cannot diff global commands, syncing")
            return False, None
        registered = list(await client.http.get_global_commands(app_id))
        # Same payload construction as _sync_global_including_entry_point.
        payload = [cmd.to_dict(command_tree) for cmd in command_tree._get_all_commands(guild=None)]  # noqa: SLF001
        return _global_payloads_match(payload, registered), registered
    except Exception:
        logging.warning(
            "Global command diff check failed; falling back to a normal sync", exc_info=True
        )
        return False, None


async def _sync_commands(
    command_tree: "discord.app_commands.CommandTree[Any]", guild_id: int
) -> None:
    """Sync slash commands. Does the global sync (eventual, propagates to all
    guilds in ~1h) unless the registered commands already match — a no-op sync
    is not free, it rewrites the Entry Point's version snowflake via the 50240
    fallback and breaks cached Activity launches (#241). When guild_id is
    non-zero, ALSO does an instant guild-scoped sync so commands appear
    immediately in that test/home guild."""
    unchanged, registered = await _global_commands_unchanged(command_tree)
    if unchanged:
        logging.info(
            "Global commands unchanged; skipping global sync (Entry Point version preserved)"
        )
    else:
        try:
            await command_tree.sync()  # global (eventual, for all guilds)
        except discord.HTTPException as exc:
            if exc.code != _ENTRY_POINT_ERROR_CODE:
                raise
            logging.warning(
                "Global command sync rejected (50240: Entry Point command); "
                "retrying with the Entry Point command included",
                exc_info=exc,
            )
            await _sync_global_including_entry_point(command_tree, registered=registered)
    if guild_id:
        guild = discord.Object(id=guild_id)
        command_tree.copy_global_to(guild=guild)
        await command_tree.sync(guild=guild)  # instant in the configured guild


# on_ready can fire again on every gateway reconnect/resume; syncing the command
# tree each time would burn Discord's application-command rate budget (and risk a
# 24h lockout). Sync exactly once per process.
_commands_synced = False


async def _sync_commands_once(
    command_tree: "discord.app_commands.CommandTree[Any]", guild_id: int
) -> None:
    """Sync the command tree at most once per process (on_ready can re-fire on
    every reconnect)."""
    global _commands_synced
    if _commands_synced:
        return
    await _sync_commands(command_tree, guild_id)
    _commands_synced = True


@bot.event
async def on_ready() -> None:
    create_users_table()  # idempotent (CREATE TABLE IF NOT EXISTS)
    await _sync_commands_once(tree, config.DISCORD_GUILD_ID)
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
