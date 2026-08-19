#!/usr/bin/env python3
"""Operator view of the BRIX daily drip (#48).

  python scripts/brix_admin_report.py --network mainnet

Prints outstanding liability (what holders could claim right now) against the
distributor's actual BRIX balance, claims by state, total distributed, and the
top unclaimed wallets. The headroom line is the one to watch: if liability
outruns the distributor balance, claims start failing with tec errors and
users have to retry after a refund (spec §10).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import brix_drip, config, history_store, xrpl_ops  # noqa: E402


async def _amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default=config.XRPL_NETWORK, choices=["testnet", "mainnet"])
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    conn = history_store.init_history_db(history_store.history_db_path(args.network))
    brix_drip.ensure_schema(conn)

    liability = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_accruals WHERE claim_id IS NULL"
    ).fetchone()[0]
    distributed = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_claims WHERE state = 'confirmed'"
    ).fetchone()[0]
    last_epoch = brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) or "(never accrued)"

    print(f"[{args.network}] last accrued epoch: {last_epoch}")
    print(f"[{args.network}] outstanding liability: {liability} BRIX")
    print(f"[{args.network}] distributed to date:   {distributed} BRIX")

    if config.BRIX_DISTRIBUTOR_ADDRESS:
        balance = await xrpl_ops.get_trustline_balance(
            config.BRIX_DISTRIBUTOR_ADDRESS, config.BRIX_CURRENCY_HEX, config.BRIX_ISSUER
        )
        if balance is None:
            print("WARNING distributor has NO BRIX trustline — every claim will fail")
        else:
            headroom = float(balance) - float(liability)
            flag = "" if headroom >= 0 else "   <-- UNDERFUNDED"
            print(f"[{args.network}] distributor balance:  {balance} BRIX")
            print(f"[{args.network}] headroom:             {headroom:g} BRIX{flag}")

    print("\nclaims by state:")
    for state, count, total in conn.execute(
        "SELECT state, COUNT(*), COALESCE(SUM(amount), 0) FROM brix_claims"
        " GROUP BY state ORDER BY state"
    ):
        print(f"  {state:<10} {count:>5} claims  {total:>8} BRIX")

    print(f"\ntop {args.top} unclaimed wallets:")
    rows = conn.execute(
        "SELECT owner, SUM(amount) t FROM brix_accruals WHERE claim_id IS NULL"
        " GROUP BY owner ORDER BY t DESC LIMIT ?",
        (args.top,),
    ).fetchall()
    for owner, total in rows:
        print(f"  {owner:<40} {total:>8} BRIX")
    if not rows:
        print("  (none)")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
