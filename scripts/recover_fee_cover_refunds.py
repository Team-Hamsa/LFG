#!/usr/bin/env python3
"""Resolve fee-cover refunds left `submitted` by a crash, or requeue a parked failure.

  .venv/bin/python scripts/recover_fee_cover_refunds.py --network mainnet
  .venv/bin/python scripts/recover_fee_cover_refunds.py --network mainnet --requeue <accept_tx_hash>

Recovery never guesses: a refund is confirmed only when its memo-tagged Payment
is found in the signing account's account_tx, and failed only when it is absent
AND the validated ledger has passed its LastLedgerSequence. Also runs at service
startup, so ordinary restarts self-heal.

--requeue moves a `failed` refund back to `owed` (the sweep then pays it) after
the operator fixed the cause — e.g. topped up the issuer. A failed payment can
never validate later, so this cannot double-pay; it is refused when the
campaign budget no longer has room.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, db_path, fee_cover_settle, fee_cover_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover or requeue marketplace fee-cover refunds."
    )
    parser.add_argument("--network", required=True, choices=("testnet", "mainnet"))
    parser.add_argument("--requeue", metavar="ACCEPT_TX_HASH")
    args = parser.parse_args(argv)
    if args.network != config.XRPL_NETWORK:
        print(
            f"refusing: --network {args.network} does not match XRPL_NETWORK={config.XRPL_NETWORK}",
            file=sys.stderr,
        )
        return 2
    if args.requeue:
        conn = fee_cover_store.connect(db_path.app_db_path(args.network))
        try:
            result = fee_cover_store.requeue_failed(conn, args.requeue)
        finally:
            conn.close()
        print(f"{args.requeue}: {result}")
        return 0 if result == "requeued" else 1
    deps = fee_cover_settle.service_deps(args.network, linked=lambda a, b: False)
    outcomes = asyncio.run(fee_cover_settle.recover_refunds(deps))
    for key, state in sorted(outcomes.items()):
        print(f"{key}: {state}")
    print(f"resolved {len(outcomes)} refund(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
