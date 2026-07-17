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

from lfg_core import config, xrpl_ops

# lsfSellNFToken bit on an NFTokenOffer ledger object's Flags field.
LSF_SELL_NFTOKEN = 0x00000001

DROPS_PER_XRP = Decimal(1_000_000)

# #239: per-kind denomination. Character listings are XRP drops; trait
# listings are BRIX IssuedCurrencyAmounts on TOKEN_CURRENCY_HEX /
# TOKEN_ISSUER_ADDRESS (the same pair shop_flow.brix_amount uses). The
# expected-currency parameter below selects the branch; every character
# caller passes (or defaults to) "xrp".
VALID_EXPECTS = ("xrp", "brix")

# Listing-value bounds for BRIX prices, mirroring the XRP ones: positive,
# capped at a generous 1e15 (NOT the Trait Shop's SHOP_MIN/MAX_BRIX pricing
# knobs — those bound shop quotes, not P2P listings), and at most 6 decimal
# places (BRIX finer than that is not a price anyone quotes; it also keeps
# the value round-trippable through the UI's fixed-point math).
MAX_BRIX = Decimal(1_000_000_000_000_000)
BRIX_DECIMAL_PLACES = 6

# XRP's total supply is 100 billion XRP (1e17 drops) — no real price can
# exceed it. Decimal accepts scientific notation, so without this bound
# "1E+30" converted to a 36-digit drops string no ledger could honor (#130).
MAX_XRP = Decimal(100_000_000_000)

FetchOffers = Callable[[str], Awaitable[list[dict[str, Any]]]]
# Returns the current validated ledger's close time in Ripple-epoch seconds
# (the same epoch an NFTokenOffer's Expiration uses). Only consulted when a
# matched offer actually carries an Expiration, so the common no-expiry offer
# never incurs the extra lookup.
FetchLedgerTime = Callable[[], Awaitable[int]]


def validate_brix_value(value: str) -> str:
    """Validate + normalize a BRIX listing value string. Rejects float input
    (TypeError), and non-numeric/non-finite strings, values <= 0, values over
    MAX_BRIX, or more than BRIX_DECIMAL_PLACES decimal places (ValueError).
    Returns the fixed-point normalized string (never scientific notation,
    trailing fractional zeros trimmed) — the exact form stored/sent on-wire."""
    if not isinstance(value, str):
        raise TypeError(f"validate_brix_value requires a str, got {type(value).__name__}")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"invalid BRIX amount: {value!r}") from None
    if not amount.is_finite():
        raise ValueError(f"invalid BRIX amount: {value!r}")
    if amount <= 0:
        raise ValueError("BRIX amount must be > 0")
    if amount > MAX_BRIX:
        raise ValueError(f"BRIX amount exceeds cap ({MAX_BRIX})")
    scaled = amount * (Decimal(10) ** BRIX_DECIMAL_PLACES)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"BRIX amount must not have more than {BRIX_DECIMAL_PLACES} decimal places")
    # Fixed-point formatting, never scientific — same rationale as
    # drops_to_xrp_str ("1E+1" broke both display and the JS BigInt path).
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def brix_amount_dict(value: str) -> dict[str, str]:
    """The XRPL IssuedCurrencyAmount dict for a BRIX listing price — the
    Amount an NFTokenCreateOffer txjson carries for a trait listing. Uses
    TOKEN_CURRENCY_HEX/TOKEN_ISSUER_ADDRESS (the pair shop_flow.brix_amount
    uses). Validates + normalizes `value` (see validate_brix_value)."""
    return {
        "currency": config.TOKEN_CURRENCY_HEX,
        "issuer": config.TOKEN_ISSUER_ADDRESS,
        "value": validate_brix_value(value),
    }


def brix_offer_value(amount: Any) -> str | None:
    """The normalized BRIX value of an on-ledger offer Amount, or None when
    the Amount is not OUR BRIX (not a dict, wrong currency/issuer, or an
    invalid value). The shared per-kind acceptance test for the listener,
    backfill, and the extract/verify branches below."""
    if not isinstance(amount, dict):
        return None
    if str(amount.get("currency") or "").upper() != config.TOKEN_CURRENCY_HEX.upper():
        return None
    if amount.get("issuer") != config.TOKEN_ISSUER_ADDRESS:
        return None
    value = amount.get("value")
    if not isinstance(value, str):
        return None
    try:
        return validate_brix_value(value)
    except ValueError:
        return None


