#!/usr/bin/env python3
"""Ops precondition audit for trait swaps (#166).

  python scripts/audit_swap_preconditions.py --network mainnet

Asserts the NFT issuer (SWAP_ISSUER_ADDRESS) holds a trustline for the BRIX
pair the burn-remint replacement offers are priced in
(SWAP_OFFER_CURRENCY_HEX / SWAP_OFFER_ISSUER). Under XLS-20, an
NFTokenCreateOffer whose Amount is an IOU, on an NFT carrying a TransferFee,
requires the NFT's issuer to hold a trustline for that IOU (the royalty pays
out in it) — a missing trustline fails every BRIX-priced offer with
tecNO_LINE, discovered only AFTER the originals were burned before the #166
runtime precheck existed. This audit catches the misconfiguration before any
user does. Read-only: one account_lines lookup, no on-chain writes.

Exit codes (mirrors scripts/audit_trait_files.py's contract):
  0 = OK (trustline present, or NFT issuer == BRIX issuer so none is needed)
  1 = issuer is MISSING the trustline — remediate before enabling swaps
  2 = lookup failed / indeterminate (transient RPC problem; re-run)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, xrpl_ops  # noqa: E402


async def check() -> tuple[int, str]:
    """Return (exit_code, message) for the issuer BRIX-trustline precondition."""
    issuer = config.SWAP_ISSUER_ADDRESS
    currency = config.SWAP_OFFER_CURRENCY_HEX
    brix_issuer = config.SWAP_OFFER_ISSUER

    if issuer == brix_issuer:
        return 0, (
            f"OK: NFT issuer {issuer} IS the BRIX issuer — no cross-account "
            "trustline needed (an account implicitly holds its own IOU)."
        )

    try:
        balance = await xrpl_ops.get_trustline_balance(issuer, currency, brix_issuer)
    except Exception as e:  # transient RPC failure — indeterminate, not a fail
        return 2, f"INDETERMINATE: trustline lookup failed ({e}) — re-run."

    if balance is None:
        return 1, (
            f"FAIL: NFT issuer {issuer} holds NO trustline for BRIX "
            f"{currency}/{brix_issuer} — every BRIX-priced swap replacement "
            "offer will fail tecNO_LINE (see #166). Remediation: set a BRIX "
            "trustline on the NFT issuer via Xaman, then re-run this audit."
        )
    return 0, (
        f"OK: NFT issuer {issuer} holds the BRIX trustline "
        f"({currency}/{brix_issuer}, balance {balance})."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        choices=["testnet", "mainnet"],
        default=os.getenv("XRPL_NETWORK", "testnet"),
        help="informational — endpoints resolve from XRPL_NETWORK at import",
    )
    args = parser.parse_args()
    code, msg = asyncio.run(check())
    print(f"[{args.network}] {msg}")
    sys.exit(code)


if __name__ == "__main__":
    main()
