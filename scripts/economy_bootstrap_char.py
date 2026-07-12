#!/usr/bin/env python3
"""Bootstrap a burnable economy character on testnet for E2E testing.

Mints a fresh burnable+transferable+mutable character (ECONOMY_NFT_FLAGS) to the
issuer, copying a valid full attribute set from an existing edition in the index
so its trait layers are known-composable. The minted token deposits straight to
the issuer (no offer/accept), so the harvest/equip/assemble flows can drive it
headlessly with owner == issuer.

  python scripts/economy_bootstrap_char.py --network testnet \
      --edition 3557 --source-edition 3544

This is test scaffolding, NOT a production flow. It mints a minimal metadata JSON
(name + attributes + image) — the economy logic reads attributes, never the
image — so it skips the FFmpeg compose path (assemble exercises real compose).
All txns carry SourceTag via xrpl_ops.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import _economy_deps as deps  # noqa: E402

from lfg_core import cdn, config, swap_meta, xrpl_ops  # noqa: E402


def _source_attributes(conn: object, source_edition: int) -> list[dict[str, str]]:
    conn.row_factory = __import__("sqlite3").Row  # type: ignore[attr-defined]
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT attributes_json FROM onchain_nfts "
        "WHERE nft_number = ? AND is_burned = 0 AND attributes_json != '[]' LIMIT 1",
        (source_edition,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"source edition {source_edition} not found / has no attributes")
    return json.loads(row["attributes_json"])


async def _amain(args: argparse.Namespace) -> int:
    conn = deps.open_index(args.network)

    dupe = conn.execute(
        "SELECT nft_id FROM onchain_nfts WHERE nft_number = ? AND is_burned = 0",
        (args.edition,),
    ).fetchone()
    if dupe is not None:
        raise SystemExit(
            f"edition {args.edition} already live in the index ({dupe[0][:16]}..) — "
            "pick an unused edition number"
        )

    attrs = _source_attributes(conn, args.source_edition)
    season = swap_meta.season_for_number(args.edition)
    meta = {
        "schema": config.NFT_SCHEMA_URL,
        "name": f"{config.NFT_COLLECTION_NAME} #{args.edition}",
        "description": f"Season {season} (economy bootstrap)",
        "image": f"{config.EXTERNAL_WEBSITE_URL}/bootstrap_{args.edition}.png",
        "external_link": config.EXTERNAL_WEBSITE_URL,
        "collection": {"name": config.NFT_COLLECTION_NAME, "family": f"Season {season}"},
        "edition": args.edition,
        "attributes": attrs,
    }
    body_class = swap_meta.detect_body(attrs)
    body_value = swap_meta.get_attr(attrs, "Body") or ""
    print(
        f"Minting burnable edition {args.edition} (body={body_value}/{body_class}) "
        f"flags={config.ECONOMY_NFT_FLAGS} to issuer {config.SWAP_ISSUER_ADDRESS}"
    )

    path = f"bootstrap/{args.edition}_{uuid.uuid4().hex[:8]}.json"
    meta_url = await cdn.upload_to_bunny(
        config.ECONOMY_CDN_FOLDER, path, json.dumps(meta, indent=2).encode(), "application/json"
    )
    print(f"metadata: {meta_url}")

    nft_id = await xrpl_ops.mint_nft(
        meta_url, config.SWAP_TAXON, config.SWAP_ISSUER_ADDRESS, flags=config.ECONOMY_NFT_FLAGS
    )
    if not nft_id:
        print("MINT FAILED")
        return 1
    print(f"nft_id: {nft_id}")
    print("Minted. Wait for the listener to index it (poll onchain_nfts), then freeze genesis.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network", choices=["mainnet", "testnet"], default=config.ECONOMY_NETWORK)
    p.add_argument("--edition", type=int, required=True, help="new edition number to mint")
    p.add_argument(
        "--source-edition",
        type=int,
        required=True,
        help="existing edition whose attribute set to copy (for composable, valid traits)",
    )
    args = p.parse_args()
    if args.network != "testnet":
        raise SystemExit("bootstrap is testnet-only scaffolding")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
