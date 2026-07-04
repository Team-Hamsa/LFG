#!/usr/bin/env python3
"""Snapshot BRIX and AMM LP token balances at a point in time.

  python scripts/snapshot_balances.py --network mainnet
  python scripts/snapshot_balances.py --network mainnet --amm-account rXXX --date 2026-07-04

Collects all account_lines balances for BRIX (from the issuer account) and
optionally AMM LP tokens (from the AMM account), then records them to the
history database with a snapshot date. Idempotent — re-running the same date
overwrites the previous snapshot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from xrpl.asyncio.clients import AsyncWebsocketClient  # noqa: E402
from xrpl.models.requests import Request  # noqa: E402

from lfg_core import config, history_store  # noqa: E402


async def collect_balances(
    request_fn: Any, brix_issuer: str, amm_account: str | None
) -> dict[str, dict]:
    """Collect BRIX and AMM LP balances from account_lines.

    Pages account_lines on the BRIX issuer (issuer-side balances are negated,
    so holder BRIX = -float(balance)), and if amm_account is set, also collects
    LP balances from the AMM account. Skips zero balances.

    Returns {holder: {"brix": x, "lp": y}}."""

    async def lines(account: str) -> list[dict]:
        """Page account_lines with marker pagination."""
        out: list[dict] = []
        marker: Any = None
        while True:
            req: dict[str, Any] = {
                "method": "account_lines",
                "account": account,
                "limit": 400,
            }
            if marker:
                req["marker"] = marker
            r = await request_fn(req)
            out.extend(r.get("lines", []))
            marker = r.get("marker")
            if not marker:
                return out

    balances: dict[str, dict] = {}

    # Collect BRIX balances
    for line in await lines(brix_issuer):
        v = -float(line.get("balance") or 0)
        if v:
            balances.setdefault(line["account"], {"brix": 0.0, "lp": 0.0})["brix"] = v

    # Collect AMM LP balances if amm_account is provided
    if amm_account:
        for line in await lines(amm_account):
            v = -float(line.get("balance") or 0)
            if v:
                balances.setdefault(line["account"], {"brix": 0.0, "lp": 0.0})["lp"] = v

    return balances


def write_snapshot(hconn: Any, balances: dict[str, dict], snap_date: str) -> int:
    """Write balance snapshot to the history database.

    Uses upsert_snapshot to insert or update (snap_date, account) rows.
    Returns the count of holders recorded."""
    for account, b in balances.items():
        history_store.upsert_snapshot(hconn, snap_date, account, b["brix"], b["lp"])
    return len(balances)


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import backfill_onchain as bf

    parser = argparse.ArgumentParser(description="Snapshot BRIX and AMM LP balances.")
    parser.add_argument("--network", choices=sorted(bf.NETWORKS), default=config.XRPL_NETWORK)
    parser.add_argument(
        "--amm-account", help="AMM account address (overrides env BRIX_AMM_ACCOUNT)"
    )
    parser.add_argument("--date", help="Snapshot date YYYY-MM-DD (default: today in UTC)")
    args = parser.parse_args()

    net = bf.NETWORKS[args.network]
    clio = net["clio"]
    brix_issuer = config.SWAP_OFFER_ISSUER
    amm_account = args.amm_account or config.BRIX_AMM_ACCOUNT
    snap_date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn = history_store.init_history_db(history_store.history_db_path(args.network))

    async with AsyncWebsocketClient(clio) as client:

        async def request_fn(req: dict[str, Any]) -> dict[str, Any]:
            r = await client.request(Request.from_dict(req))
            if not r.is_successful():
                raise RuntimeError(f"{req['method']} failed: {r.result}")
            return r.result

        balances = await collect_balances(request_fn, brix_issuer, amm_account)
        count = write_snapshot(conn, balances, snap_date)
        print(f"[{args.network}] snapshot {snap_date}: {count} holders")

    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
