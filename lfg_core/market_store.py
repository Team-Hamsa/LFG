# lfg_core/market_store.py
# In-app marketplace listing index. `market_listings` is a derived,
# droppable, rebuildable index over on-ledger NFTokenOffer sell offers — same
# posture as `nft_events` (lfg_core/history_store.py) and `onchain_nfts`
# (lfg_core/nft_index.py). No listing exists here unless a live NFTokenOffer
# ledger object backs it; the listener/backfill are the only writers. Lives
# in the same per-network onchain_{network}.db as nft_index/economy_store.
#
# Browse joins the owner-of-record for each kind so a stale listing (seller
# no longer owns the token) is hidden without a ledger round-trip:
# `onchain_nfts` for kind='character', `trait_tokens` for kind='trait'.

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

_VALID_KINDS = {"character", "trait"}
_VALID_CLOSE_REASONS = {"sold", "cancelled", "stale"}
_VALID_SORTS = {"price_asc", "price_desc", "newest"}

# Verbatim from docs/superpowers/specs/2026-07-05-marketplace-design.md (Q1),
# including comments — the DDL is spec-authoritative.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_listings (
    offer_index   TEXT PRIMARY KEY,   -- NFTokenOffer LedgerIndex (64-hex)
    nft_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- 'character' | 'trait'
    seller        TEXT NOT NULL,      -- offer Owner
    amount_drops  INTEGER NOT NULL,   -- XRP-denominated only in MVP
    destination   TEXT,               -- non-NULL ⇒ hidden from browse
    slot          TEXT,               -- trait kind only (denormalized)
    value         TEXT,               -- trait kind only (denormalized)
    created_ledger INTEGER,
    created_ts    INTEGER,
    is_live       INTEGER NOT NULL DEFAULT 1,
    closed_reason TEXT,               -- sold | cancelled | stale
    settled       INTEGER             -- trait kind: 0=burn-back pending, 1=done; NULL for characters
);
CREATE INDEX IF NOT EXISTS idx_market_live ON market_listings(is_live, kind, nft_id);
"""


@dataclass
class MarketListing:
    """A live (or about-to-be-upserted) row. `slot`/`value` are populated
    only for kind='trait' (denormalized from trait_tokens at write time so
    browse/history never need a second join for them). `settled` is left
    unset (None) at listing time for both kinds — `close_listing` is the only
    place that assigns it a meaning, per the spec's sold-trait rule."""

    offer_index: str
    nft_id: str
    kind: str
    seller: str
    amount_drops: int
    destination: str | None = None
    slot: str | None = None
    value: str | None = None
    created_ledger: int | None = None
    created_ts: int | None = None
    is_live: int = 1
    closed_reason: str | None = None
    settled: int | None = None


def init_db(conn: sqlite3.Connection) -> None:
    """Create the market_listings table + index if absent. Idempotent —
    calling this twice on the same connection is a no-op the second time."""
    conn.executescript(_SCHEMA)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: MarketListing) -> None:
    """Insert a listing or overwrite it in place (keyed on offer_index) —
    used by the listener's offer_create handler and by the backfill rebuild.
    Raises ValueError for an unrecognized `kind`."""
    if listing.kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind: {listing.kind!r}")
    conn.execute(
        """
        INSERT INTO market_listings
            (offer_index, nft_id, kind, seller, amount_drops, destination,
             slot, value, created_ledger, created_ts, is_live, closed_reason, settled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(offer_index) DO UPDATE SET
            nft_id=excluded.nft_id,
            kind=excluded.kind,
            seller=excluded.seller,
            amount_drops=excluded.amount_drops,
            destination=excluded.destination,
            slot=excluded.slot,
            value=excluded.value,
            created_ledger=excluded.created_ledger,
            created_ts=excluded.created_ts,
            is_live=excluded.is_live,
            closed_reason=excluded.closed_reason,
            settled=excluded.settled
        """,
        (
            listing.offer_index,
            listing.nft_id,
            listing.kind,
            listing.seller,
            listing.amount_drops,
            listing.destination,
            listing.slot,
            listing.value,
            listing.created_ledger,
            listing.created_ts,
            listing.is_live,
            listing.closed_reason,
            listing.settled,
        ),
    )
    conn.commit()


