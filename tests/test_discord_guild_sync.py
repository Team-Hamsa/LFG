# The Discord adapter does a global tree.sync() on ready (eventual, ~1h
# propagation) — unless the registered global commands already match the
# tree's would-be payload, in which case the global sync is skipped entirely
# (see the #241 section below). When DISCORD_GUILD_ID is set it should ALSO do
# an instant guild-scoped sync so /letsgo etc. appear immediately in the test
# guild. When unset (0): global sync (when needed) only.
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

    # registered=[] differs from the fixture's tree command, so the #241 gate
    # returns a genuine "changed" verdict and the global sync runs through the
    # intended path (not via the fail-open exception path a bare MagicMock hits).
    tree = _fake_tree_with_client([])
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

    # registered=[] vs a non-empty tree → genuine "changed", sync must run.
    tree = _fake_tree_with_client([])
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


def _fake_tree_with_client(registered_commands, tree_command_dicts=None):
    tree = _fake_tree()
    if tree_command_dicts is None:
        tree_command_dicts = [{"name": "letsgo", "type": 1}]
    cmds = []
    for d in tree_command_dicts:
        cmd = MagicMock()
        cmd.to_dict.return_value = d
        cmds.append(cmd)
    tree._get_all_commands.return_value = cmds
    client = MagicMock()
    client.application_id = 1234
    client.http.get_global_commands = AsyncMock(return_value=registered_commands)
    client.http.bulk_upsert_global_commands = AsyncMock()
    tree.client = client
    return tree


