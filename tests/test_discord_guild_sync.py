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


def test_sync_runs_only_once_across_reconnects(monkeypatch):
    # on_ready can re-fire on reconnect; _sync_commands_once must sync only the
    # first time so we don't burn Discord's application-command rate budget.
    import surfaces.discord_bot.bot as bot_mod

    monkeypatch.setattr(bot_mod, "_commands_synced", False)
    inner = AsyncMock()
    monkeypatch.setattr(bot_mod, "_sync_commands", inner)

    _run(bot_mod._sync_commands_once(_fake_tree(), guild_id=987654321))
    _run(bot_mod._sync_commands_once(_fake_tree(), guild_id=987654321))
    _run(bot_mod._sync_commands_once(_fake_tree(), guild_id=987654321))

    inner.assert_awaited_once()  # synced once despite three on_ready cycles


# --- Entry Point fallback (#236) -------------------------------------------
# Apps with an Activity carry a type-4 Entry Point command that discord.py
# <=2.7 can't include in tree.sync()'s bulk overwrite; Discord rejects the
# sync with 50240. The adapter must retry the bulk upsert with the existing
# Entry Point command(s) appended instead of failing startup sync.


def _http_exc(code):
    import discord

    resp = MagicMock(status=400, reason="Bad Request")
    return discord.HTTPException(resp, {"code": code, "message": "boom"})


def _fake_tree_with_client(registered_commands):
    tree = _fake_tree()
    cmd = MagicMock()
    cmd.to_dict.return_value = {"name": "letsgo", "type": 1}
    tree._get_all_commands.return_value = [cmd]
    client = MagicMock()
    client.application_id = 1234
    client.http.get_global_commands = AsyncMock(return_value=registered_commands)
    client.http.bulk_upsert_global_commands = AsyncMock()
    tree.client = client
    return tree


def test_50240_falls_back_to_bulk_upsert_with_entry_point():
    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree_with_client(
        [
            {"id": "111", "name": "launch", "type": 4},
            {"id": "222", "name": "letsgo", "type": 1},
        ]
    )
    tree.sync = AsyncMock(side_effect=_http_exc(50240))

    _run(_sync_commands(tree, guild_id=0))

    upsert = tree.client.http.bulk_upsert_global_commands
    upsert.assert_awaited_once()
    payload = upsert.await_args.kwargs["payload"]
    # The tree's own commands plus the registered Entry Point — and only it.
    assert {"name": "letsgo", "type": 1} in payload
    assert {"id": "111", "name": "launch", "type": 4} in payload
    assert not any(p.get("id") == "222" for p in payload)


def test_50240_fallback_still_runs_guild_sync():
    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree_with_client([{"id": "111", "name": "launch", "type": 4}])

    # Global sync (no kwargs) raises 50240; guild-scoped sync succeeds.
    async def sync(guild=None):
        if guild is None:
            raise _http_exc(50240)

    tree.sync = AsyncMock(side_effect=sync)

    _run(_sync_commands(tree, guild_id=987654321))

    tree.client.http.bulk_upsert_global_commands.assert_awaited_once()
    tree.copy_global_to.assert_called_once()
    guild_calls = [c for c in tree.sync.await_args_list if c.kwargs.get("guild") is not None]
    assert len(guild_calls) == 1


def test_other_http_errors_still_raise():
    import pytest

    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree_with_client([])
    tree.sync = AsyncMock(side_effect=_http_exc(50001))

    import discord

    with pytest.raises(discord.HTTPException):
        _run(_sync_commands(tree, guild_id=0))
    tree.client.http.bulk_upsert_global_commands.assert_not_awaited()
