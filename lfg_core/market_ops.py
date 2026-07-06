# lfg_core/market_ops.py
# In-app marketplace: pure offer-meta extraction + the XRP<->drops money
# edge. No I/O, no network — the service layer feeds this already-fetched,
# validated NFTokenCreateOffer tx metadata. Internally prices are always
# integer drops; Decimal is used only at this XRP<->drops conversion edge,
# and floats are never accepted or produced (money discipline, see
# .superpowers/sdd/global-constraints.md).

from decimal import Decimal, InvalidOperation
from typing import Any

# lsfSellNFToken bit on an NFTokenOffer ledger object's Flags field.
LSF_SELL_NFTOKEN = 0x00000001

DROPS_PER_XRP = Decimal(1_000_000)


def extract_created_sell_offer(meta: dict[str, Any], nft_id: str) -> dict[str, Any] | None:
    """Find the CreatedNode NFTokenOffer for `nft_id` in a validated
    NFTokenCreateOffer transaction's `meta["AffectedNodes"]`, and return it
    only if it is a *sell* offer (lsfSellNFToken set) priced in XRP (a
    string-drops Amount).

    Returns `{offer_index, amount_drops, destination, flags}` on a match, or
    None when: meta/AffectedNodes is missing or malformed; no CreatedNode of
    LedgerEntryType "NFTokenOffer" matches `nft_id`; the matching offer is a
    buy offer (lsfSellNFToken not set); or the offer's Amount is a dict
    (an IOU amount, not a valid XRP sell offer for our purposes).
    """
    nodes = meta.get("AffectedNodes") if isinstance(meta, dict) else None
    if not isinstance(nodes, list):
        return None

    for node in nodes:
        if not isinstance(node, dict):
            continue
        created = node.get("CreatedNode")
        if not isinstance(created, dict):
            continue
        if created.get("LedgerEntryType") != "NFTokenOffer":
            continue
        new_fields = created.get("NewFields")
        if not isinstance(new_fields, dict):
            continue
        if str(new_fields.get("NFTokenID") or "") != str(nft_id):
            continue

        flags = int(new_fields.get("Flags") or 0)
        if not (flags & LSF_SELL_NFTOKEN):
            return None  # buy-side offer for this nft_id

        amount = new_fields.get("Amount")
        if not isinstance(amount, str) or not amount.isdigit():
            return None  # IOU (dict) Amount, or malformed drops string

        return {
            "offer_index": created.get("LedgerIndex"),
            "amount_drops": int(amount),
            "destination": new_fields.get("Destination"),
            "flags": flags,
        }
    return None


def xrp_to_drops_str(xrp: str) -> str:
    """Convert a decimal XRP amount string to an integer drops string.

    Rejects float input (TypeError), and non-numeric strings, values <= 0,
    or amounts with more than 6 decimal places (ValueError) — drops are the
    atomic unit of XRP, so anything finer is not representable.
    """
    if not isinstance(xrp, str):
        raise TypeError(f"xrp_to_drops_str requires a str, got {type(xrp).__name__}")
    try:
        value = Decimal(xrp)
    except InvalidOperation:
        raise ValueError(f"invalid XRP amount: {xrp!r}") from None
    if value <= 0:
        raise ValueError("XRP amount must be > 0")

    drops = value * DROPS_PER_XRP
    if drops != drops.to_integral_value():
        raise ValueError("XRP amount must not have more than 6 decimal places")
    return str(int(drops))


def drops_to_xrp_str(drops: str) -> str:
    """Convert an integer drops string to a decimal XRP amount string.
    Inverts `xrp_to_drops_str`. Rejects float input (TypeError) and
    non-digit strings (ValueError)."""
    if not isinstance(drops, str):
        raise TypeError(f"drops_to_xrp_str requires a str, got {type(drops).__name__}")
    if not drops.isdigit():
        raise ValueError(f"invalid drops amount: {drops!r}")

    value = Decimal(drops) / DROPS_PER_XRP
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.to_integral_value())
    return format(normalized, "f")
