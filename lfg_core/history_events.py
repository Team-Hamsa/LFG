"""Pure derivation of NFT/BRIX events from normalized XRPL tx dicts.

A "normalized" tx has its fields at top level plus `meta` (metadata dict),
`hash`, `ledger_index` — the shape scripts/onchain_listener.py's
_normalize_stream_tx produces and normalize_entry() below reproduces for
account_tx / nft_history entries. All functions are pure and unit-testable."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from lfg_core import nft_listener

RIPPLE_EPOCH = 946684800

_LSF_SELL = 0x00000001  # lsfSellNFToken on NFTokenOffer


def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Flatten one account_tx / nft_history response entry into a normalized
    tx dict (tx fields top-level, plus meta/hash/ledger_index)."""
    tx = dict(entry.get("tx") or entry.get("tx_json") or {})
    tx["meta"] = entry.get("meta") or entry.get("metaData") or {}
    tx.setdefault("hash", entry.get("hash"))
    tx.setdefault("ledger_index", entry.get("ledger_index"))
    if "close_time_iso" in entry:
        tx.setdefault("close_time_iso", entry["close_time_iso"])
    return tx


def tx_unix_time(tx: dict[str, Any]) -> int | None:
    date = tx.get("date")
    if isinstance(date, int):
        return date + RIPPLE_EPOCH
    iso = tx.get("close_time_iso")
    if isinstance(iso, str):
        try:
            return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _deleted_nft_offers(meta: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for node in meta.get("AffectedNodes", []):
        wrapper = node.get("DeletedNode") or {}
        if wrapper.get("LedgerEntryType") == "NFTokenOffer":
            out.append(wrapper.get("FinalFields") or {})
    return out


def _price_fields(amount: Any) -> tuple[int | None, str | None]:
    """XRPL Amount -> (price_drops, price_token JSON)."""
    if isinstance(amount, str):
        return int(amount), None
    if isinstance(amount, dict):
        return None, json.dumps(amount, sort_keys=True)
    return None, None


def derive_nft_events(tx: dict[str, Any], *, nft_issuer: str) -> list[dict[str, Any]]:
    ttype = str(tx.get("TransactionType", ""))
    meta = tx.get("meta") or {}
    ts = tx_unix_time(tx)
    base = {
        "tx_hash": tx.get("hash"),
        "nft_number": None,
        "price_drops": None,
        "price_token": None,
        "ledger_index": tx.get("ledger_index"),
        "ts": ts,
    }
    account = tx.get("Account")

    if ttype == "NFTokenMint":
        ids = nft_listener.affected_nft_ids(tx)
        return [
            {
                **base,
                "nft_id": i,
                "event": "mint",
                "from_addr": None,
                "to_addr": tx.get("Issuer") or account,
            }
            for i in ids
        ]

    if ttype == "NFTokenBurn":
        nft_id = tx.get("NFTokenID")
        if not nft_id:
            return []
        return [
            {
                **base,
                "nft_id": nft_id,
                "event": "burn",
                "from_addr": tx.get("Owner") or account,
                "to_addr": None,
            }
        ]

    if ttype == "NFTokenModify":
        nft_id = tx.get("NFTokenID")
        if not nft_id:
            return []
        return [
            {
                **base,
                "nft_id": nft_id,
                "event": "modify",
                "from_addr": None,
                "to_addr": tx.get("Owner") or account,
            }
        ]

    if ttype == "NFTokenAcceptOffer":
        ids = nft_listener.affected_nft_ids(tx)
        offers = _deleted_nft_offers(meta)
        if not ids or not offers:
            return []
        # Prefer the sell offer for price/seller; fall back to the first.
        sell = next((o for o in offers if int(o.get("Flags") or 0) & _LSF_SELL), None)
        offer = sell or offers[0]
        drops, token = _price_fields(offer.get("Amount"))
        if sell is not None:
            seller, buyer = sell.get("Owner"), account
        else:  # buy offer accepted: offer owner is the buyer, accepter sells
            seller, buyer = account, offer.get("Owner")
        event = "transfer" if (drops == 0 and token is None) else "sale"
        out = {
            **base,
            "nft_id": ids[0],
            "event": event,
            "from_addr": seller,
            "to_addr": buyer,
            "price_token": token,
        }
        if event == "sale" and drops:
            out["price_drops"] = drops
        return [out]

    if ttype == "NFTokenCreateOffer":
        nft_id = tx.get("NFTokenID")
        if not nft_id:
            return []
        drops, token = _price_fields(tx.get("Amount"))
        return [
            {
                **base,
                "nft_id": nft_id,
                "event": "offer_create",
                "from_addr": account,
                "to_addr": tx.get("Destination"),
                "price_drops": drops,
                "price_token": token,
            }
        ]

    if ttype == "NFTokenCancelOffer":
        return [
            {
                **base,
                "nft_id": o.get("NFTokenID"),
                "event": "offer_cancel",
                "from_addr": o.get("Owner"),
                "to_addr": None,
            }
            for o in _deleted_nft_offers(meta)
            if o.get("NFTokenID")
        ]

    return []
