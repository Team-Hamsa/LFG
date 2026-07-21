#!/usr/bin/env python3
"""Migrate legacy `closet_bodies` rows (edition ints) to `("Body", value)`
rows in `closet_assets`.

*** RUN THIS BEFORE THE NEW BODY-AS-ASSET CODE SERVES TRAFFIC. ***
The Harvest/Assemble/Equip/Extract/Deposit flows now wipe `closet_bodies` on
every economy op (`economy_store.set_closet_contents` no longer preserves
legacy body rows — a Closet's bodies are Body-slot assets now). Any owner
whose legacy body edition hasn't been converted by this script before their
next economy op will silently lose that body from their Closet. Run this
script for every network BEFORE deploying/serving the new code.

For each owner with rows in `closet_bodies`:
  1. Resolve each edition's `(body_value, body_class)` via the frozen genesis
     (`trait_economy.effective_genesis`).
  2. Known editions become a `("Body", value, 1)` row merged into the owner's
     existing `closet_assets`.
  3. Unknown editions (not present in the effective genesis — should not
     normally happen) are logged as a warning and LEFT IN `closet_bodies`
     untouched; they are never silently dropped.
  4. Chain-first: the Closet NFToken is synced (new merged assets, bodies=[])
     via `sync_fn` BEFORE the DB rows are rewritten. Only after a successful
     sync does `economy_store.set_closet_contents` persist the new mirror.

Usage:
  python scripts/migrate_closet_bodies_to_values.py --network testnet [--owner rXXX]

Idempotent: an owner with no `closet_bodies` rows (nothing left to migrate,
or a previous run already converted everything) is a no-op — `sync_fn` is
never called for that owner.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import _economy_deps as deps  # noqa: E402

from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config, trait_economy  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402

logger = logging.getLogger(__name__)

# Type alias for the sync injectable (tests replace this with a fake recorder;
# production wires a closure over ct.sync_closet + es.set_closet_contents).
SyncFn = Callable[[sqlite3.Connection, str, list[ct.Asset], list[int]], Awaitable[None]]


async def migrate_owner(
    conn: sqlite3.Connection,
    owner: str,
    sync_fn: SyncFn,
) -> dict[str, Any]:
    """Migrate one owner's legacy `closet_bodies` rows to Body-slot assets.

    Returns a summary dict:
      - skipped=True + reason           (nothing to migrate)
      - skipped=False + converted_count + unknown_editions + asset_count
    """
    all_bodies = es.read_closet_bodies(conn)
    owner_bodies = [edition for o, edition in all_bodies if o == owner]

    if not owner_bodies:
        return {"owner": owner, "skipped": True, "reason": "no legacy closet_bodies rows"}

    genesis = trait_economy.effective_genesis(es.read_genesis(conn), es.read_supply_changes(conn))

    known_bodies: list[tuple[str, str]] = []
    unknown_editions: list[int] = []
    for edition in owner_bodies:
        rec = genesis.edition_bodies.get(edition)
        if rec is None:
            logger.warning(
                "owner %s: edition %s not found in effective genesis — leaving in "
                "closet_bodies, not migrated",
                owner,
                edition,
            )
            unknown_editions.append(edition)
        else:
            body_value, _body_class = rec
            known_bodies.append((body_value, _body_class))

    if not known_bodies:
        return {
            "owner": owner,
            "skipped": True,
            "reason": "only unknown editions — nothing to convert",
            "unknown_editions": unknown_editions,
        }

    all_assets = es.read_closet_assets(conn)
    owner_assets: list[ct.Asset] = [
        (slot, value, count) for o, slot, value, count in all_assets if o == owner
    ]

    merged: dict[tuple[str, str], int] = {
        (slot, value): count for slot, value, count in owner_assets
    }
    for body_value, _body_class in known_bodies:
        key = ("Body", body_value)
        merged[key] = merged.get(key, 0) + 1

    new_assets: list[ct.Asset] = [(slot, value, count) for (slot, value), count in merged.items()]

    # Chain-first: sync the token with the merged contents (bodies=[]) BEFORE
    # rewriting the DB rows.
    await sync_fn(conn, owner, new_assets, [])

    # Now persist the mirror: new merged assets, remaining (unknown) bodies only.
    es.set_closet_contents(conn, owner, new_assets, unknown_editions)

    return {
        "owner": owner,
        "skipped": False,
        "converted_count": len(known_bodies),
        "unknown_editions": unknown_editions,
        "asset_count": len(new_assets),
    }


async def _real_sync_fn(
    conn: sqlite3.Connection,
    owner: str,
    assets: list[ct.Asset],
    bodies: list[int],
    economy_deps: Any,
) -> None:
    await ct.sync_closet(
        conn,
        owner,
        assets,
        bodies,
        upload_fn=economy_deps.closet_upload_fn,
        modify_fn=economy_deps.closet_modify_fn,
    )


async def _amain(args: argparse.Namespace) -> int:
    conn = deps.open_index(args.network)
    economy_deps = deps.build_economy_deps(conn)

    async def sync_fn(
        conn: sqlite3.Connection, owner: str, assets: list[ct.Asset], bodies: list[int]
    ) -> None:
        await _real_sync_fn(conn, owner, assets, bodies, economy_deps)

    if args.owner:
        owners = [args.owner]
    else:
        rows = conn.execute("SELECT DISTINCT owner FROM closet_bodies").fetchall()
        owners = [r[0] for r in rows]

    if not owners:
        print("No owners with legacy closet_bodies rows found.")
        return 0

    exit_code = 0
    for owner in owners:
        result = await migrate_owner(conn, owner, sync_fn)
        if result.get("skipped"):
            print(f"SKIP  {owner}: {result.get('reason', '')}")
            unknown = result.get("unknown_editions") or []
            if unknown:
                exit_code = 1
        else:
            unknown = result.get("unknown_editions") or []
            print(
                f"DONE  {owner}: converted {result['converted_count']} body edition(s), "
                f"{result['asset_count']} total assets"
                + (f", unknown editions left: {unknown}" if unknown else "")
            )
            if unknown:
                exit_code = 1

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate legacy closet_bodies (edition ints) to Body-slot "
            "closet_assets rows. RUN BEFORE the new code serves traffic."
        )
    )
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument(
        "--owner",
        default=None,
        help="Migrate a single owner's Closet (default: all owners with legacy bodies).",
    )
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
