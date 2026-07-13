#!/usr/bin/env python3
# Loopback ops CLI for the newcomer free-mint claim ledger. Not wired into any
# surface. See docs/superpowers/specs/2026-07-13-free-mint-newcomers-design.md.

import argparse
import sqlite3
from typing import Any

from lfg_core import free_mint
from lfg_core.user_db import DATABASE


def list_claims(network: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "SELECT platform, platform_user_id, network, wallet, nft_number, "
                "status, claimed_at FROM free_mint_claims WHERE network = ? "
                "ORDER BY claimed_at DESC",
                (network,),
            )
        ]
    finally:
        conn.close()


def revoke(platform: str, uid: str, network: str) -> None:
    """Delete any claim (reserved OR claimed) so the identity can claim again."""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            "DELETE FROM free_mint_claims WHERE platform=? AND platform_user_id=? AND network=?",
            (platform, uid, network),
        )
        conn.commit()
    finally:
        conn.close()


def grant(platform: str, uid: str, network: str, wallet: str) -> None:
    """Pre-authorize a claim, bypassing the eligibility scan. Idempotent."""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            "INSERT INTO free_mint_claims "
            "(platform, platform_user_id, network, wallet, status) "
            "VALUES (?, ?, ?, ?, 'claimed') "
            "ON CONFLICT(platform, platform_user_id, network) "
            "DO UPDATE SET status='claimed', wallet=excluded.wallet",
            (platform, uid, network, wallet),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    free_mint.ensure_tables()
    ap = argparse.ArgumentParser(description="Free-mint claim admin")
    ap.add_argument("--network", default="mainnet")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("platform")
    r.add_argument("uid")
    g = sub.add_parser("grant")
    g.add_argument("platform")
    g.add_argument("uid")
    g.add_argument("wallet")
    args = ap.parse_args()
    if args.cmd == "list":
        for row in list_claims(args.network):
            print(row)
    elif args.cmd == "revoke":
        revoke(args.platform, args.uid, args.network)
        print(f"revoked {args.platform}/{args.uid} on {args.network}")
    elif args.cmd == "grant":
        grant(args.platform, args.uid, args.network, args.wallet)
        print(f"granted {args.platform}/{args.uid} -> {args.wallet} on {args.network}")


if __name__ == "__main__":
    main()
