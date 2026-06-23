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


import asyncio  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xrpl.asyncio.clients import AsyncJsonRpcClient  # noqa: E402
from xrpl.asyncio.transaction import submit_and_wait  # noqa: E402
from xrpl.models.amounts import IssuedCurrencyAmount  # noqa: E402
from xrpl.models.currencies import XRP, IssuedCurrency  # noqa: E402
from xrpl.models.requests import AccountInfo, AMMInfo, ServerInfo  # noqa: E402
from xrpl.models.response import Response  # noqa: E402
from xrpl.models.transactions import (  # noqa: E402
    AccountSet,
    AccountSetAsfFlag,
    AMMCreate,
)
from xrpl.wallet import Wallet  # noqa: E402

from lfg_core import config, xrpl_ops  # noqa: E402


def _tx_result(response: Response) -> str:
    """Pull the engine result string out of a submit_and_wait response."""
    return response.result["meta"]["TransactionResult"]  # type: ignore[no-any-return]


async def main() -> int:
    if not config.IS_TESTNET:
        print("ABORT: XRPL_NETWORK is not 'testnet'. Refusing to run.", file=sys.stderr)
        return 1

    wallet = Wallet.from_seed(config.SEED)
    client = AsyncJsonRpcClient(config.JSON_RPC_URL)
    issuer = config.SWAP_OFFER_ISSUER
    currency = config.SWAP_OFFER_CURRENCY_HEX
    print(f"Network: testnet | Account/issuer: {wallet.classic_address}")

    # 1. Default Ripple (required for the token to be holdable / AMM-eligible)
    info = await client.request(
        AccountInfo(account=wallet.classic_address, ledger_index="validated")
    )
    flags = int(info.result["account_data"].get("Flags", 0))
    if default_ripple_enabled(flags):
        print("Default Ripple: already enabled.")
    else:
        print("Default Ripple: enabling...")
        resp = await submit_and_wait(
            AccountSet(
                account=wallet.classic_address,
                set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
            ),
            client,
            wallet,
        )
        result = _tx_result(resp)
        if result != "tesSUCCESS":
            print(f"ABORT: AccountSet failed: {result}", file=sys.stderr)
            return 1
        print("Default Ripple: enabled.")

    # 2. Idempotency: skip if the pool already exists
    asset2 = IssuedCurrency(currency=currency, issuer=issuer)
    try:
        existing = await client.request(AMMInfo(asset=XRP(), asset2=asset2))
        amm = existing.result.get("amm")
    except Exception:
        amm = None
    if amm:
        print("AMM already exists — skipping creation.")
        print(format_pool_summary(amm["account"], XRP_AMOUNT_DROPS, BRIX_AMOUNT, TRADING_FEE))
        return await _verify_swap(currency, issuer)

    # 3. Create the pool. Fee = incremental owner reserve (read live).
    si = await client.request(ServerInfo())
    reserve_inc = float(si.result["info"]["validated_ledger"]["reserve_inc_xrp"])
    fee = amm_create_fee_drops(reserve_inc)
    print(f"Creating AMM (fee {fee} drops)...")
    resp = await submit_and_wait(
        AMMCreate(
            account=wallet.classic_address,
            amount=XRP_AMOUNT_DROPS,
            amount2=IssuedCurrencyAmount(currency=currency, issuer=issuer, value=BRIX_AMOUNT),
            trading_fee=TRADING_FEE,
            fee=fee,
        ),
        client,
        wallet,
    )
    result = _tx_result(resp)
    if result != "tesSUCCESS":
        print(f"ABORT: AMMCreate failed: {result}", file=sys.stderr)
        return 1
    print("AMMCreate: tesSUCCESS")

    # 4. Verify pool exists and print summary
    confirmed = await client.request(AMMInfo(asset=XRP(), asset2=asset2))
    amm = confirmed.result["amm"]
    print(format_pool_summary(amm["account"], XRP_AMOUNT_DROPS, BRIX_AMOUNT, TRADING_FEE))

    # 5. Verify a swap clears through the pool (AC #2)
    return await _verify_swap(currency, issuer)


async def _verify_swap(currency: str, issuer: str) -> int:
    """Run the production XRP->BRIX path (quote + buy_and_burn) to prove the pool
    clears a swap. Mirrors the trait-swap XRP-fee path exactly."""
    print(f"Verifying swap: quoting {SWAP_TEST_BRIX} BRIX...")
    quote = await xrpl_ops.get_amm_xrp_cost(currency, issuer, Decimal(SWAP_TEST_BRIX))
    if quote is None:
        print("ABORT: AMM quote unavailable after creation.", file=sys.stderr)
        return 1
    max_xrp = str((quote * Decimal(config.SWAP_XRP_FEE_BUFFER)).quantize(Decimal("0.000001")))
    print(f"Quote: {quote} XRP (max_xrp {max_xrp}). Running buy_and_burn...")
    tx_hash = await xrpl_ops.buy_and_burn(currency, issuer, SWAP_TEST_BRIX, max_xrp=max_xrp)
    if tx_hash is None:
        print("ABORT: buy_and_burn through AMM failed.", file=sys.stderr)
        return 1
    print(f"Swap verified through AMM. tx: {tx_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
