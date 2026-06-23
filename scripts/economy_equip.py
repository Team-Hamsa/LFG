#!/usr/bin/env python3
"""Equip a loose Bucket asset onto a live character; the displaced asset returns
to the Bucket (in-place NFTokenModify).

  python scripts/economy_equip.py --network testnet --owner rUSER \\
      --nft-id 00... --slot Head --value Crown

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
    conn = deps.open_index(args.network)
    rec = deps.load_index_character(conn, args.nft_id)
    if rec is None:
        print(f"NFT {args.nft_id} not found in the {args.network} index.")
        return 2
    session = economy_flow.EquipSession(
        owner=args.owner, character=rec, slot=args.slot, incoming_value=args.value
    )
    await economy_flow.run_equip(session, deps.build_economy_deps(conn))
    print(f"State: {session.state}")
    if session.error:
        print(f"Error: {session.error}")
    if session.state == economy_flow.DONE:
        print(
            f"Equipped {args.slot}={args.value}; {session.displaced_value} returned to the Bucket."
        )
    return 0 if session.state == economy_flow.DONE else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Equip a Bucket asset onto a character.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--owner", required=True, help="owner's XRPL address")
    parser.add_argument("--nft-id", required=True, help="character NFTokenID to modify")
    parser.add_argument("--slot", required=True, help="non-body slot to change")
    parser.add_argument(
        "--value", required=True, help="incoming asset value (must be in the Bucket)"
    )
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
