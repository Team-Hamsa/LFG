"""Shared BRIX-vs-XRP payment-path detection (#238).

Extracted from swap_flow.detect_swap_payment so the trait-swap fee path and
the Trait Shop use one implementation. Silent detection: wallets holding
enough BRIX pay in BRIX; everyone else pays the live AMM XRP equivalent
(buffered, rounded up) and the buyback is never surfaced to the user.
"""

from __future__ import annotations

from decimal import ROUND_UP, Decimal

from lfg_core import config, xrpl_ops


async def detect_payment_path(
    wallet_address: str,
    brix_amount: str,
    *,
    currency: str | None = None,
    issuer: str | None = None,
    buffer: str | None = None,
) -> tuple[str, str]:
    """Returns ("BRIX", brix_amount) when `wallet_address` holds at least
    `brix_amount` on its currency/issuer trustline; otherwise quotes the AMM
    XRP cost, applies the fee `buffer`, and returns ("XRP", xrp_amount)
    rounded UP to 6 decimals. Raises RuntimeError if the wallet holds no BRIX
    and the AMM can't quote a price. Keyword defaults resolve from config at
    call time (SWAP_OFFER_CURRENCY_HEX / SWAP_OFFER_ISSUER /
    SWAP_XRP_FEE_BUFFER)."""
    currency = config.SWAP_OFFER_CURRENCY_HEX if currency is None else currency
    issuer = config.SWAP_OFFER_ISSUER if issuer is None else issuer
    buffer = config.SWAP_XRP_FEE_BUFFER if buffer is None else buffer

    balance = await xrpl_ops.get_trustline_balance(wallet_address, currency, issuer)
    if balance is not None and balance >= Decimal(brix_amount):
        return "BRIX", brix_amount
    cost = await xrpl_ops.get_amm_xrp_cost(currency, issuer, Decimal(brix_amount))
    if cost is None:
        raise RuntimeError(
            "Swap fee pricing is unavailable right now — please try again in a moment."
        )
    xrp = cost * Decimal(buffer)
    return "XRP", str(xrp.quantize(Decimal("0.000001"), rounding=ROUND_UP))
