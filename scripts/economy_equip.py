#!/usr/bin/env python3
"""Equip loose Closet assets onto a live character; each displaced asset returns
to the Closet. All changes commit in ONE in-place NFTokenModify.

  python scripts/economy_equip.py --network testnet --owner rUSER \\
      --nft-id 00... --set Head=Crown --set Eyes=Laser

Operations are free. All txns carry SourceTag.
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

from lfg_core import config, economy_flow  # noqa: E402


async def _amain(args: argparse.Namespace) -> int:
    changes: list[tuple[str, str]] = []
    for pair in args.set:
        slot, sep, value = pair.partition("=")
        if not sep or not slot or not value:
            print(f"--set expects SLOT=VALUE, got {pair!r}")
            return 2
        changes.append((slot, value))
    if args.slot and args.value:
        changes.append((args.slot, args.value))
    if not changes:
        print("nothing to do: pass --set SLOT=VALUE (repeatable) or --slot/--value")
        return 2

    conn = deps.open_index(args.network)
    rec = deps.load_index_character(conn, args.nft_id)
    if rec is None:
        print(f"NFT {args.nft_id} not found in the {args.network} index.")
        return 2
    session = economy_flow.EquipSession(owner=args.owner, character=rec, changes=changes)
    await economy_flow.run_equip(session, deps.build_economy_deps(conn))
    print(f"State: {session.state}")
    if session.error:
        print(f"Error: {session.error}")
    if session.state == economy_flow.DONE:
        for slot, value in changes:
            print(f"Equipped {slot}={value}; {session.displaced[slot]} returned to the Closet.")
    return 0 if session.state == economy_flow.DONE else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Equip Closet assets onto a character.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.ECONOMY_NETWORK)
    parser.add_argument("--owner", required=True, help="owner's XRPL address")
    parser.add_argument("--nft-id", required=True, help="character NFTokenID to modify")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="SLOT=VALUE",
        help="repeatable; all changes commit in ONE NFTokenModify",
    )
    parser.add_argument("--slot", help="single-change form; non-body slot to change")
    parser.add_argument("--value", help="single-change form; incoming asset value")
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
