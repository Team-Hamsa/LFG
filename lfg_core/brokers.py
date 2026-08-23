# lfg_core/brokers.py
# Known-broker allowlist for external/brokered marketplace listings (#131).
#
# A destination-locked NFTokenOffer (Destination set) is either a *brokered*
# marketplace listing (Destination = the marketplace's broker account) or a
# *directed peer-to-peer* offer (seller -> one specific buyer). Per the #131
# decision, browse surfaces ONLY known-broker destinations — showing arbitrary
# destination offers would publicly expose private directed offers — so this
# allowlist is the single gate on what counts as "external listing" anywhere
# in the app. Unknown destinations stay hidden.
#
# Built-ins were identified from the live mainnet market_listings index
# (the top destination accounts actually holding offers on our NFTs) and
# verified against Bithomp's account naming (2026-07-20). Operators can extend
# or override the set without a code change via BROKER_ALLOWLIST_PATH — a JSON
# file of {"<address>": {"name": ..., "url_template": ..., "broker_rate": ...}}
# entries, where url_template may contain "{nft_id}". A file entry for a
# built-in address replaces the built-in.
#
# broker_rate (#426) is the broker's fee as a fraction of the BUY amount
# (cafe: 0.015890, measured to the drop against a live mainnet fill — see
# docs/superpowers/specs/2026-08-22-external-buy-now-design.md). The brokers'
# bots auto-settle any plain buy offer that clears the ask after their fee,
# regardless of who created it, so a known rate lets the app compute the
# minimum bid that fills ("Buy now"). An UNMEASURED broker ships
# broker_rate None — a guessed rate produces bids that silently never fill,
# so None means "no Buy-now button", not "assume zero".

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

# Sanity ceiling on an overlay-supplied rate: no marketplace takes a quarter
# of the buy amount; anything at/above this is a typo (e.g. 1.589 for 1.589%).
MAX_BROKER_RATE = 0.25

BrokerEntry = dict[str, Any]

_BUILTIN: dict[str, BrokerEntry] = {
    # xrp.cafe's brokered-sale account (Bithomp username "xrpcafe").
    "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC": {
        "name": "xrp.cafe",
        "url_template": "https://xrp.cafe/nft/{nft_id}",
        "broker_rate": 0.015890,
    },
    # bidds.com (Bithomp username "bidds"). Rate unmeasured (#426).
    "rpZqTPC8GvrSvEfFsUuHkmPCg29GdQuXhC": {
        "name": "bidds",
        "url_template": "https://bidds.com/nft/{nft_id}",
        "broker_rate": None,
    },
    # artdept.fun (Bithomp username "Art Dept") — no stable per-NFT deep-link
    # scheme known, so external cards for it show the name without a link.
    # Rate unmeasured (#426).
    "rnPNSonfEN1TWkPH4Kwvkk3693sCT4tsZv": {
        "name": "Art Dept",
        "url_template": None,
        "broker_rate": None,
    },
}

_cache: dict[str, BrokerEntry] | None = None
_cache_key: tuple[str | None, float | None] | None = None


