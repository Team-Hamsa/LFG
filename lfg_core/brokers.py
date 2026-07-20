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
# file of {"<address>": {"name": ..., "url_template": ...}} entries, where
# url_template may contain "{nft_id}". A file entry for a built-in address
# replaces the built-in.

from __future__ import annotations

import json
import logging
import os
from typing import Any

_BUILTIN: dict[str, dict[str, str | None]] = {
    # xrp.cafe's brokered-sale account (Bithomp username "xrpcafe").
    "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC": {
        "name": "xrp.cafe",
        "url_template": "https://xrp.cafe/nft/{nft_id}",
    },
    # bidds.com (Bithomp username "bidds").
    "rpZqTPC8GvrSvEfFsUuHkmPCg29GdQuXhC": {
        "name": "bidds",
        "url_template": "https://bidds.com/nft/{nft_id}",
    },
    # artdept.fun (Bithomp username "Art Dept") — no stable per-NFT deep-link
    # scheme known, so external cards for it show the name without a link.
    "rnPNSonfEN1TWkPH4Kwvkk3693sCT4tsZv": {
        "name": "Art Dept",
        "url_template": None,
    },
}

_cache: dict[str, dict[str, str | None]] | None = None
_cache_key: str | None = None


def _load() -> dict[str, dict[str, str | None]]:
    """The effective allowlist: built-ins merged with the optional
    BROKER_ALLOWLIST_PATH JSON overlay (file entries win). Cached per path so
    repeated browse requests don't re-read the file; a malformed/unreadable
    file logs a warning and falls back to the built-ins alone (never crashes
    the public browse endpoint)."""
    global _cache, _cache_key
    path = os.getenv("BROKER_ALLOWLIST_PATH") or None
    if _cache is not None and _cache_key == path:
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
                merged[str(addr)] = {"name": str(entry["name"]), "url_template": template}
        except Exception as e:
            logging.warning(f"broker allowlist {path!r} unusable ({e}); using built-ins")
            merged = dict(_BUILTIN)
    _cache, _cache_key = merged, path
    return merged


def known_destinations() -> frozenset[str]:
    """Every allowlisted broker account address."""
    return frozenset(_load())


def resolve(destination: str | None, nft_id: str) -> dict[str, str | None] | None:
    """{"name", "url"} for an allowlisted broker destination (url None when
    the broker has no known deep-link scheme), or None for an unknown/absent
    destination."""
    if not destination:
        return None
    entry = _load().get(destination)
    if entry is None:
        return None
    template = entry.get("url_template")
    return {
        "name": entry["name"],
        "url": template.format(nft_id=nft_id) if template else None,
    }