def test_50240_falls_back_to_bulk_upsert_with_entry_point():
    from surfaces.discord_bot.bot import _sync_commands

    # The registered letsgo carries a stale description so the #241 unchanged
    # gate sees a genuine change and the sync (and its fallback) still runs.
    tree = _fake_tree_with_client(
        [
            {"id": "111", "name": "launch", "type": 4},
            {"id": "222", "name": "letsgo", "type": 1, "description": "stale"},
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


# --- Skip-unchanged global sync (#241) --------------------------------------
# Every restart used to global-sync unconditionally; tree.sync() always fails
# 50240 on this app (Entry Point), so the fallback bulk PUT ran every boot and
# rewrote the Entry Point's version snowflake — breaking cached Activity
# launches in running Discord clients. The adapter must diff the registered
# commands against the tree's would-be payload and skip the global sync (and
# its fallback) entirely when nothing changed.


def test_unchanged_commands_skip_global_sync_entirely():
    from surfaces.discord_bot.bot import _sync_commands

    # What the tree would upsert (real Command.to_dict / ContextMenu.to_dict
    # shapes: nsfw/dm_permission always present, contexts/integration_types
    # None when unset, perms as int, context menu without description/options).
    tree_cmds = [
        {
            "name": "letsgo",
            "description": "Mint an NFT",
            "type": 1,
            "options": [
                {"type": 3, "name": "flavor", "description": "pick one", "required": False}
            ],
            "nsfw": False,
            "dm_permission": True,
            "default_member_permissions": None,
            "contexts": None,
            "integration_types": None,
        },
        {
            "name": "Swap Traits",
            "type": 2,
            "dm_permission": True,
            "contexts": None,
            "integration_types": None,
            "default_member_permissions": 8,
            "nsfw": False,
        },
    ]
    # The same commands as GET /applications/{id}/commands returns them,
    # dressed with server noise: id/application_id/version, str perms, missing
    # nsfw, explicit-null dm_permission, missing `required`, null
    # localizations, server-resolved contexts/integration_types, plus the
    # type-4 Entry Point the tree can never produce.
    registered = [
        {"id": "111", "application_id": "999", "version": "1000", "name": "launch", "type": 4},
        {
            "id": "222",
            "application_id": "999",
            "version": "1001",
            "name": "letsgo",
            "description": "Mint an NFT",
            "type": 1,
            "options": [{"type": 3, "name": "flavor", "description": "pick one"}],
            "dm_permission": None,
            "default_member_permissions": None,
            "contexts": [0, 1, 2],
            "integration_types": [0],
            "name_localizations": None,
            "description_localizations": None,
        },
        {
            "id": "333",
            "application_id": "999",
            "version": "1002",
            "name": "Swap Traits",
            "type": 2,
            "description": "",
            "options": [],
            "default_member_permissions": "8",
            "contexts": [0, 1, 2],
            "integration_types": [0],
        },
    ]
    tree = _fake_tree_with_client(registered, tree_command_dicts=tree_cmds)

    _run(_sync_commands(tree, guild_id=987654321))

    # No global sync, no bulk upsert — the Entry Point's version is untouched.
    global_calls = [c for c in tree.sync.await_args_list if not c.kwargs]
    assert not global_calls
    tree.client.http.bulk_upsert_global_commands.assert_not_awaited()
    # The instant guild sync still runs even when the global sync is skipped.
    tree.copy_global_to.assert_called_once()
    guild_calls = [c for c in tree.sync.await_args_list if c.kwargs.get("guild") is not None]
    assert len(guild_calls) == 1


def test_changed_commands_still_sync():
    from surfaces.discord_bot.bot import _sync_commands

    # Registered description differs from the tree's — a genuine change.
    tree = _fake_tree_with_client(
        [{"id": "222", "name": "letsgo", "type": 1, "description": "old text"}]
    )

    _run(_sync_commands(tree, guild_id=0))

    tree.sync.assert_awaited_once_with()


def test_diff_check_failure_fails_open_to_sync():
    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree_with_client([])
    tree.client.http.get_global_commands = AsyncMock(side_effect=RuntimeError("boom"))

    _run(_sync_commands(tree, guild_id=0))

    tree.sync.assert_awaited_once_with()  # current behavior preserved


def test_missing_application_id_fails_open_to_sync():
    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree_with_client([])
    tree.client.application_id = None

    _run(_sync_commands(tree, guild_id=0))

    tree.sync.assert_awaited_once_with()


# --- Normalization pitfalls (#241) ------------------------------------------


def test_normalized_command_strips_server_noise():
    from surfaces.discord_bot.bot import _normalized_command

    wire = {
        "id": "1",
        "application_id": "2",
        "version": "3",
        "name": "x",
        "type": 1,
        "description": "d",
        "default_member_permissions": "2147483648",  # str on the wire
        "dm_permission": None,  # explicit null means True
        # nsfw omitted means False
        "name_localizations": None,
        "description_localizations": {},
    }
    ours = {
        "name": "x",
        "description": "d",
        "type": 1,
        "options": [],
        "nsfw": False,
        "dm_permission": True,
        "default_member_permissions": 2147483648,
        "contexts": None,
        "integration_types": None,
    }
    assert _normalized_command(wire) == _normalized_command(ours)


def test_normalized_context_menu_matches_wire_shape():
    from surfaces.discord_bot.bot import _normalized_command

    # GET returns description "" and options [] for context menus; ContextMenu
    # .to_dict emits neither key (and no dm_permission key is also True-ish on
    # the wire side).
    wire = {"id": "9", "name": "Swap", "type": 2, "description": "", "options": []}
    ours = {
        "name": "Swap",
        "type": 2,
        "dm_permission": True,
        "contexts": None,
        "integration_types": None,
        "default_member_permissions": None,
        "nsfw": False,
    }
    assert _normalized_command(wire) == _normalized_command(ours)


def test_normalized_options_recurse_missing_required():
    from surfaces.discord_bot.bot import _normalized_command

    wire = {
        "name": "x",
        "type": 1,
        "description": "d",
        "options": [
            {
                "type": 1,
                "name": "sub",
                "description": "s",
                "options": [{"type": 3, "name": "arg", "description": "a"}],
            }
        ],
    }
    ours = dict(wire)
    ours["options"] = [
        {
            "type": 1,
            "name": "sub",
            "description": "s",
            "required": False,
            "options": [{"type": 3, "name": "arg", "description": "a", "required": False}],
        }
    ]
    assert _normalized_command(wire) == _normalized_command(ours)


def test_payload_match_excludes_entry_point_and_wildcards_none():
    from surfaces.discord_bot.bot import _global_payloads_match

    ours = [
        {
            "name": "x",
            "type": 1,
            "description": "d",
            "options": [],
            "nsfw": False,
            "dm_permission": True,
            "default_member_permissions": None,
            "contexts": None,  # unset in the tree — matches any server value
            "integration_types": None,
        }
    ]
    registered = [
        {"id": "1", "name": "launch", "type": 4},  # excluded from the diff
        {
            "id": "2",
            "application_id": "3",
            "version": "9",
            "name": "x",
            "type": 1,
            "description": "d",
            "contexts": [0, 1, 2],  # server-resolved defaults
            "integration_types": [0],
        },
    ]
    assert _global_payloads_match(ours, registered)


def test_payload_match_explicit_contexts_compare_exactly():
    from surfaces.discord_bot.bot import _global_payloads_match

    ours = [
        {"name": "x", "type": 1, "description": "d", "contexts": [0], "integration_types": None}
    ]
    registered = [
        {
            "id": "2",
            "name": "x",
            "type": 1,
            "description": "d",
            "contexts": [0, 1, 2],
            "integration_types": [0],
        }
    ]
    # Explicit tree-side contexts must match exactly (no wildcard).
    assert not _global_payloads_match(ours, registered)

    registered[0]["contexts"] = [0]
    assert _global_payloads_match(ours, registered)


def test_payload_match_explicit_contexts_normalize_order_and_type():
    from surfaces.discord_bot.bot import _global_payloads_match

    # A future command may set explicit contexts/integration_types; the gate
    # must treat them as order-insensitive int sets (the server may echo sorted
    # ints where the tree emitted unsorted — or string — values).
    ours = [
        {
            "name": "x",
            "type": 1,
            "description": "d",
            "contexts": [1, 0],
            "integration_types": [1, 0],
        }
    ]
    registered = [
        {
            "id": "2",
            "name": "x",
            "type": 1,
            "description": "d",
            "contexts": ["0", "1"],
            "integration_types": [0, 1],
        }
    ]
    assert _global_payloads_match(ours, registered)


def test_payload_match_detects_real_change():
    from surfaces.discord_bot.bot import _global_payloads_match

    ours = [{"name": "x", "type": 1, "description": "new"}]
    registered = [{"id": "2", "name": "x", "type": 1, "description": "old"}]
    assert not _global_payloads_match(ours, registered)
    # A missing command is a change too.
    assert not _global_payloads_match(ours, [{"id": "1", "name": "launch", "type": 4}])


def test_normalization_matches_real_discordpy_to_dict_shapes():
    # Canary against discord.py itself: every other test hand-writes the
    # tree-side dicts, so a discord.py upgrade that changes to_dict()'s output
    # shape would silently flip the fail-open gate to "changed" on every boot
    # (Entry Point version rewritten again — the exact #241 regression) with no
    # test signal. Feed REAL Command/ContextMenu.to_dict() output through the
    # gate so shape drift fails loudly at upgrade time.
    from types import SimpleNamespace

    import discord
    from discord import app_commands
    from discord.app_commands.installs import AppCommandContext, AppInstallationType

    from surfaces.discord_bot.bot import _global_payloads_match, _normalized_command

    @app_commands.command(name="letsgo", description="Mint an NFT")
    @app_commands.describe(flavor="pick one")
    async def letsgo(interaction: discord.Interaction, flavor: str | None = None) -> None: ...

    async def swap_cb(interaction: discord.Interaction, member: discord.Member) -> None: ...

    menu = app_commands.ContextMenu(name="Swap Traits", callback=swap_cb)
    menu.default_permissions = discord.Permissions(8)

    # NOT a bare MagicMock: to_dict() merges tree.allowed_contexts/installs,
    # and a MagicMock there iterates as empty, corrupting contexts to []
    # instead of the None a real default tree emits.
    tree_stub = SimpleNamespace(
        allowed_contexts=AppCommandContext(),
        allowed_installs=AppInstallationType(),
        translator=None,
    )
    real_cmd = letsgo.to_dict(tree_stub)
    real_menu = menu.to_dict(tree_stub)

    # The hand-written mirrors used by the skip-gate test above must match the
    # real to_dict() output under normalization.
    mirror_cmd = {
        "name": "letsgo",
        "description": "Mint an NFT",
        "type": 1,
        "options": [{"type": 3, "name": "flavor", "description": "pick one", "required": False}],
        "nsfw": False,
        "dm_permission": True,
        "default_member_permissions": None,
        "contexts": None,
        "integration_types": None,
    }
    mirror_menu = {
        "name": "Swap Traits",
        "type": 2,
        "dm_permission": True,
        "contexts": None,
        "integration_types": None,
        "default_member_permissions": 8,
        "nsfw": False,
    }
    assert _normalized_command(real_cmd) == _normalized_command(mirror_cmd)
    assert _normalized_command(real_menu) == _normalized_command(mirror_menu)

    # And the real payload must match the simulated wire echo (same server
    # noise as test_unchanged_commands_skip_global_sync_entirely).
    registered = [
        {"id": "111", "application_id": "999", "version": "1000", "name": "launch", "type": 4},
        {
            "id": "222",
            "application_id": "999",
            "version": "1001",
            "name": "letsgo",
            "description": "Mint an NFT",
            "type": 1,
            "options": [{"type": 3, "name": "flavor", "description": "pick one"}],
            "dm_permission": None,
            "default_member_permissions": None,
            "contexts": [0, 1, 2],
            "integration_types": [0],
            "name_localizations": None,
            "description_localizations": None,
        },
        {
            "id": "333",
            "application_id": "999",
            "version": "1002",
            "name": "Swap Traits",
            "type": 2,
            "description": "",
            "options": [],
            "default_member_permissions": "8",
            "contexts": [0, 1, 2],
            "integration_types": [0],
        },
    ]
    assert _global_payloads_match([real_cmd, real_menu], registered)


def test_50240_fallback_reuses_the_gates_registered_fetch():
    # The unchanged-gate fetches GET /commands moments before the sync; the
    # changed->50240 fallback must reuse that result rather than fetching a
    # second time (one GET per boot, not two).
    from surfaces.discord_bot.bot import _sync_commands

    tree = _fake_tree_with_client([{"id": "111", "name": "launch", "type": 4}])
    tree.sync = AsyncMock(side_effect=_http_exc(50240))
    _run(_sync_commands(tree, guild_id=0))
    assert tree.client.http.get_global_commands.await_count == 1
    upsert = tree.client.http.bulk_upsert_global_commands
    upsert.assert_awaited_once()
    # The reused fetch still supplies the Entry Point entry for the upsert.
    assert {"id": "111", "name": "launch", "type": 4} in upsert.await_args.kwargs["payload"]
