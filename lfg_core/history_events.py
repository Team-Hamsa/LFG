"""Pure derivation of NFT/BRIX events from normalized XRPL tx dicts.

A "normalized" tx has its fields at top level plus `meta` (metadata dict),
`hash`, `ledger_index` — the shape scripts/onchain_listener.py's
_normalize_stream_tx produces and normalize_entry() below reproduces for
account_tx / nft_history entries. All functions are pure and unit-testable."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from xrpl.core import addresscodec

from lfg_core import nft_listener

RIPPLE_EPOCH = 946684800

_LSF_SELL = 0x00000001  # lsfSellNFToken on NFTokenOffer


def issuer_account_hex(address: str) -> str:
    """Classic r-address -> 40-hex uppercase AccountID (as embedded in NFTokenIDs)."""
    return addresscodec.decode_classic_address(address).hex().upper()


def nft_id_issuer_matches(nft_id: str, issuer_account_hex: str) -> bool:
    """True iff `nft_id` is a 64-hex NFTokenID whose issuer field (bytes 4-24,
    hex chars 8..48) equals `issuer_account_hex`. Used to scope firehose-derived
    NFT events to our collection."""
    return (
        isinstance(nft_id, str)
        and len(nft_id) == 64
        and nft_id[8:48].upper() == issuer_account_hex.upper()
    )


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


def _is_zero_price(amount: Any) -> bool:
    """True for a zero-value IOU Amount or a missing/None Amount."""
    if amount is None:
        return True
    if isinstance(amount, str):
        try:
            return int(amount) == 0
        except ValueError:
            return False
    if isinstance(amount, dict):
        try:
            return float(amount.get("value", "0")) == 0
        except (TypeError, ValueError):
            return False
    return False


def memo_action(tx: dict[str, Any]) -> str | None:
    """The tx's provenance `action` memo value (#54), or None.

    Legacy transactions predate the memo schema and third-party txs carry
    arbitrary memos, so anything malformed (non-dict entries, bad hex,
    non-UTF-8 data) is skipped, never raised. The leaderboard keys on this to
    tell an economy assemble-remint from a legacy burn+remint trait swap —
    the two are otherwise identical on-chain."""
    for entry in tx.get("Memos") or []:
        if not isinstance(entry, dict):
            continue
        memo = entry.get("Memo")
        if not isinstance(memo, dict):
            continue
        try:
            if bytes.fromhex(memo.get("MemoType") or "").decode("utf-8") == "action":
                # Absent/empty MemoData is None per contract, not ""
                return bytes.fromhex(memo.get("MemoData") or "").decode("utf-8") or None
        except (ValueError, UnicodeDecodeError):
            continue
    return None


def derive_nft_events(tx: dict[str, Any], *, nft_issuer: str) -> list[dict[str, Any]]:
    """Derive event rows for one normalized tx.

    Per-collection scoping (by `nft_issuer`) happens in the CALLERS, not here:
    the listener firehose and the rederive pass both filter the returned events
    with `nft_id_issuer_matches(ev["nft_id"], issuer_account_hex(nft_issuer))`
    — every NFTokenID embeds its issuer's AccountID at hex chars 8..48.
    `nft_issuer` is accepted for signature stability but is unused in-function.

    A non-tesSUCCESS tx is ledger-included but performed nothing, so it derives
    no events (#235: tec NFTokenBurn attempts once counted as real burns).
    """
    if not nft_listener.tx_succeeded(tx, log_skip_as="derive_nft_events"):
        return []
    ttype = str(tx.get("TransactionType", ""))
    meta = tx.get("meta") or {}
    ts = tx_unix_time(tx)
    base = {
        "tx_hash": tx.get("hash"),
        "memo_action": memo_action(tx),
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
        sell = next((o for o in offers if int(o.get("Flags") or 0) & _LSF_SELL), None)
        buy = next((o for o in offers if not (int(o.get("Flags") or 0) & _LSF_SELL)), None)
        if sell is not None and buy is not None:
            # Brokered sale: tx.Account is the broker, not a party to the
            # trade. Seller = sell offer's Owner, buyer = buy offer's Owner,
            # price = what the buyer paid (the buy offer's Amount).
            seller, buyer = sell.get("Owner"), buy.get("Owner")
            price_amount = buy.get("Amount")
        elif sell is not None:
            seller, buyer = sell.get("Owner"), account
            price_amount = sell.get("Amount")
        else:  # buy offer accepted: offer owner is the buyer, accepter sells
            offer = buy or offers[0]
            seller, buyer = account, offer.get("Owner")
            price_amount = offer.get("Amount")
        is_transfer = _is_zero_price(price_amount)
        event = "transfer" if is_transfer else "sale"
        drops, token = (None, None) if is_transfer else _price_fields(price_amount)
        # The deleted offer's own NFTokenID is authoritative for which token
        # changed hands. affected_nft_ids' page-diff fallback scans
        # NFTokenPage NewFields/FinalFields, which can include *other* tokens
        # shuffled between pages in the same tx — falling back to ids[0] there
        # can silently attribute the event to the wrong nft_id.
        chosen_offer = sell if sell is not None else (buy or offers[0])
        nft_id = chosen_offer.get("NFTokenID") or meta.get("nftoken_id") or ids[0]
        out = {
            **base,
            "nft_id": nft_id,
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


def _is_brix(cur: Any, brix_hex: str) -> bool:
    return isinstance(cur, str) and cur.upper() in (brix_hex.upper(), "BRIX")


def _brix_deltas(meta: dict[str, Any], brix_issuer: str, brix_hex: str) -> dict[str, float]:
    """Per-holder BRIX balance change from RippleState node diffs."""
    deltas: dict[str, float] = {}
    for node in meta.get("AffectedNodes", []):
        wrapper = (
            node.get("ModifiedNode") or node.get("CreatedNode") or node.get("DeletedNode") or {}
        )
        if wrapper.get("LedgerEntryType") != "RippleState":
            continue
        final = wrapper.get("FinalFields") or wrapper.get("NewFields") or {}
        bal = final.get("Balance") or {}
        if not _is_brix(bal.get("currency"), brix_hex):
            continue
        low = (final.get("LowLimit") or {}).get("issuer")
        high = (final.get("HighLimit") or {}).get("issuer")
        if brix_issuer not in (low, high):
            continue
        holder = high if low == brix_issuer else low
        if not isinstance(holder, str):
            continue  # malformed node (firehose input): skip, don't crash
        sign = 1.0 if holder == low else -1.0
        prev_bal = (wrapper.get("PreviousFields") or {}).get("Balance") or {}
        old = float(prev_bal.get("value") or 0.0)
        new = float(bal.get("value") or 0.0)
        if node.get("DeletedNode"):
            new = 0.0
        delta = sign * (new - old)
        if delta:
            deltas[holder] = deltas.get(holder, 0.0) + delta
    return deltas


def derive_brix_events(
    tx: dict[str, Any],
    *,
    brix_issuer: str,
    brix_hex: str,
    distributor: str | None = None,
) -> list[dict[str, Any]]:
    """Derive BRIX balance-event rows for one normalized tx.

    Per-holder BRIX trustline balance changes, shaped for history_store.insert_brix_event.
    A non-tesSUCCESS tx moved no balances, so it derives no events (#235).
    """
    if not nft_listener.tx_succeeded(tx, log_skip_as="derive_brix_events"):
        return []
    ttype = str(tx.get("TransactionType", ""))
    account = tx.get("Account")
    deltas = _brix_deltas(tx.get("meta") or {}, brix_issuer, brix_hex)
    if not deltas:
        return []
    if ttype == "TrustSet":
        kind = "trustset"
    elif ttype == "Payment":
        kind = "airdrop" if distributor and account == distributor else "payment"
    elif ttype == "AMMDeposit":
        kind = "amm_deposit"
    elif ttype == "AMMWithdraw":
        kind = "amm_withdraw"
    else:
        kind = "amm_swap"
    ts = tx_unix_time(tx)
    accounts = sorted(deltas)
    return [
        {
            "tx_hash": tx.get("hash"),
            "account": a,
            # counterparty: the other mover if exactly two, else the tx sender.
            "counterparty": (
                next((b for b in accounts if b != a), account) if len(accounts) == 2 else account
            ),
            "delta": deltas[a],
            "kind": kind,
            "ts": ts,
        }
        for a in accounts
    ]
