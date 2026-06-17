"""Stand up (idempotently) an XRP/BRIX AMM pool on XRPL testnet for swap testing.

Issue: https://github.com/Team-Hamsa/LFG/issues/26
Recreate after a testnet reset with:  .venv/bin/python scripts/testnet_amm_setup.py

Signed by the SEED account, which on testnet is the BRIX issuer. Safe to re-run:
skips Default-Ripple if already set and skips AMMCreate if the pool already exists.
"""

from __future__ import annotations

from decimal import Decimal

LSF_DEFAULT_RIPPLE = 0x00800000

# Pool parameters (see docs/superpowers/specs/2026-06-17-testnet-brix-amm-design.md)
XRP_AMOUNT_DROPS = "50000000"  # 50 XRP
BRIX_AMOUNT = "5000"
TRADING_FEE = 500  # 0.5% (units of 1/100000)
SWAP_TEST_BRIX = "10"  # 10 BRIX -> exercises the trait-swap fee path


def default_ripple_enabled(flags: int) -> bool:
    """True iff the account's lsfDefaultRipple flag bit is set."""
    return bool(flags & LSF_DEFAULT_RIPPLE)


def amm_create_fee_drops(reserve_inc_xrp: float) -> str:
    """AMMCreate special fee (in drops) = the network's incremental owner reserve.

    AMMCreate must destroy at least one incremental owner reserve; autofill does
    not set this, so it is passed explicitly as the transaction ``fee``.
    """
    return str(int(Decimal(str(reserve_inc_xrp)) * 1_000_000))


def format_pool_summary(
    amm_account: str, xrp_amount: str, brix_amount: str, trading_fee: int
) -> str:
    """Human-readable summary block for the console and CLAUDE.md."""
    xrp = Decimal(xrp_amount) / 1_000_000
    fee_pct = Decimal(trading_fee) / 1000  # 500 -> 0.5
    return (
        "=== Testnet XRP/BRIX AMM ===\n"
        f"AMM account (pool ID): {amm_account}\n"
        f"Pair: {xrp} XRP : {brix_amount} BRIX\n"
        f"Price: {(xrp / Decimal(brix_amount)).normalize()} XRP/BRIX\n"
        f"Trading fee: {fee_pct}%"
    )
