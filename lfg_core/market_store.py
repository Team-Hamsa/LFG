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
from decimal import Decimal
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
    amount_drops  INTEGER,            -- XRP price (kind='character'); NULL for BRIX rows (#239)
    destination   TEXT,               -- non-NULL ⇒ hidden from browse
    slot          TEXT,               -- trait kind only (denormalized)
    value         TEXT,               -- trait kind only (denormalized)
    created_ledger INTEGER,
    created_ts    INTEGER,
    is_live       INTEGER NOT NULL DEFAULT 1,
    closed_reason TEXT,               -- sold | cancelled | stale
    settled       INTEGER,            -- trait kind: 0=burn-back pending, 1=done; NULL for characters
    buyer         TEXT,                -- sold kind: durable buyer-of-record for settlement recovery; NULL otherwise
    amount_brix   TEXT                 -- BRIX price (kind='trait', #239); exactly one of amount_drops/amount_brix is non-NULL
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
    amount_drops: int | None = None
    amount_brix: str | None = None  # #239: BRIX price for trait listings
    destination: str | None = None
    slot: str | None = None
    value: str | None = None
    created_ledger: int | None = None
    created_ts: int | None = None
    is_live: int = 1
    closed_reason: str | None = None
    settled: int | None = None


def _check_exactly_one_amount(listing: MarketListing) -> None:
    """#239 invariant: every row carries exactly one denomination — XRP drops
    (characters) or a BRIX value string (trait listings), never both/neither."""
    if (listing.amount_drops is None) == (listing.amount_brix is None):
        raise ValueError(
            "exactly one of amount_drops/amount_brix must be set "
            f"(got drops={listing.amount_drops!r}, brix={listing.amount_brix!r})"
        )


def init_db(conn: sqlite3.Connection) -> None:
    """Create the market_listings table + index if absent. Idempotent —
    calling this twice on the same connection is a no-op the second time.

    Also runs forward-only migrations: `buyer` and `amount_brix` (#239) were
    added after the initial schema shipped, and `CREATE TABLE IF NOT EXISTS`
    will not add a column to an already-created table, so ADD them when a
    pre-existing DB lacks them. #239 additionally relaxed amount_drops from
    NOT NULL (a BRIX trait row carries no drops); SQLite cannot drop a NOT
    NULL constraint in place, so a pre-#239 table is rebuilt once
    (rename -> copy -> drop), preserving every row verbatim."""
    conn.executescript(_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(market_listings)")}
    if "buyer" not in cols:
        conn.execute("ALTER TABLE market_listings ADD COLUMN buyer TEXT")
    if "amount_brix" not in cols:
        conn.execute("ALTER TABLE market_listings ADD COLUMN amount_brix TEXT")
    drops_not_null = any(
        row[1] == "amount_drops" and row[3]
        for row in conn.execute("PRAGMA table_info(market_listings)")
    )
    if drops_not_null:
        column_list = (
            "offer_index, nft_id, kind, seller, amount_drops, destination, slot, value, "
            "created_ledger, created_ts, is_live, closed_reason, settled, buyer, amount_brix"
        )
        conn.execute("ALTER TABLE market_listings RENAME TO _market_listings_migrate")
        # The rename carried idx_market_live along with the old table, so the
        # fresh CREATE INDEX IF NOT EXISTS is skipped until the old table (and
        # its index) is dropped — recreate via a second _SCHEMA pass below.
        conn.executescript(_SCHEMA)
        conn.execute(
            f"INSERT INTO market_listings ({column_list}) "
            f"SELECT {column_list} FROM _market_listings_migrate"
        )
        conn.execute("DROP TABLE _market_listings_migrate")
        conn.executescript(_SCHEMA)
    conn.commit()


def upsert_listing(conn: sqlite3.Connection, listing: MarketListing) -> None:
    """Insert a listing or overwrite it in place (keyed on offer_index) —
    used by the listener's offer_create handler and by the backfill rebuild.
    Raises ValueError for an unrecognized `kind`.

    `created_ledger`/`created_ts` COALESCE on conflict: they are immutable
    creation facts only the listener (which sees the offer_create tx) knows;
    the backfill re-confirming a live offer passes None for them, and a
    plain overwrite would permanently wipe the listener-written values
    (nothing ever repopulates them), silently degrading sort=newest. Every
    other field overwrites: an NFTokenOffer ledger object is immutable, so
    for a given offer_index the incoming values are either identical or a
    correction, and is_live/closed_reason/settled must overwrite so a
    falsely-staled row can be resurrected when a later sweep re-confirms
    the offer on-ledger."""
    if listing.kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind: {listing.kind!r}")
    _check_exactly_one_amount(listing)
    conn.execute(
        """
        INSERT INTO market_listings
            (offer_index, nft_id, kind, seller, amount_drops, amount_brix, destination,
             slot, value, created_ledger, created_ts, is_live, closed_reason, settled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(offer_index) DO UPDATE SET
            nft_id=excluded.nft_id,
            kind=excluded.kind,
            seller=excluded.seller,
            amount_drops=excluded.amount_drops,
            amount_brix=excluded.amount_brix,
            destination=excluded.destination,
            slot=excluded.slot,
            value=excluded.value,
            created_ledger=COALESCE(excluded.created_ledger, market_listings.created_ledger),
            created_ts=COALESCE(excluded.created_ts, market_listings.created_ts),
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
            listing.amount_brix,
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


def record_listing_creation(conn: sqlite3.Connection, listing: MarketListing) -> None:
    """Creation-only write for the service *finalize* paths (the list status
    handler + the trait-sell wizard), NOT the listener/backfill.

    The finalize poll only knows the offer's creation-time facts — the same
    kind/slot/value/seller/amount the listener writes from the offer_create tx
    on-ledger. But it can land at any time relative to the listener, including
    long AFTER a buyer has already purchased and the listener has closed the
    row (is_live=0, closed_reason='sold', settled=0). A full overwrite
    (upsert_listing) would resurrect that dead row — re-listing a sold token in
    browse AND breaking the settlement sweep's `closed_reason='sold' AND
    settled=0` predicate so the buyer's paid-for trait is never deposited.

    So this INSERTs a fresh live row when the offer_index is unseen (finalize
    raced ahead of the listener — the row must still exist, live, with
    kind/slot/value) and does NOTHING on conflict: whatever lifecycle state a
    prior listener/backfill/finalize write established is authoritative and
    must survive a late finalize poll. Raises ValueError for an unknown `kind`.

    The listener's own echo of the offer_create still uses the full-overwrite
    upsert_listing, so a finalize-then-listener ordering converges on fresh
    on-ledger truth as before."""
    if listing.kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind: {listing.kind!r}")
    _check_exactly_one_amount(listing)
    conn.execute(
        """
        INSERT INTO market_listings
            (offer_index, nft_id, kind, seller, amount_drops, amount_brix, destination,
             slot, value, created_ledger, created_ts, is_live, closed_reason, settled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(offer_index) DO NOTHING
        """,
        (
            listing.offer_index,
            listing.nft_id,
            listing.kind,
            listing.seller,
            listing.amount_drops,
            listing.amount_brix,
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


def close_listing(
    conn: sqlite3.Connection, offer_index: str, reason: str, buyer: str | None = None
) -> None:
    """Mark a listing no-longer-live. `reason` must be one of
    sold|cancelled|stale. Per the spec, closing a *trait* listing with
    reason='sold' also sets settled=0 (burn-back-to-Closet pending) in the
    same statement — every other case leaves `settled` untouched (NULL for
    characters, or whatever it already was).

    `buyer` (when given) is persisted as the durable buyer-of-record so the
    settlement sweep can still resolve who to credit even after `run_deposit`
    deletes the token's `trait_tokens` ownership row mid-settlement. Passed
    only on a sold close (the new owner); COALESCE keeps any previously
    recorded buyer if a later close passes None."""
    if reason not in _VALID_CLOSE_REASONS:
        raise ValueError(f"unknown close reason: {reason!r}")
    conn.execute(
        """
        UPDATE market_listings
        SET is_live = 0,
            closed_reason = ?,
            settled = CASE WHEN kind = 'trait' AND ? = 'sold' THEN 0 ELSE settled END,
            buyer = COALESCE(?, buyer)
        WHERE offer_index = ?
        """,
        (reason, reason, buyer, offer_index),
    )
    conn.commit()


def mark_settled(conn: sqlite3.Connection, offer_index: str) -> bool:
    """Flip a sold trait listing's settled flag to 1 (burn-back-to-Closet
    done) — called after `run_deposit` succeeds on behalf of the buyer.

    Guarded to kind='trait': `settled` is a trait-only lifecycle (NULL for
    characters, per the spec), so a character row is never touched. Returns
    True when a row was actually settled, False for a nonexistent or
    character offer_index — a safe explicit outcome, never a silent success."""
    cur = conn.execute(
        "UPDATE market_listings SET settled = 1 WHERE offer_index = ? AND kind = 'trait'",
        (offer_index,),
    )
    conn.commit()
    return cur.rowcount > 0


def unsettled_trait_sales(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Sold trait listings still awaiting settlement — the settlement sweep's
    worklist (backstop for the primary buy-status-handler trigger)."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM market_listings WHERE kind='trait' AND closed_reason='sold' AND settled=0"
    )
    return [dict(row) for row in cur.fetchall()]


def live_listing_for_nft(conn: sqlite3.Connection, nft_id: str) -> dict[str, Any] | None:
    """The live listing row for `nft_id`, if one exists — Task 8's list-start
    dedup check (409 when a listing is already live for this token). Ignores
    `destination`/ownership joins (browse's concerns); a listing being live
    in market_listings is the only fact this check needs."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM market_listings WHERE nft_id = ? AND is_live = 1", (nft_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_listing(conn: sqlite3.Connection, offer_index: str) -> dict[str, Any] | None:
    """A listing row by its offer_index (primary key), live or not — Task 8's
    cancel/buy lookup. Returns None only when no such offer_index was ever
    written (never listed); a closed listing still returns its row (with
    is_live=0) so callers can distinguish 'unknown' (404) from 'dead' (410)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM market_listings WHERE offer_index = ?", (offer_index,)
    ).fetchone()
    return dict(row) if row is not None else None


def listing_price(row: Any) -> Decimal:
    """A row's price in its own denomination, as a Decimal, for within-kind
    sorting (#239): BRIX value when present, else drops. Browse is per-kind so
    rows are denomination-homogeneous apart from the legacy transition case
    (a live XRP trait row awaiting the backfill's stale-close) — sorting a
    Decimal BRIX against a Decimal drops count is well-defined, just not
    meaningful, and beats crashing on None."""
    brix = row["amount_brix"] if "amount_brix" in row.keys() else None
    if brix is not None:
        return Decimal(brix)
    return Decimal(row["amount_drops"] or 0)


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
    min_amount_brix: str | None = None,
    max_amount_brix: str | None = None,
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
    set. Raises ValueError for an unrecognized `kind` or `sort`, or a
    negative `limit`/`offset` (a negative value silently produces a nonsense
    Python slice — wrap-around paging — instead of erroring).
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    if sort not in _VALID_SORTS:
        raise ValueError(f"unknown sort: {sort!r}")
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")

    rows = _browse_character_rows(conn) if kind == "character" else _browse_trait_rows(conn)

    # Per-kind price filters (#239): drops bounds apply to XRP-denominated
    # rows, BRIX bounds to BRIX-denominated ones. A row lacking the filtered
    # denomination (e.g. a legacy live XRP trait row awaiting the backfill's
    # stale-close) is excluded by that filter rather than crashing on None.
    if min_amount_drops is not None:
        rows = [
            r
            for r in rows
            if r["amount_drops"] is not None and r["amount_drops"] >= min_amount_drops
        ]
    if max_amount_drops is not None:
        rows = [
            r
            for r in rows
            if r["amount_drops"] is not None and r["amount_drops"] <= max_amount_drops
        ]
    if min_amount_brix is not None:
        floor = Decimal(min_amount_brix)
        rows = [
            r for r in rows if r["amount_brix"] is not None and Decimal(r["amount_brix"]) >= floor
        ]
    if max_amount_brix is not None:
        ceiling = Decimal(max_amount_brix)
        rows = [
            r for r in rows if r["amount_brix"] is not None and Decimal(r["amount_brix"]) <= ceiling
        ]

    if trait_filters:
        rows = [r for r in rows if _attributes_match(_row_attrs(r, kind), trait_filters)]

    if sort == "price_asc":
        rows.sort(key=lambda r: (listing_price(r), r["offer_index"]))
    elif sort == "price_desc":
        rows.sort(key=lambda r: (-listing_price(r), r["offer_index"]))
    else:  # newest
        rows.sort(key=lambda r: (-(r["created_ts"] or 0), r["offer_index"]))

    page = rows[offset : offset + limit]
    return [dict(r) for r in page]
