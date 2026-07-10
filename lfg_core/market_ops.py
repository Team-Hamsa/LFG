# lfg_core/market_ops.py
# In-app marketplace: pure offer-meta extraction + the XRP<->drops money
# edge. No I/O, no network — the service layer feeds this already-fetched,
# validated NFTokenCreateOffer tx metadata. Internally prices are always
# integer drops; Decimal is used only at this XRP<->drops conversion edge,
# and floats are never accepted or produced (money discipline, see
# .superpowers/sdd/global-constraints.md).
#
# `verify_sell_offer` is the one exception to "no I/O": it needs a live
# ledger lookup to be fail-closed, so the lookup is injected as a callable
# (`fetch_offers`, defaulting to `xrpl_ops.get_nft_sell_offers`) — the
# decision logic stays pure and unit-testable, the network dependency stays
# at the edge.

from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from lfg_core import xrpl_ops

# lsfSellNFToken bit on an NFTokenOffer ledger object's Flags field.
LSF_SELL_NFTOKEN = 0x00000001

DROPS_PER_XRP = Decimal(1_000_000)

# XRP's total supply is 100 billion XRP (1e17 drops) — no real price can
# exceed it. Decimal accepts scientific notation, so without this bound
# "1E+30" converted to a 36-digit drops string no ledger could honor (#130).
MAX_XRP = Decimal(100_000_000_000)

FetchOffers = Callable[[str], Awaitable[list[dict[str, Any]]]]


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


async def verify_sell_offer(
    nft_id: str,
    offer_index: str,
    expected_drops: int,
    fetch_offers: FetchOffers | None = None,
    strict: bool = False,
) -> bool:
    """Fail-closed check that a sell offer is exactly the listing a buyer is
    about to pay for, run immediately before money moves.

    True ONLY when all of: the offer for `nft_id` at `offer_index` is present
    among the fetched offers; its Amount is an XRP drops string equal to
    `expected_drops` (a dict/IOU Amount can never match — that is a mismatch,
    not a type error); and it carries no Destination (a destination-locked
    offer is not a listing this buyer can accept).

    False on every other case, including: the offer being absent, an amount
    mismatch, or a foreign Destination.

    `strict` controls how a *lookup failure* is treated. In the default
    (non-strict) mode `fetch_offers` raising is swallowed to False — a
    fail-closed "no valid offer", never a false positive. In strict mode the
    exception PROPAGATES so the caller (the buy-start verify, fix #3) can tell
    "the lookup itself broke" (respond 503, touch nothing) apart from "the
    offer is genuinely gone" (stale-close). When `fetch_offers` is left as the
    default, strict is threaded into `get_nft_sell_offers(raise_on_error=...)`
    so a rippled soft-error surfaces as a raise rather than an empty list."""
    fetch: FetchOffers
    if fetch_offers is None:

        async def _default_fetch(nid: str) -> list[dict[str, Any]]:
            return await xrpl_ops.get_nft_sell_offers(nid, raise_on_error=strict)

        fetch = _default_fetch
    else:
        fetch = fetch_offers

    try:
        offers = await fetch(nft_id)
    except Exception:
        if strict:
            raise
        return False
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        if str(offer.get("offer_index")) != str(offer_index):
            continue
        amount = offer.get("amount")
        if not isinstance(amount, str) or not amount.isdigit():
            return False  # dict/IOU Amount (or malformed drops string)
        if int(amount) != int(expected_drops):
            return False
        if offer.get("destination") is not None:
            return False
        return True
    return False  # offer_index not present among the fetched offers


def xrp_to_drops_str(xrp: str) -> str:
    """Convert a decimal XRP amount string to an integer drops string.

    Rejects float input (TypeError), and non-numeric strings, values <= 0,
    values beyond XRP's 100e9 total supply (MAX_XRP), or amounts with more
    than 6 decimal places (ValueError) — drops are the atomic unit of XRP,
    so anything finer is not representable.
    """
    if not isinstance(xrp, str):
        raise TypeError(f"xrp_to_drops_str requires a str, got {type(xrp).__name__}")
    try:
        value = Decimal(xrp)
    except InvalidOperation:
        raise ValueError(f"invalid XRP amount: {xrp!r}") from None
    if value <= 0:
        raise ValueError("XRP amount must be > 0")
    if value > MAX_XRP:
        raise ValueError(f"XRP amount exceeds total supply ({MAX_XRP} XRP)")

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
    # Fixed-point formatting, never scientific: Decimal.normalize() turns e.g.
    # Decimal("10") into Decimal("1E+1") (and to_integral_value() preserves that
    # exponent form), so a 10 XRP listing rendered as "1E+1" — which also made
    # the JS BigInt("1E+1") buy path throw. format(_, "f") never uses an
    # exponent; trim trailing fractional zeros by hand.
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
