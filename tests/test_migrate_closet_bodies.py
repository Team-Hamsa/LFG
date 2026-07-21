# tests/test_migrate_closet_bodies.py
# Migration: legacy closet_bodies (edition ints) -> ("Body", value) rows in
# closet_assets. All network calls are replaced with a fake `sync_fn` so no
# XRPL connection is needed.

import asyncio
import os
import sqlite3
import sys

# Make scripts/ importable (mirrors test_migrate_bucket_to_closet.py)
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

# Stub out env vars that config.py requires at import time (env-guard preamble)
os.environ.setdefault("BUNNY_PULL_ZONE", "test.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

import migrate_closet_bodies_to_values as migration  # noqa: E402

from lfg_core import economy_store as es  # noqa: E402
from lfg_core import trait_economy  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    return conn


def _freeze_simple_genesis(conn: sqlite3.Connection) -> None:
    genesis = trait_economy.Genesis(
        trait_counts={},
        edition_bodies={3: ("Milady", "milady")},
    )
    es.freeze_genesis(conn, genesis, meta={})


class _SyncRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list, list]] = []

    async def __call__(self, conn, owner, assets, bodies) -> None:
        self.calls.append((list(assets), list(bodies)))


def test_migrate_owner_converts_legacy_body_to_asset():
    conn = _mem_conn()
    _freeze_simple_genesis(conn)

    owner = "rUser"
    es.set_closet_contents(conn, owner, [("Head", "Cap", 1)], [3])

    sync = _SyncRecorder()
    result = _run(migration.migrate_owner(conn, owner, sync))

    assert result["skipped"] is False

    assets = sorted(
        (slot, value, count) for o, slot, value, count in es.read_closet_assets(conn) if o == owner
    )
    assert assets == [("Body", "Milady", 1), ("Head", "Cap", 1)]

    bodies = [ed for o, ed in es.read_closet_bodies(conn) if o == owner]
    assert bodies == []

    assert len(sync.calls) == 1
    called_assets, called_bodies = sync.calls[0]
    assert sorted(called_assets) == [("Body", "Milady", 1), ("Head", "Cap", 1)]
    assert called_bodies == []

    # Second run is a no-op — idempotent.
    sync2 = _SyncRecorder()
    result2 = _run(migration.migrate_owner(conn, owner, sync2))
    assert result2["skipped"] is True
    assert sync2.calls == []


def test_migrate_owner_no_legacy_bodies_is_noop():
    conn = _mem_conn()
    _freeze_simple_genesis(conn)

    owner = "rNoBodies"
    es.set_closet_contents(conn, owner, [("Head", "Cap", 1)], [])

    sync = _SyncRecorder()
    result = _run(migration.migrate_owner(conn, owner, sync))

    assert result["skipped"] is True
    assert sync.calls == []


def test_migrate_owner_unknown_edition_left_in_place():
    conn = _mem_conn()
    _freeze_simple_genesis(conn)

    owner = "rUnknown"
    es.set_closet_contents(conn, owner, [], [999])

    sync = _SyncRecorder()
    result = _run(migration.migrate_owner(conn, owner, sync))

    # Unknown edition is neither converted nor dropped.
    bodies = [ed for o, ed in es.read_closet_bodies(conn) if o == owner]
    assert bodies == [999]
    assert result["unknown_editions"] == [999]
    # No known bodies were migrated, so sync_fn is never called.
    assert sync.calls == []
