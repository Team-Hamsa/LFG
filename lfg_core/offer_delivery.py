# lfg_core/offer_delivery.py
#
# Mint offer self-heal (#466): given an NFT already minted at the issuer,
# ensure a destination-locked NFTokenCreateOffer sell offer exists for it,
# retrying a transient create failure (e.g. JSON-RPC tooBusy) instead of
# stranding the token at the issuer with no recovery path.
#
# Extracted verbatim (adopt -> delivered -> create ordering, #227) from
# bulk_mint_flow._ensure_offer, which now delegates here with attempts=1 --
# its own callers (the fulfillment loop's main pass, the bounded final
# re-offer pass, and a resumed job's startup sweep) already provide the
# retry cadence across MULTIPLE persisted invocations, so wrapping each one
# in its own multi-attempt retry would only duplicate that and slow every
# pass by the internal backoff. mint_flow._finalize_minted_unit (#466) is
# the first caller to use the built-in multi-attempt retry directly, closing
# the self-heal gap a failed NFTokenCreateOffer used to leave in a single
# mint (and its sponsored-mint resume twin): no retry, no recovery record,
# and a "contact an administrator" dead end.
#
# Each attempt re-runs the FULL adopt -> delivered -> create sequence (not
# just the create call): xrpl_ops.create_nft_offer already collapses an
# indeterminate outcome to None (#211) -- if a create actually landed
# on-chain despite that, the NEXT attempt's adopt check finds it and returns
# it rather than risking a duplicate live offer for the same token.
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from lfg_core import memos, xrpl_ops

OfferStatus = Literal["offered", "adopted", "delivered", "failed"]


@dataclass(frozen=True)
class OfferResult:
    """Outcome of ensure_offer. `offer_index` is the live sell offer's ledger
    index for "offered"/"adopted" (something for the caller to build a XUMM
    accept payload around); it is None for "delivered" (the offer was
    already accepted -- nothing left to point at) and for "failed".
    `reason` is a human-readable explanation, set only for "failed"."""

    status: OfferStatus
    offer_index: str | None
    reason: str | None


def _is_our_gift_offer(offer: dict[str, object], destination: str) -> bool:
    """True for a live offer matching exactly what create_nft_offer emits:
    our wallet as owner, `destination` as the locked recipient, a free
    (amount "0") gift with no Expiration. Anything looser risks adopting a
    foreign or priced offer (#227)."""
    return (
        offer.get("owner") == xrpl_ops.bot_wallet_address()
        and offer.get("destination") == destination
        and offer.get("amount") == "0"
        and offer.get("expiration") is None
        and bool(offer.get("offer_index"))
    )


async def ensure_offer(
    nft_id: str,
    destination: str,
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    platform: str = memos.PLATFORM_BACKEND,
) -> OfferResult:
    """Ensure a destination-locked sell offer exists for an already-minted
    `nft_id`. NEVER mints -- only adopts / detects-delivered / creates a
    delivery offer for a token that already exists on-ledger.

    Retries the full check-then-create sequence up to `attempts` times,
    sleeping `base_delay` seconds between attempts (not before the first).
    Per attempt, in order:

    1. Adopt: a live offer already shaped exactly like our own gift offer
       (see `_is_our_gift_offer`) is reused as-is -- status "adopted",
       `create_nft_offer` is never called. A lookup failure here is
       INDETERMINATE (a live offer may be hiding behind the blip, and
       creating blind risks a duplicate), so it counts as a failed attempt
       and moves on to the next retry rather than falling through to create.
    2. Delivered: if the token's on-ledger owner is exactly `destination`
       (the intended recipient) and it is not burned, there is nothing to
       offer -- our own offer from an earlier attempt (or an earlier crashed
       process) already landed and was accepted. status "delivered",
       `offer_index` stays None (the accept consumed the offer object;
       there is nothing left to point at). Any other owner (a third party,
       or still us) and a burned token both fail this check and fall through
       to create -- "owner is merely not us" is NOT delivered (#571 review:
       a token transferred, burned, or otherwise gone missing must never be
       reported as a success with no recovery record; only the intended
       recipient actually holding it counts). An indeterminate owner lookup
       (nft_info returns None) fails closed the same way -- falls through to
       create, never marks an undelivered token "delivered" on a transient
       clio blip.
    3. Create: `xrpl_ops.create_nft_offer` (its return-None contract, #211,
       is untouched). A falsy result or a raised exception counts as a
       failed attempt.

    Returns "failed" with the most recent attempt's `reason` once `attempts`
    is exhausted.

    KNOWN RACE (#571 review, not closed here by explicit decision -- see the
    PR thread): two concurrent callers for the SAME `nft_id` can each pass
    the adopt and delivered checks before either has created an offer, and
    both then call create_nft_offer -- yielding two live sell offers for one
    token. This function does not serialize across callers (only
    xrpl_ops.create_nft_offer's own per-SIGNING_ACCOUNT submission_coordinator
    lock applies, and it protects sequence-number safety, not this decision);
    a multi-attempt retry loop with backoff widens the exposure window
    versus a single-pass call, though it does not create a NEW race (bulk
    mint's original single-pass _ensure_offer already had it). The
    consequence is a stray extra offer, never a lost or double-minted NFT.
    Closing it for real needs mutual exclusion per nft_id (e.g. re-checking
    adopt while already holding xrpl_ops.submission_coordinator, immediately
    before create) -- deliberately not built here; local reordering cannot
    shrink the window, since it is bounded by create_nft_offer's own
    multi-second ledger round trip, not by anything between two awaits in
    this function.
    """
    reason: str | None = None
    for attempt in range(attempts):
        if attempt > 0:
            await asyncio.sleep(base_delay)
        try:
            offers = await xrpl_ops.get_nft_sell_offers(nft_id, raise_on_error=True)
        except Exception as e:
            reason = f"offer lookup failed: {e}"
            logging.warning(f"ensure_offer({nft_id}): {reason}")
            continue

        for offer in offers:
            if _is_our_gift_offer(offer, destination):
                offer_index = offer["offer_index"]
                assert isinstance(offer_index, str)
                return OfferResult(status="adopted", offer_index=offer_index, reason=None)

        info = await xrpl_ops.nft_info(nft_id)
        if info is not None and not info.get("is_burned") and info.get("owner") == destination:
            return OfferResult(status="delivered", offer_index=None, reason=None)

        try:
            offer_id = await xrpl_ops.create_nft_offer(nft_id, destination, platform=platform)
        except Exception as e:
            reason = str(e)
            continue
        if offer_id:
            return OfferResult(status="offered", offer_index=offer_id, reason=None)
        reason = "offer creation failed"

    return OfferResult(status="failed", offer_index=None, reason=reason)
