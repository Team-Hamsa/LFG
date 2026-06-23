#!/usr/bin/env python3
"""Assemble (rebirth) an edition: body + a full asset set from the Bucket -> mint.

  python scripts/economy_assemble.py --network testnet --owner rUSER --edition 42 \\
      --set Background=Blue --set Back=None --set Clothing=Hoodie ... (all 8 slots)

The body is taken from the (effective) genesis ledger. Operations are free.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import _economy_deps as deps  # noqa: E402

from lfg_core import config, economy_flow, economy_store, nft_index, trait_economy  # noqa: E402


def _parse_set(pairs: list[str]) -> dict[str, str]:
    chosen: dict[str, str] = {}
    for pair in pairs or []:
        slot, _, value = pair.partition("=")
        chosen[slot.strip()] = value.strip()
    return chosen


async def _amain(args: argparse.Namespace) -> int:
    conn = deps.open_index(args.network)
    if not economy_store.genesis_exists(conn):
        print("No frozen genesis. Run scripts/freeze_genesis.py first.")
        return 2
    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
    )
    body = genesis.edition_bodies.get(args.edition)
    if body is None:
        print(f"Edition {args.edition} has no known body in genesis.")
        return 2
    live_editions = {r.nft_number for r in nft_index.live_nfts(conn) if r.nft_number is not None}
    session = economy_flow.AssembleSession(
        owner=args.owner,
        edition=args.edition,
        chosen=_parse_set(args.set),
        body_value=body[0],
        body_class=body[1],
        live_editions=live_editions,
    )
    await economy_flow.run_assemble(session, deps.build_economy_deps(conn))
    print(f"State: {session.state}")
    if session.error:
        print(f"Error: {session.error}")
    for r in session.results:
        accept = (r.get("accept") or {}).get("xumm_url")
        print(f"Minted {r['nft_id']} — accept: {accept}")
    return 0 if session.state == economy_flow.DONE else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble an edition from the Bucket.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--owner", required=True, help="owner's XRPL address")
    parser.add_argument("--edition", required=True, type=int, help="edition number to rebirth")
    parser.add_argument(
        "--set",
        action="append",
        metavar="Slot=Value",
        help="one per non-body slot (repeatable)",
    )
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
