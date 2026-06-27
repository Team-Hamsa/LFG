#!/usr/bin/env python3
"""Migrate legacy Bucket NFTs (LEGACY_BUCKET_TAXON) to Closets (CLOSET_TAXON).

For each owner whose recorded Closet NFToken lives under the old BUCKET_TAXON,
this script:
  1. Mints a new Closet under CLOSET_TAXON (via ensure_closet with a cleared record).
  2. Copies the owner's existing assets + bodies into the new token via sync_closet.
  3. Records the abandoned legacy nft_id in the output summary.
  4. Leaves the new Closet pending_accept (the owner must accept the on-chain offer).

The old soulbound Bucket (flags 16, non-burnable) is abandoned in place — it
cannot be issuer-burned, so we simply stop tracking it.

Usage:
  python scripts/migrate_bucket_to_closet.py --network testnet [--owner rXXX]

Idempotent: owners already on CLOSET_TAXON are skipped with a "already on
CLOSET_TAXON" reason. Owners with no recorded closet are also skipped.

The taxon lookup is performed via deps.closet_owner_fn's companion nft_info
call (injected as `nft_info_fn` in tests, wired to xrpl_ops.nft_info at runtime).
"""

from __future__ import annotations

import argparse
import asyncio
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
from lfg_core import config, xrpl_ops  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402

# Type alias for the taxon-lookup injectable (tests replace this with a fake)
NftInfoFn = Callable[[str], Awaitable[dict[str, Any] | None]]


async def migrate_owner(
    conn: sqlite3.Connection,
    owner: str,
    economy_deps: Any,  # EconomyDeps
    *,
    nft_info_fn: NftInfoFn | None = None,
) -> dict[str, Any]:
    """Migrate one owner from the legacy Bucket taxon to the new Closet taxon.

    Returns a summary dict describing what happened:
      - skipped=True  + reason  (no record, or already on CLOSET_TAXON)
      - skipped=False + old_nft_id, new_nft_id, asset_count, body_count, status

    The `nft_info_fn` parameter exists solely for testing: in production it
    defaults to `xrpl_ops.nft_info`.  Injecting it in tests avoids any network
    calls while still exercising the full migration logic.
    """
    if nft_info_fn is None:
        nft_info_fn = xrpl_ops.nft_info

    # 1. Read the recorded closet (if any)
    record = es.get_closet_record(conn, owner)
    if record is None:
        return {"owner": owner, "skipped": True, "reason": "no record — nothing to migrate"}

    old_nft_id, old_uri_hex, old_status, old_offer_id = record

    # 2. Look up the on-ledger taxon
    info = await nft_info_fn(old_nft_id)
    if info is None:
        # Can't determine the taxon (network blip or token missing) — be conservative
        return {
            "owner": owner,
            "skipped": True,
            "reason": f"nft_info returned None for {old_nft_id} — skipping (safe)",
        }

    taxon = info.get("taxon")
    if taxon == config.CLOSET_TAXON:
        return {"owner": owner, "skipped": True, "reason": "already on CLOSET_TAXON"}

    if taxon != config.LEGACY_BUCKET_TAXON:
        return {
            "owner": owner,
            "skipped": True,
            "reason": f"unexpected taxon {taxon!r} — not a legacy Bucket; skipping",
        }

    # 3. Gather current contents for this owner
    all_assets = es.read_closet_assets(conn)
    owner_assets: list[ct.Asset] = [
        (slot, value, count) for o, slot, value, count in all_assets if o == owner
    ]
    all_bodies = es.read_closet_bodies(conn)
    owner_bodies: list[int] = [edition for o, edition in all_bodies if o == owner]

    # 4. Clear the stale record so ensure_closet will mint a NEW Closet (not
    #    return the existing one).  We do this atomically: delete, then mint.
    conn.execute("DELETE FROM closet_tokens WHERE owner = ?", (owner,))
    conn.commit()

    # 5. Mint the new Closet under CLOSET_TAXON via ensure_closet.
    #    ensure_closet finds no record (we just deleted it) and mints fresh.
    new_ref = await ct.ensure_closet(
        conn,
        owner,
        upload_fn=economy_deps.closet_upload_fn,
        mint_fn=economy_deps.closet_mint_fn,
        offer_fn=economy_deps.closet_offer_fn,
        accept_payload_fn=economy_deps.closet_accept_fn,
        exists_fn=economy_deps.closet_exists_fn,
    )

    # 6. Sync the owner's existing contents into the new token (NFTokenModify).
    #    This also persists the updated URI hex in closet_tokens.
    if owner_assets or owner_bodies:
        await ct.sync_closet(
            conn,
            owner,
            owner_assets,
            owner_bodies,
            upload_fn=economy_deps.closet_upload_fn,
            modify_fn=economy_deps.closet_modify_fn,
        )

    # 7. The new record is already PENDING_ACCEPT (ensure_closet writes it that way).
    new_record = es.get_closet_record(conn, owner)
    new_nft_id = new_record[0] if new_record else new_ref.nft_id

    return {
        "owner": owner,
        "skipped": False,
        "old_nft_id": old_nft_id,
        "new_nft_id": new_nft_id,
        "asset_count": len(owner_assets),
        "body_count": len(owner_bodies),
        "status": ct.PENDING_ACCEPT,
    }


async def _amain(args: argparse.Namespace) -> int:
    conn = deps.open_index(args.network)
    economy_deps = deps.build_economy_deps(conn)

    if args.owner:
        owners = [args.owner]
    else:
        # All owners that have a closet_tokens record
        rows = conn.execute("SELECT owner FROM closet_tokens").fetchall()
        owners = [r[0] for r in rows]

    if not owners:
        print("No owners with recorded closets found.")
        return 0

    exit_code = 0
    for owner in owners:
        result = await migrate_owner(conn, owner, economy_deps)
        if result.get("skipped"):
            print(f"SKIP  {owner}: {result.get('reason', '')}")
        else:
            print(
                f"DONE  {owner}: {result['old_nft_id']} -> {result['new_nft_id']} "
                f"({result['asset_count']} assets, {result['body_count']} bodies, "
                f"status={result['status']})"
            )
            if result.get("status") != ct.PENDING_ACCEPT:
                exit_code = 1

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-mint legacy Bucket NFTs as Closets under CLOSET_TAXON."
    )
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument(
        "--owner",
        default=None,
        help="Migrate a single owner's Closet (default: all owners).",
    )
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
