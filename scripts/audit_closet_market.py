#!/usr/bin/env python3
"""Nightly Closet Market audit (#443).

Offline checks (always):
  * every paid/mirrored fill recorded its funds (payment or escrow finish),
    its forward payment, and — if it had overshoot — the overshoot refund
  * every refunded fill recorded its refund
  * no owner has more units encumbered (open/matched asks + pre-move holder
    fills) than they hold
  * every live bid knows its escrow sequence
  * no fill has sat non-terminal for over an hour
  * no non-terminal fill has failed a payout (or mirror) 10+ times
With --onchain:
  * every live bid's escrow object exists with the recorded amount
  * the app wallet holds at least the BRIX it has received but not yet paid
    out (skipped when the app wallet IS the BRIX issuer, as on testnet)

Exit 0 clean, 1 drift. Usage:
  .venv/bin/python scripts/audit_closet_market.py --network mainnet [--onchain]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from decimal import Decimal
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import _economy_deps  # noqa: E402

from lfg_core import closet_market_flow, config, market_ops, xrpl_ops  # noqa: E402
from lfg_core import closet_market_store as cms  # noqa: E402

STUCK_SECONDS = 3600
PAYOUT_FAILING_ATTEMPTS = 10  # e.g. the counterparty removed their BRIX trust line


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def audit_rows(conn: sqlite3.Connection, now: int | None = None) -> list[str]:
    now = now if now is not None else int(time.time())
    problems: list[str] = []
    for f in _rows(conn, "SELECT * FROM closet_fills"):
        fid, state = f["id"], f["state"]
        if Decimal(f["fee_brix"]) > Decimal(f["price_brix"]):
            problems.append(f"fill {fid}: fee {f['fee_brix']} exceeds price {f['price_brix']}")
        if state in (cms.PAID, cms.MIRRORED):
            funded = (
                f["payment_tx_hash"]
                if f["funds_source"] == cms.FUNDS_PAYMENT
                else f["escrow_finish_hash"]
            )
            if not funded:
                problems.append(f"fill {fid}: {state} without recorded funds")
            if not f["forward_tx_hash"]:
                problems.append(f"fill {fid}: {state} without a forward payment")
            if Decimal(f["overshoot_brix"]) > 0 and not f["overshoot_tx_hash"]:
                problems.append(
                    f"fill {fid}: {state} but overshoot {f['overshoot_brix']} never refunded"
                )
        if state == cms.REFUNDED and not f["refund_tx_hash"]:
            problems.append(f"fill {fid}: refunded without a refund payment")
        if state not in cms.TERMINAL_FILL_STATES and int(f["attempts"]) >= PAYOUT_FAILING_ATTEMPTS:
            problems.append(
                f"fill {fid}: {state} payout failing after {f['attempts']} attempts ({f['error']})"
            )
        if state not in cms.TERMINAL_FILL_STATES and now - int(f["updated_ts"]) > STUCK_SECONDS:
            problems.append(f"fill {fid}: stuck in {state} for {now - int(f['updated_ts'])}s")
    for o in _rows(
        conn,
        "SELECT * FROM closet_orders WHERE side = 'bid' AND state IN ('open', 'matched', 'cancelling')",
    ):
        if o["escrow_owner_seq"] is None:
            problems.append(f"bid {o['id']}: {o['state']} without an escrow sequence")
    owners = {
        r[0]
        for r in conn.execute(
            "SELECT owner FROM closet_orders WHERE side = 'ask' AND state IN ('open', 'matched') "
            "UNION SELECT seller FROM closet_fills WHERE ask_order_id IS NULL AND state IN ('funds_pending', 'funded')"
        )
    }
    for owner in sorted(owners):
        for (slot, value), n in cms.encumbrance(conn, owner).items():
            held = cms.holding_count(conn, owner, slot, value)
            if n > held:
                problems.append(f"{owner}: {n} x {slot}={value} encumbered but only {held} held")
    return problems


def unforwarded_brix(conn: sqlite3.Connection) -> Decimal:
    """BRIX the app wallet has received for fills it has not yet paid out."""
    total = Decimal(0)
    for f in _rows(
        conn,
        "SELECT * FROM closet_fills WHERE state IN ('funded', 'asset_moved', 'indeterminate', 'refund_pending')",
    ):
        if not (f["payment_tx_hash"] or f["escrow_finish_hash"]):
            continue
        # Compute remaining obligation per fill: only count what hasn't been paid out yet.
        if f["state"] == cms.REFUNDED or f["state"] == cms.REFUND_PENDING:
            # Refund-phase fills: owed = refund_brix if set, else full fill_in_amount
            owed = Decimal(f["refund_brix"] or cms.fill_in_amount(f))
        elif f["state"] == cms.INDETERMINATE and f.get("pending_phase") == "refund":
            # Write-ahead intent in refund phase: same as refund_pending
            owed = Decimal(f["refund_brix"] or cms.fill_in_amount(f))
        else:
            # Other states: forward and overshoot legs paid separately
            forward_part = (
                Decimal(0)
                if f["forward_tx_hash"]
                else Decimal(f["price_brix"]) - Decimal(f["fee_brix"])
            )
            overshoot_part = (
                Decimal(0)
                if f["overshoot_tx_hash"] or Decimal(f["overshoot_brix"]) <= 0
                else Decimal(f["overshoot_brix"])
            )
            owed = forward_part + overshoot_part
        total += owed
    return total


async def audit_onchain(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    bids = _rows(
        conn,
        "SELECT o.* FROM closet_orders o WHERE o.side = 'bid' AND o.state IN ('open', 'matched', 'cancelling') "
        "AND o.escrow_owner_seq IS NOT NULL AND o.cancel_finish_hash IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM closet_fills f WHERE f.bid_order_id = o.id AND f.escrow_finish_hash IS NOT NULL)",
    )
    for o in bids:
        try:
            node = await xrpl_ops.get_escrow(o["owner"], o["escrow_owner_seq"])
        except Exception as exc:
            problems.append(f"bid {o['id']}: escrow lookup failed ({exc})")
            continue
        if node is None:
            problems.append(f"bid {o['id']}: {o['state']} but its escrow is gone")
        elif not closet_market_flow._amount_equals(
            node.get("Amount"), market_ops.brix_amount_dict(o["price_brix"])
        ):
            problems.append(
                f"bid {o['id']}: escrow amount {node.get('Amount')} != {o['price_brix']} BRIX"
            )
    if config.SIGNING_ACCOUNT != config.BRIX_ISSUER:
        owed = unforwarded_brix(conn)
        balance = await xrpl_ops.get_trustline_balance(
            config.SIGNING_ACCOUNT, config.BRIX_CURRENCY_HEX, config.BRIX_ISSUER
        )
        if balance is None:
            problems.append("app wallet BRIX balance unreadable")
        elif balance < owed:
            problems.append(f"app wallet holds {balance} BRIX but owes {cms.fmt_brix(owed)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--network", required=True, choices=["mainnet", "testnet"])
    parser.add_argument("--onchain", action="store_true")
    args = parser.parse_args(argv)
    conn = _economy_deps.open_index(args.network)
    try:
        problems = audit_rows(conn)
        if args.onchain:
            problems += asyncio.run(audit_onchain(conn))
    finally:
        conn.close()
    for p in problems:
        print(f"DRIFT {p}")
    print("PASS" if not problems else f"FAIL ({len(problems)} problems)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