def _overlay_mtime(path: str | None) -> float | None:
    if not path:
        return None
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _parse_rate(addr: str, raw: Any) -> float | None:
    """Validate an overlay broker_rate: absent/null -> None (unmeasured);
    otherwise a finite number (or numeric string) in [0, MAX_BROKER_RATE).
    bool is rejected explicitly — json `true` is an int subclass in Python
    and would otherwise parse as 1.0."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"bad broker_rate for {addr!r}: not a number")
    try:
        rate = float(raw)
    except ValueError as e:
        raise ValueError(f"bad broker_rate for {addr!r}: {raw!r}") from e
    if not math.isfinite(rate) or not (0.0 <= rate < MAX_BROKER_RATE):
        raise ValueError(f"bad broker_rate for {addr!r}: {rate!r} not in [0, {MAX_BROKER_RATE})")
    return rate


def _load() -> dict[str, BrokerEntry]:
    """The effective allowlist: built-ins merged with the optional
    BROKER_ALLOWLIST_PATH JSON overlay (file entries win). Cached per
    (path, file mtime) so repeated browse requests don't re-parse the file
    while an operator edit (add a broker / pull a compromised one) is picked
    up on the next call — no restart needed. A malformed/unreadable file logs
    a warning and falls back to the built-ins alone (never crashes the public
    browse endpoint)."""
    global _cache, _cache_key
    path = os.getenv("BROKER_ALLOWLIST_PATH") or None
    key = (path, _overlay_mtime(path))
    if _cache is not None and _cache_key == key:
        return _cache
    merged = dict(_BUILTIN)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                overlay: Any = json.load(f)
            if not isinstance(overlay, dict):
                raise ValueError("allowlist root must be a JSON object")
            for addr, entry in overlay.items():
                if not isinstance(entry, dict) or not entry.get("name"):
                    raise ValueError(f"bad entry for {addr!r}: need a 'name'")
                template = entry.get("url_template") or None
                if template is not None:
                    if not isinstance(template, str):
                        raise ValueError(f"bad url_template for {addr!r}: not a string")
                    # A template with an unknown placeholder ({nftid}, {0}, a
                    # stray brace) would raise inside resolve() at serve time
                    # and crash browse serialization — validate it here so a
                    # bad file falls back to built-ins instead.
                    try:
                        template.format(nft_id="x")
                    except (KeyError, IndexError, ValueError) as fmt_err:
                        raise ValueError(f"bad url_template for {addr!r}: {fmt_err}") from fmt_err
                merged[str(addr)] = {
                    "name": str(entry["name"]),
                    "url_template": template,
                    "broker_rate": _parse_rate(str(addr), entry.get("broker_rate")),
                }
        except Exception as e:
            logging.warning(f"broker allowlist {path!r} unusable ({e}); using built-ins")
            merged = dict(_BUILTIN)
    _cache, _cache_key = merged, key
    return merged


def known_destinations() -> frozenset[str]:
    """Every allowlisted broker account address."""
    return frozenset(_load())


def resolve(destination: str | None, nft_id: str) -> BrokerEntry | None:
    """{"name", "url", "broker_rate"} for an allowlisted broker destination
    (url None when the broker has no known deep-link scheme; broker_rate None
    when unmeasured — #426), or None for an unknown/absent destination."""
    if not destination:
        return None
    entry = _load().get(destination)
    if entry is None:
        return None
    template = entry.get("url_template")
    return {
        "name": entry["name"],
        "url": template.format(nft_id=nft_id) if template else None,
        "broker_rate": entry.get("broker_rate"),
    }


def _clearing_buffer_drops() -> int:
    """Optional operator safety margin added on top of the exact clearing
    price (env BROKER_CLEARING_BUFFER_DROPS, default 0). The brokers' fee is
    capped at their rate, so any overshoot goes to the seller — cheap
    insurance against an unannounced rate bump, paid by the buyer only as
    the overshoot. Unparsable/negative values are ignored (0)."""
    raw = os.getenv("BROKER_CLEARING_BUFFER_DROPS")
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        logging.warning(f"BROKER_CLEARING_BUFFER_DROPS={raw!r} is not an int; ignoring")
        return 0


def clearing_drops(ask_drops: int, broker_rate: float) -> int:
    """The minimum buy-offer amount (drops) a broker taking `broker_rate` of
    the BUY amount will settle against an `ask_drops` sell offer (#426):

        ceil(ask / (1 - rate))  [+ BROKER_CLEARING_BUFFER_DROPS]

    Verified to the drop against cafe's live fill (fee = ceil(bid * rate),
    seller gets bid - fee, which must be >= ask). Rounds UP — one drop short
    and the bot ignores the bid forever, silently."""
    if not isinstance(ask_drops, int) or isinstance(ask_drops, bool) or ask_drops <= 0:
        raise ValueError(f"ask_drops must be a positive int, got {ask_drops!r}")
    if not (0.0 <= broker_rate < 1.0):
        raise ValueError(f"broker_rate must be in [0, 1), got {broker_rate!r}")
    # Integer-exact for rate 0; Decimal-free because the rates are
    # short decimals and the ceil absorbs float error (the property test in
    # tests/test_brokers.py pins bid - ceil(bid*rate) >= ask across a spread).
    base = math.ceil(ask_drops / (1.0 - broker_rate))
    # Guard the float ceiling: the minimum bid must actually clear the ask
    # after the broker's ceil()'d fee, and must be the least such bid.
    while base - math.ceil(base * broker_rate) < ask_drops:
        base += 1
    while base > ask_drops and (base - 1) - math.ceil((base - 1) * broker_rate) >= ask_drops:
        base -= 1
    return base + _clearing_buffer_drops()