def extract_created_sell_offer(
    meta: dict[str, Any], nft_id: str, expect: str = "xrp"
) -> dict[str, Any] | None:
    """Find the CreatedNode NFTokenOffer for `nft_id` in a validated
    NFTokenCreateOffer transaction's `meta["AffectedNodes"]`, and return it
    only if it is a *sell* offer (lsfSellNFToken set) priced in the expected
    currency: `expect="xrp"` requires a string-drops Amount (characters),
    `expect="brix"` requires an IssuedCurrencyAmount dict on our BRIX
    currency+issuer with a valid value (trait listings, #239).

    Returns `{offer_index, amount_drops, destination, flags}` (xrp) or
    `{offer_index, amount_brix, destination, flags}` (brix) on a match, or
    None when: meta/AffectedNodes is missing or malformed; no CreatedNode of
    LedgerEntryType "NFTokenOffer" matches `nft_id`; the matching offer is a
    buy offer (lsfSellNFToken not set); or the offer's Amount is the wrong
    denomination for `expect` (a dict/IOU for xrp; a drops string or a
    foreign/invalid IOU for brix).
    """
    if expect not in VALID_EXPECTS:
        raise ValueError(f"unknown expected currency: {expect!r}")
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
        if expect == "brix":
            brix_value = brix_offer_value(amount)
            if brix_value is None:
                return None  # XRP drops string, or a foreign/invalid IOU
            return {
                "offer_index": created.get("LedgerIndex"),
                "amount_brix": brix_value,
                "destination": new_fields.get("Destination"),
                "flags": flags,
            }
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
    expected_drops: int | None,
    fetch_offers: FetchOffers | None = None,
    strict: bool = False,
    fetch_ledger_time: FetchLedgerTime | None = None,
    *,
    expect: str = "xrp",
    expected_brix: str | None = None,
) -> bool:
    """Fail-closed check that a sell offer is exactly the listing a buyer is
    about to pay for, run immediately before money moves.

    True ONLY when all of: the offer for `nft_id` at `offer_index` is present
    among the fetched offers; its Amount matches the expected denomination —
    for `expect="xrp"` an XRP drops string equal to `expected_drops`, for
    `expect="brix"` (#239, trait listings) an IssuedCurrencyAmount dict on our
    BRIX currency+issuer whose value Decimal-equals `expected_brix` (any other
    Amount shape can never match — that is a mismatch, not a type error);
    it carries no Destination (a destination-locked offer is
    not a listing this buyer can accept); and it is not expired (an Expiration,
    if present, is strictly after the current ledger's close time — an accept
    against an expired offer fails tecEXPIRED, #183).

    False on every other case, including: the offer being absent, an amount
    mismatch, a foreign Destination, or an at/before-now Expiration.

    `strict` controls how a *lookup failure* is treated. In the default
    (non-strict) mode `fetch_offers` raising is swallowed to False — a
    fail-closed "no valid offer", never a false positive. In strict mode the
    exception PROPAGATES so the caller (the buy-start verify, fix #3) can tell
    "the lookup itself broke" (respond 503, touch nothing) apart from "the
    offer is genuinely gone" (stale-close). When `fetch_offers` is left as the
    default, strict is threaded into `get_nft_sell_offers(raise_on_error=...)`
    so a rippled soft-error surfaces as a raise rather than an empty list.
    `fetch_ledger_time` (defaulting to `xrpl_ops.get_ledger_time`) is only
    consulted when the matched offer actually carries an Expiration; its
    failure is treated exactly like a `fetch_offers` failure (raise under
    strict, else False)."""
    if expect not in VALID_EXPECTS:
        raise ValueError(f"unknown expected currency: {expect!r}")
    if expect == "brix" and expected_brix is None:
        raise ValueError("expected_brix is required when expect='brix'")
    if expect == "xrp" and expected_drops is None:
        raise ValueError("expected_drops is required when expect='xrp'")
    fetch: FetchOffers
    if fetch_offers is None:

        async def _default_fetch(nid: str) -> list[dict[str, Any]]:
            return await xrpl_ops.get_nft_sell_offers(nid, raise_on_error=strict)

        fetch = _default_fetch
    else:
        fetch = fetch_offers

    ledger_time: FetchLedgerTime
    if fetch_ledger_time is None:

        async def _default_ledger_time() -> int:
            return await xrpl_ops.get_ledger_time()

        ledger_time = _default_ledger_time
    else:
        ledger_time = fetch_ledger_time

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
        if expect == "brix":
            brix_value = brix_offer_value(amount)
            if brix_value is None:
                return False  # drops string, or a foreign/invalid IOU
            assert expected_brix is not None  # guarded above
            if Decimal(brix_value) != Decimal(expected_brix):
                return False
        else:
            if not isinstance(amount, str) or not amount.isdigit():
                return False  # dict/IOU Amount (or malformed drops string)
            assert expected_drops is not None  # guarded above
            if int(amount) != int(expected_drops):
                return False
        if offer.get("destination") is not None:
            return False
        expiration = offer.get("expiration")
        if expiration is not None:
            # The offer has an XRPL Expiration (Ripple-epoch seconds). It is
            # already dead — NFTokenAcceptOffer would fail tecEXPIRED — once
            # that time is at/before the current ledger's close time, so
            # reject it rather than hand the buyer a doomed XUMM payload. Only
            # expiring offers pay for this extra ledger-time lookup.
            try:
                now = await ledger_time()
            except Exception:
                if strict:
                    raise
                return False
            try:
                if int(expiration) <= int(now):
                    return False
            except (TypeError, ValueError):
                return False  # malformed Expiration — fail closed
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
