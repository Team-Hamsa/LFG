# The Discord adapter does a global tree.sync() on ready (eventual, ~1h
# propagation). When DISCORD_GUILD_ID is set it should ALSO do an instant
# guild-scoped sync so /letsgo etc. appear immediately in the test guild.
# When unset (0), behavior is unchanged: global sync only.
import asyncio
from unittest.mock import AsyncMock, MagicMock


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_tree():
    tree = MagicMock()
    tree.sync = AsyncMock()
    tree.copy_global_to = MagicMock()
    return tree


def test_guild_sync_runs_when_guild_id_set():
    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree()
    _run(_sync_commands(tree, guild_id=987654321))

    # Global sync (no guild kwarg) plus an instant guild sync (guild kwarg).
    assert tree.sync.await_count == 2
    tree.copy_global_to.assert_called_once()
    # One call is the bare global sync; the other targets the guild.
    guild_calls = [c for c in tree.sync.await_args_list if c.kwargs.get("guild") is not None]
    global_calls = [c for c in tree.sync.await_args_list if not c.kwargs]
    assert len(guild_calls) == 1
    assert len(global_calls) == 1


def test_only_global_sync_when_guild_id_unset():
    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree()
    _run(_sync_commands(tree, guild_id=0))

    tree.sync.assert_awaited_once_with()  # bare global sync only
    tree.copy_global_to.assert_not_called()
