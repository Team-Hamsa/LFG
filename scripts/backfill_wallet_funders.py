#!/usr/bin/env python3
"""Backfill wallet_funders for free-mint claimants (#461) and tagged signers (#490).

The all-time funder gate joins free_mint_claims against wallet_funders, so
claimant wallets admitted before the gate shipped are invisible to dedup
until their funders are recorded. Run once per network after deploy (and the
readiness audit's funder_coverage check fails until it has been run):

  .venv/bin/python scripts/backfill_wallet_funders.py --network mainnet
  .venv/bin/python scripts/backfill_wallet_funders.py --network mainnet \
      --seed-cache reports/funder_cache.json   # optional: reuse the ops cache
  .venv/bin/python scripts/backfill_wallet_funders.py --network mainnet \
      --from-history                           # also cover every tagged signer

Idempotent and resumable: wallets already in wallet_funders are skipped, and
every lookup commits immediately, so Ctrl-C and re-run is always safe. A
failed lookup is reported and left missing (re-run to retry).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, ".")

from lfg_core import config, funding, history_store  # noqa: E402
from lfg_core.db_path import app_db_path  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default=config.XRPL_NETWORK)
    ap.add_argument("--app-db", default=None)
    ap.add_argument(
        "--seed-cache",
        default=None,
        help="optional free_mint_clusters funder_cache.json to seed from (no RPC for hits)",
    )
    ap.add_argument(
        "--from-history",
        nargs="?",
        const="",
        default=None,
        help=(
            "also cover every wallet that has signed a SourceTag-carrying tx in the "
            "history archive (#490 — the published unique_actors count dedups those, "
            "not just free-mint claimants). Optional path; defaults to the network's "
            "history DB."
        ),
    )
    a = ap.parse_args()
    if a.network != config.XRPL_NETWORK:
        print(f"--network {a.network} must match XRPL_NETWORK={config.XRPL_NETWORK}")
        return 2

    seed: dict[str, tuple[str | None, int | None]] = {}
    if a.seed_cache:
        for wallet, value in json.load(open(a.seed_cache)).items():
            funder, ledger = (
                (value + [None, None])[:2] if isinstance(value, list) else (value, None)
            )
            # The clusters cache stores pseudo-funders ("rpc-error:…",
            # "<TxType>:<acct>") for failed/non-payment lookups; skip those.
            if funder is None or (funder.startswith("r") and ":" not in funder):
                seed[wallet] = (funder, ledger)

    conn = sqlite3.connect(a.app_db or app_db_path(a.network))
    funding.ensure_schema(conn)
    # A campaign may never have run on this network (no free_mint_claims
    # table yet) — that is not an error, there are simply no claimants.
    has_claims = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='free_mint_claims'"
    ).fetchone()
    wallets = (
        [
            r[0]
            for r in conn.execute(
                """
                SELECT DISTINCT c.wallet FROM free_mint_claims c
                LEFT JOIN wallet_funders f ON f.wallet = c.wallet
                WHERE c.network = ? AND f.wallet IS NULL
                """,
                (a.network,),
            )
        ]
        if has_claims
        else []
    )
    print(f"{len(wallets)} claimant wallet(s) missing funder rows")

    if a.from_history is not None:
        history_path = a.from_history or history_store.history_db_path(a.network)
        if not os.path.exists(history_path):
            conn.close()
            print(f"history DB not found: {history_path}", file=sys.stderr)
            return 2
        # A corrupt or incompatible history file is an operator-facing
        # failure, not a traceback (CodeRabbit on #490).
        try:
            hist = sqlite3.connect(history_path)
            try:
                tagged = [
                    r[0]
                    for r in hist.execute(
                        "SELECT DISTINCT account FROM xrpl_txs"
                        " WHERE source_tag = ? AND account IS NOT NULL",
                        (config.SOURCE_TAG,),
                    )
                ]
            finally:
                hist.close()
        except sqlite3.Error as exc:
            conn.close()
            print(f"failed to read history DB {history_path}: {exc}", file=sys.stderr)
            return 2
        known = set(wallets)
        extra = []
        for wallet in tagged:
            if wallet in known or funding.has_cached_funder(conn, wallet):
                continue
            known.add(wallet)
            extra.append(wallet)
        print(f"{len(extra)} tagged signer(s) missing funder rows")
        wallets += extra
    failed = 0
    for i, wallet in enumerate(wallets, 1):
        if wallet in seed:
            funder, ledger = seed[wallet]
        else:
            try:
                funder, ledger = funding.lookup_funder(wallet)
            except funding.FunderLookupError as e:
                print(f"  [{i}/{len(wallets)}] {wallet}: lookup failed ({e}); re-run to retry")
                failed += 1
                continue
        funding.record_funder(conn, wallet, funder, ledger)
        conn.commit()
        label = funding.EXCHANGES.get(funder or "", "")
        print(f"  [{i}/{len(wallets)}] {wallet} <- {funder or '(none)'} {label}")
    conn.close()
    print(f"done; {failed} failed lookup(s) remain" if failed else "done; full coverage")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
