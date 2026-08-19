#!/usr/bin/env python3
"""Resolve BRIX claims left open by a crash (#48).

  python scripts/recover_brix_claims.py --network mainnet

A claim whose payment was submitted but whose outcome was never recorded sits
in `submitted` with its accruals still bound — deliberately, because the
payment may have landed and unbinding would let the holder claim the same BRIX
twice. This reconciles those claims against the chain.

The verdict is never a guess. A claim is confirmed only when its memo-tagged
payout is found in the distributor's account_tx, and failed only when the
payout is absent AND the validated ledger has passed the claim's
LastLedgerSequence — past that point the XRPL guarantees the transaction can
never validate. Anything else is left alone for the next run.

Also invoked once at service startup, so ordinary restarts self-heal.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import brix_drip, config, history_store  # noqa: E402


async def _amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default=config.XRPL_NETWORK, choices=["testnet", "mainnet"])
    args = ap.parse_args()

    if args.network != config.XRPL_NETWORK:
        # account_tx would be read from the wrong chain, where every payout is
        # absent — and absence past LastLedgerSequence is what marks a claim
        # FAILED and unbinds it. A split network could therefore unbind claims
        # that were really paid, letting the same BRIX be claimed twice.
        print(
            f"refusing to recover: --network {args.network} but XRPL_NETWORK is "
            f"{config.XRPL_NETWORK}; re-run with XRPL_NETWORK={args.network}."
        )
        return 2

    conn = history_store.init_history_db(history_store.history_db_path(args.network))
    brix_drip.ensure_schema(conn)

    outcomes = await brix_drip.recover_from_chain(conn)
    if not outcomes:
        print(f"[{args.network}] no claims resolved (nothing open, or still undecidable)")
        return 0
    for claim_id, state in sorted(outcomes.items()):
        print(f"[{args.network}] claim {claim_id} -> {state}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