def close_listing(conn: sqlite3.Connection, offer_index: str, reason: str) -> None:
    """Mark a listing no-longer-live. `reason` must be one of
    sold|cancelled|stale. Per the spec, closing a *trait* listing with
    reason='sold' also sets settled=0 (burn-back-to-Closet pending) in the
    same statement — every other case leaves `settled` untouched (NULL for
    characters, or whatever it already was)."""
    if reason not in _VALID_CLOSE_REASONS:
        raise ValueError(f"unknown close reason: {reason!r}")
    conn.execute(
        """
        UPDATE market_listings
        SET is_live = 0,
            closed_reason = ?,
            settled = CASE WHEN kind = 'trait' AND ? = 'sold' THEN 0 ELSE settled END
        WHERE offer_index = ?
        """,
        (reason, reason, offer_index),
    )
    conn.commit()


def mark_settled(conn: sqlite3.Connection, offer_index: str) -> None:
    """Flip a sold trait listing's settled flag to 1 (burn-back-to-Closet
    done) — called after `run_deposit` succeeds on behalf of the buyer."""
    conn.execute(
        "UPDATE market_listings SET settled = 1 WHERE offer_index = ?",
        (offer_index,),
    )
    conn.commit()


def unsettled_trait_sales(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Sold trait listings still awaiting settlement — the settlement sweep's
    worklist (backstop for the primary buy-status-handler trigger)."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM market_listings WHERE kind='trait' AND closed_reason='sold' AND settled=0"
    )
    return [dict(row) for row in cur.fetchall()]


def _attributes_match(attrs: list[dict[str, str]], filters: dict[str, list[str]]) -> bool:
    """AND across slots in `filters`, OR within a slot's value list. `attrs`
    is a list of {"trait_type": ..., "value": ...} entries (the normalized
    metadata shape from swap_meta.normalize_attributes / nft_index)."""
    for slot, wanted_values in filters.items():
        wanted = set(wanted_values)
        if not any(a.get("trait_type") == slot and a.get("value") in wanted for a in attrs):
            return False
    return True


def _browse_character_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT ml.*, o.nft_number AS nft_number, o.attributes_json AS attributes_json
        FROM market_listings ml
        JOIN onchain_nfts o ON ml.nft_id = o.nft_id
        WHERE ml.kind = 'character'
          AND ml.is_live = 1
          AND ml.destination IS NULL
          AND ml.seller = o.owner
          AND o.is_burned = 0
        """
    )
    return cur.fetchall()


def _browse_trait_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT ml.*
        FROM market_listings ml
        JOIN trait_tokens t ON ml.nft_id = t.nft_id
        WHERE ml.kind = 'trait'
          AND ml.is_live = 1
          AND ml.destination IS NULL
          AND ml.seller = t.owner
        """
    )
    return cur.fetchall()


def _row_attrs(row: sqlite3.Row, kind: str) -> list[dict[str, str]]:
    if kind == "character":
        raw = row["attributes_json"]
        return list(json.loads(raw)) if raw else []
    return [{"trait_type": str(row["slot"]), "value": str(row["value"])}]


def browse(
    conn: sqlite3.Connection,
    kind: str = "character",
    trait_filters: dict[str, list[str]] | None = None,
    min_amount_drops: int | None = None,
    max_amount_drops: int | None = None,
    sort: str = "price_asc",
    limit: int = 24,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Live, browsable listings of one `kind`, joined against the owner-of-
    record table so a listing whose seller no longer holds the token is
    hidden. Rows carry the market_listings columns plus, for kind='character',
    the joined `nft_number`/`attributes_json` (kind='trait' already carries
    its own denormalized `slot`/`value` columns — no extra join needed).

    Trait filtering (AND across slots, OR within a slot's values) and amount
    bounds are applied in Python after the ownership join — the dataset is
    small (thousands of rows) and attribute matching needs to parse JSON, so
    there is no benefit to pushing it into SQL. Sort + limit/offset are
    applied last, after filtering, so pagination is over the final result
    set. Raises ValueError for an unrecognized `kind` or `sort`.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    if sort not in _VALID_SORTS:
        raise ValueError(f"unknown sort: {sort!r}")

    rows = _browse_character_rows(conn) if kind == "character" else _browse_trait_rows(conn)

    if min_amount_drops is not None:
        rows = [r for r in rows if r["amount_drops"] >= min_amount_drops]
    if max_amount_drops is not None:
        rows = [r for r in rows if r["amount_drops"] <= max_amount_drops]

    if trait_filters:
        rows = [r for r in rows if _attributes_match(_row_attrs(r, kind), trait_filters)]

    if sort == "price_asc":
        rows.sort(key=lambda r: (r["amount_drops"], r["offer_index"]))
    elif sort == "price_desc":
        rows.sort(key=lambda r: (-r["amount_drops"], r["offer_index"]))
    else:  # newest
        rows.sort(key=lambda r: (-(r["created_ts"] or 0), r["offer_index"]))

    page = rows[offset : offset + limit]
    return [dict(r) for r in page]
