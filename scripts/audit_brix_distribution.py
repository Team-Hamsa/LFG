#!/usr/bin/env python3
"""Conservation audit for the BRIX daily drip (#48).

  python scripts/audit_brix_distribution.py --network mainnet

Cross-checks four invariants (spec §6): accruals bound to confirmed claims sum
to the confirmed claim total; that total matches the distributor's on-chain
BRIX debits derived as `kind='claim'`; no accrual is left bound to a failed
claim; and no epoch accrued more rows than there are live tokens.

Exit code is non-zero (1) on any FAIL. Run it after the first epoch and
whenever claim numbers look suspicious, before trusting them.
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import brix_drip, config, history_store, nft_index  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default=config.XRPL_NETWORK, choices=["testnet", "mainnet"])
    ap.add_argument("--distributor", default=config.BRIX_DISTRIBUTOR_ADDRESS)
    args = ap.parse_args()

    if not args.distributor:
        print("FAIL no distributor address (set BRIX_DISTRIBUTOR_ADDRESS or --distributor)")
        return 1

    hconn = history_store.init_history_db(history_store.history_db_path(args.network))
    brix_drip.ensure_schema(hconn)
    oconn = nft_index.init_db(nft_index.index_db_path(args.network))
    live = oconn.execute("SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0").fetchone()[0]

    results = brix_drip.audit_distribution(hconn, args.distributor, int(live))
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'} {r.name}: {r.detail}")

    failed = [r for r in results if not r.ok]
    print(f"{'FAIL' if failed else 'PASS'} brix distribution audit ({len(failed)} failing checks)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
