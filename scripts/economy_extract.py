#!/usr/bin/env python3
"""Extract a loose Closet trait into a standalone tradeable NFToken.

  python scripts/economy_extract.py --network testnet --owner rUSER --slot Hat --value "Wizard Hat"

Headless Phase-4 driver. Operations carry SourceTag. Supply-neutral (no
supply_changes written — asset_census already tallies trait_tokens).
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
    session = economy_flow.ExtractSession(
        owner=args.owner,
        slot=args.slot,
        value=args.value,
    )
    await economy_flow.run_extract(session, deps.build_economy_deps(conn))
    print(f"State: {session.state}")
    if session.error:
        print(f"Error: {session.error}")
    if session.accept:
        print(f"Accept your trait: {session.accept.get('xumm_url')}")
    return 0 if session.state == economy_flow.DONE else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract a loose Closet trait into a standalone tradeable NFToken."
    )
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--owner", required=True, help="owner's XRPL address")
    parser.add_argument("--slot", required=True, help="trait slot (e.g. Hat, Eyes)")
    parser.add_argument("--value", required=True, help="trait value (e.g. 'Wizard Hat')")
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
