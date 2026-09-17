#!/usr/bin/env python3
"""Resolve fee-cover refunds left `submitted` by a crash, or requeue a parked failure.

  .venv/bin/python scripts/recover_fee_cover_refunds.py --network mainnet
  .venv/bin/python scripts/recover_fee_cover_refunds.py --network mainnet --requeue <accept_tx_hash>

Recovery never guesses: a refund is confirmed only when its memo-tagged Payment
is found in the signing account's account_tx, and failed only when it is absent
AND the validated ledger has passed its LastLedgerSequence. Also runs at service
startup, so ordinary restarts self-heal.

--requeue moves a `failed` refund (reason payout_failed or payout_expired) back
to `owed` (the sweep then pays it) after the operator fixed the cause — e.g.
topped up the issuer. It first re-checks the chain for the memo-tagged payout:
found, or the lookup errors, and it refuses (exit 1) without requeueing, so a
payout that did land is never paid twice. It is also refused when the campaign
budget no longer has room. A requeue that goes through writes a
`fee_cover_audit` row (action `requeue`) with actor `cli:<os-login>`: the
account owning the process, from the real UID, so `$USER` cannot forge it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pwd
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, db_path, fee_cover_settle, fee_cover_store, xrpl_ops  # noqa: E402


def _audit_actor() -> str:
    """`cli:<os-login>`, the same convention as scripts/sponsored_mint_admin.py."""
    return f"cli:{pwd.getpwuid(os.getuid()).pw_name}"


def _requeue(network: str, accept_tx_hash: str) -> int:
    conn = fee_cover_store.connect(db_path.app_db_path(network))
    try:
        row = fee_cover_store.get_refund(conn, accept_tx_hash)
        if row is not None and row["state"] == "failed":
            # A `payout_expired` verdict rests on an account_tx read; never
            # requeue on it alone — re-check that the payout is really absent.
            try:
                found = asyncio.run(
                    xrpl_ops.find_fee_cover_payment(
                        accept_tx_hash,
                        destination=str(row["bidder"]),
                        drops=int(row["refund_drops"]),
                        # From the claim ledger, never deadline-minus-margin:
                        # see fee_cover_settle.recover_refunds.
                        min_ledger=row["claim_ledger"],
                    )
                )
            except Exception as exc:
                print(
                    f"{accept_tx_hash}: on-ledger check failed, not requeued: {exc}",
                    file=sys.stderr,
                )
                return 1
            if found:
                print(f"{found} found on-ledger; not requeued")
                return 1
        result = fee_cover_store.requeue_failed(conn, accept_tx_hash, actor=_audit_actor())
    finally:
        conn.close()
    print(f"{accept_tx_hash}: {result}")
    return 0 if result == "requeued" else 1


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
        return _requeue(args.network, args.requeue)
    deps = fee_cover_settle.service_deps(args.network, linked=lambda a, b: False)
    outcomes = asyncio.run(fee_cover_settle.recover_refunds(deps))
    for key, state in sorted(outcomes.items()):
        print(f"{key}: {state}")
    print(f"resolved {len(outcomes)} refund(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
