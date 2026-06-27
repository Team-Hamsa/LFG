#!/usr/bin/env python3
"""Deposit a standalone trait NFToken back into the owner's Closet (burn -> Closet credit).

  python scripts/economy_deposit.py --network testnet --owner rUSER --nft-id 00...

Headless Phase-4 driver. Fail-closed: the burn is irreversible, so ownership is
verified on-ledger before burning. Supply-neutral (no supply_changes written).
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
    session = economy_flow.DepositSession(
        owner=args.owner,
        nft_id=args.nft_id,
    )
    await economy_flow.run_deposit(session, deps.build_economy_deps(conn))
    print(f"State: {session.state}")
    if session.error:
        print(f"Error: {session.error}")
    if session.state == economy_flow.DONE:
        print(f"Slot: {session.slot}")
        print(f"Value: {session.value}")
    return 0 if session.state == economy_flow.DONE else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deposit a standalone trait NFToken back into the owner's Closet."
    )
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--owner", required=True, help="owner's XRPL address")
    parser.add_argument("--nft-id", required=True, help="trait NFTokenID to deposit")
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
