#!/usr/bin/env python3
"""Assemble: dress a caller-owned BLANK edition in place with a body + a full
asset set from the Closet (NFTokenModify — no mint/offer/accept).

  python scripts/economy_assemble.py --network testnet --owner rUSER --edition 42 \\
      --set Background=Blue --set Back=None --set Clothing=Hoodie ... (all 8 slots)

The target edition must already be a live blank (harvest it first). The body is
taken from the (effective) genesis ledger. Operations are free.
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
    character = nft_index.nft_by_number(conn, args.edition)
    if character is None:
        print(f"Edition {args.edition} not found in the on-chain index.")
        return 2
    if character.owner != args.owner:
        print(f"Edition {args.edition} is not owned by {args.owner}.")
        return 2
    session = economy_flow.AssembleSession(
        owner=args.owner,
        character=character,
        chosen=_parse_set(args.set),
        body_value=body[0],
        body_class=body[1],
    )
    if not trait_economy.is_blank(character):
        print(f"Edition {args.edition} is not blank — harvest it first.")
        return 2
    await economy_flow.run_assemble(session, deps.build_economy_deps(conn))
    print(f"State: {session.state}")
    if session.error:
        print(f"Error: {session.error}")
    # Assemble dresses the blank in place (NFTokenModify) — no new mint/offer.
    for r in session.results:
        print(f"Dressed {r['nft_id']} in place — image: {r.get('image_url')}")
    return 0 if session.state == economy_flow.DONE else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble an edition from the Closet.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.ECONOMY_NETWORK)
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
