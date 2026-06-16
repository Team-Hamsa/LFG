# lfg_core/rarity.py
# Variable rarity engine: proportional-with-floor trait weights cached in
# the trait_rarity table, with a dormant-then-stepped boost for new traits.
# Pure sqlite3 + stdlib; time and randomness are injectable for tests.
# Spec: docs/superpowers/specs/2026-06-12-variable-rarity-engine-design.md

import random as _random
import sqlite3
from datetime import datetime, timezone
from typing import Any

from lfg_core import config

BODY_SENTINEL = "*"  # legacy/ungendered rows and Body Type rows
BODY_CATEGORY = "Body Type"  # reserved category weighting the body pick

# trait_rarity.category uses layer-store trait-type names (TRAIT_ORDER);
# the LFG table's headwear column is named Hat (layer tree uses Head).
LFG_COLUMN_FOR_CATEGORY = {
    "Background": "Background",
    "Back": "Back",
    "Body": "Body",
    "Clothing": "Clothing",
    "Mouth": "Mouth",
    "Eyebrows": "Eyebrows",
    "Eyes": "Eyes",
    "Head": "Hat",
    "Accessory": "Accessory",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trait_rarity (
    network          TEXT NOT NULL DEFAULT 'mainnet',
    body             TEXT NOT NULL,
    category         TEXT NOT NULL,
    trait            TEXT NOT NULL,
    live_count       INTEGER NOT NULL DEFAULT 0,
    floor_weight     REAL NOT NULL DEFAULT 0.005,
    boost_initial    REAL,
    boost_step_hours INTEGER DEFAULT 24,
    boost_started_at TIMESTAMP,
    enabled          INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (network, body, category, trait)
)
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or config.DB_PATH)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create trait_rarity and add network/body_type columns to LFG. Idempotent.

    Note: the LFG table already has a `Body` column (body trait value e.g.
    "Straight Dark"). The new `body_type` column stores the body class
    (male/female/skeleton/ape). Using a distinct name avoids SQLite's
    case-insensitive column name handling.
    """
    conn.execute(_SCHEMA)
    # Create burned_nfts if absent (older DBs may not have it yet)
    conn.execute("""CREATE TABLE IF NOT EXISTS burned_nfts (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        burned_by TEXT, reason TEXT,
        burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        original_mint_time TIMESTAMP)""")
    lfg_cols = {r[1] for r in conn.execute("PRAGMA table_info(LFG)")}
    if lfg_cols:  # LFG may not exist yet on a fresh DB; init_db owns it
        if "network" not in lfg_cols:
            conn.execute("ALTER TABLE LFG ADD COLUMN network TEXT NOT NULL DEFAULT 'mainnet'")
        if "body_type" not in lfg_cols:
            conn.execute("ALTER TABLE LFG ADD COLUMN body_type TEXT NOT NULL DEFAULT '*'")
    conn.commit()


def boost_multiplier(
    boost_initial: float | None,
    boost_step_hours: int | None,
    boost_started_at: str | None,
    now: datetime,
) -> float:
    """Stepped decay: boost_initial for the first window, −1 per window,
    never below 1. Dormant (clock unstarted) or unconfigured → 1."""
    if not boost_initial or not boost_started_at:
        return 1.0
    started = datetime.fromisoformat(boost_started_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    hours = (now - started).total_seconds() / 3600.0
    windows = int(hours // (boost_step_hours or 24))
    return max(1.0, boost_initial - windows)


def effective_weight(
    live_count: int,
    category_total: int,
    floor_weight: float,
    boost_initial: float | None,
    boost_step_hours: int | None,
    boost_started_at: str | None,
    now: datetime,
) -> float:
    """weight = max(live_share, floor) × boost multiplier. Relative weight,
    not a normalized probability."""
    share = (live_count / category_total) if category_total else 0.0
    base = max(share, floor_weight)
    return base * boost_multiplier(boost_initial, boost_step_hours, boost_started_at, now)


def _ensure_rows(
    conn: sqlite3.Connection,
    network: str,
    body: str,
    category: str,
    available: list[str],
    now: datetime,
) -> None:
    """Auto-detect: insert floor-weight rows for traits the engine hasn't
    seen (e.g. a PNG just dropped into the layer store). No boost."""
    for trait in available:
        conn.execute(
            """INSERT OR IGNORE INTO trait_rarity
               (network, body, category, trait, live_count, floor_weight,
                first_seen_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (network, body, category, trait, config.RARITY_FLOOR, now.isoformat()),
        )
    conn.commit()


def weighted_pick(
    conn: sqlite3.Connection,
    body: str,
    category: str,
    available: list[str],
    *,
    network: str | None = None,
    now: datetime | None = None,
    rng: Any = _random,
) -> str:
    """Pick one trait from `available` (the values that exist in the layer
    store — the store stays the authority on what's mintable) using
    proportional-with-floor × boost weights from trait_rarity."""
    if not available:
        raise ValueError(f"No traits available for {body}/{category}")
    network = network or config.XRPL_NETWORK
    now = now or utcnow()
    ensure_schema(conn)
    _ensure_rows(conn, network, body, category, available, now)
    if _is_stale(conn, network, category):
        recalculate_rarity(conn, network=network)

    placeholders = ",".join("?" * len(available))
    rows = conn.execute(
        f"""SELECT trait, live_count, floor_weight, boost_initial,
                   boost_step_hours, boost_started_at
            FROM trait_rarity
            WHERE network=? AND body=? AND category=? AND enabled=1
              AND trait IN ({placeholders})""",
        (network, body, category, *available),
    ).fetchall()
    if not rows:
        raise ValueError(f"All traits disabled for {body}/{category} on {network}")

    total = sum(r[1] for r in rows)
    traits = [r[0] for r in rows]
    weights = [effective_weight(r[1], total, r[2], r[3], r[4], r[5], now) for r in rows]
    return rng.choices(traits, weights=weights, k=1)[0]  # type: ignore[no-any-return]


def _live_where(network: str) -> tuple[str, tuple[str, ...]]:
    """WHERE fragment selecting live (unburned) LFG rows for a network."""
    return (
        """network=? AND nft_number NOT IN
               (SELECT nft_number FROM burned_nfts)""",
        (network,),
    )


def recalculate_rarity(conn: sqlite3.Connection, network: str | None = None) -> None:
    """Recount live_count for every (body_type, category, trait) from the LFG
    table minus burned_nfts, plus the reserved Body Type category. Upserts
    counts; preserves boost/floor/enabled columns; zeroes traits that no
    longer occur. Cheap (GROUP BY over a few thousand rows).

    Note: LFG.body_type stores the body class (male/female/skeleton/ape).
    LFG.Body (capitalized) stores the body trait value (e.g. Straight Dark).
    SQLite is case-insensitive for column names, hence the distinct name.
    """
    network = network or config.XRPL_NETWORK
    ensure_schema(conn)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='LFG'").fetchone():
        return  # LFG table not yet created; nothing to recount
    where, params = _live_where(network)

    conn.execute("UPDATE trait_rarity SET live_count=0 WHERE network=?", (network,))
    upsert = """INSERT INTO trait_rarity
                (network, body, category, trait, live_count, floor_weight)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(network, body, category, trait)
                DO UPDATE SET live_count=excluded.live_count"""
    for category, column in LFG_COLUMN_FOR_CATEGORY.items():
        rows = conn.execute(
            f"""SELECT body_type, "{column}", COUNT(*) FROM LFG
                WHERE {where} AND "{column}" != '' AND "{column}" IS NOT NULL
                GROUP BY body_type, "{column}" """,
            params,
        ).fetchall()
        for body, trait, count in rows:
            conn.execute(
                upsert,
                (network, body or BODY_SENTINEL, category, trait, count, config.RARITY_FLOOR),
            )
    body_rows = conn.execute(
        f"SELECT body_type, COUNT(*) FROM LFG WHERE {where} GROUP BY body_type", params
    ).fetchall()
    for body, count in body_rows:
        if body and body != BODY_SENTINEL:
            conn.execute(
                upsert, (network, BODY_SENTINEL, BODY_CATEGORY, body, count, config.RARITY_FLOOR)
            )
    conn.commit()


FOLDER_CATEGORY = {
    "background": "Background",
    "back": "Back",
    "body": "Body",
    "clothing": "Clothing",
    "mouth": "Mouth",
    "eyebrows": "Eyebrows",
    "eyes": "Eyes",
    "hat:hair": "Head",
    "accessory": "Accessory",
}


def category_for_folder(folder_name: str) -> str | None:
    """Map a legacy trait_layers folder name ('8 hat:hair') to a rarity
    category ('Head'). None if unrecognized."""
    import re

    name = re.sub(r"^\d+\s*", "", folder_name).strip().lower()
    return FOLDER_CATEGORY.get(name)


def arm_boost(
    conn: sqlite3.Connection,
    body: str,
    category: str,
    trait: str,
    *,
    network: str | None = None,
    boost_initial: float | None = None,
    boost_step_hours: int | None = None,
) -> None:
    """Admin opt-in: configure a dormant boost. Resets the clock, so it also
    re-arms a finished boost (comeback event)."""
    network = network or config.XRPL_NETWORK
    cur = conn.execute(
        """UPDATE trait_rarity
           SET boost_initial=?, boost_step_hours=?, boost_started_at=NULL
           WHERE network=? AND body=? AND category=? AND trait=?""",
        (
            boost_initial if boost_initial is not None else config.RARITY_BOOST_INITIAL,
            boost_step_hours if boost_step_hours is not None else config.RARITY_BOOST_STEP_HOURS,
            network,
            body,
            category,
            trait,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"No trait_rarity row for {network}/{body}/{category}/{trait}")


def start_boost_clock(
    conn: sqlite3.Connection,
    body: str,
    category: str,
    trait: str,
    *,
    network: str | None = None,
    now: datetime | None = None,
) -> None:
    """Called when a mint completes: if the picked trait has an armed,
    dormant boost, start its clock. No-op otherwise."""
    network = network or config.XRPL_NETWORK
    now = now or utcnow()
    conn.execute(
        """UPDATE trait_rarity SET boost_started_at=?
           WHERE network=? AND body=? AND category=? AND trait=?
             AND boost_initial IS NOT NULL AND boost_started_at IS NULL""",
        (now.isoformat(), network, body, category, trait),
    )
    conn.commit()


def boost_status(
    boost_initial: float | None,
    boost_step_hours: int | None,
    boost_started_at: str | None,
    now: datetime,
) -> str:
    """Human-readable boost state for admin views."""
    if not boost_initial:
        return "—"
    if not boost_started_at:
        return "dormant"
    mult = boost_multiplier(boost_initial, boost_step_hours, boost_started_at, now)
    if mult <= 1.0:
        return "finished"
    started = datetime.fromisoformat(boost_started_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    step = boost_step_hours or 24
    total_h = (boost_initial - 1) * step
    left_h = total_h - (now - started).total_seconds() / 3600.0
    return f"active {mult:g}x — {left_h / 24:.1f}d left"


def _is_stale(conn: sqlite3.Connection, network: str, category: str) -> bool:
    """True when cached category counts disagree with the live collection."""
    column = LFG_COLUMN_FOR_CATEGORY.get(category)
    if column is None:
        return False  # Body Type and unknown categories: recalc handles them
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='LFG'").fetchone():
        return False  # LFG table not yet created; nothing to be stale about
    (cached,) = conn.execute(
        """SELECT COALESCE(SUM(live_count), 0) FROM trait_rarity
           WHERE network=? AND category=?""",
        (network, category),
    ).fetchone()
    where, params = _live_where(network)
    (actual,) = conn.execute(
        f"""SELECT COUNT(*) FROM LFG WHERE {where}
            AND "{column}" != '' AND "{column}" IS NOT NULL""",
        params,
    ).fetchone()
    return cached != actual  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Admin functions (CLI + Discord /admin)
# ---------------------------------------------------------------------------


def seed_from_collection(
    conn: sqlite3.Connection,
    network: str | None = None,
    mark_testnet: list[int] | None = None,
    layer_values: dict[str, dict[str, list[str]]] | None = None,
) -> None:
    """Bootstrap: optionally mark known test mints as testnet, backfill
    LFG.body_type from the stored Body trait value, register any layer-store
    values, then full recount."""
    from lfg_core.swap_meta import detect_body

    network = network or config.XRPL_NETWORK
    ensure_schema(conn)
    if mark_testnet:
        qs = ",".join("?" * len(mark_testnet))
        conn.execute(
            f"UPDATE LFG SET network='testnet' WHERE nft_number IN ({qs})", list(mark_testnet)
        )
    rows = conn.execute(
        "SELECT nft_number, Body FROM LFG WHERE body_type=?", (BODY_SENTINEL,)
    ).fetchall()
    for number, body_trait in rows:
        conn.execute(
            "UPDATE LFG SET body_type=? WHERE nft_number=?",
            (detect_body([{"trait_type": "Body", "value": body_trait or ""}]), number),
        )
    now = utcnow()
    for body, categories in (layer_values or {}).items():
        for category, values in categories.items():
            _ensure_rows(conn, network, body, category, values, now)
    conn.commit()
    recalculate_rarity(conn, network=network)


def set_floor(
    conn: sqlite3.Connection,
    floor: float,
    *,
    network: str | None = None,
    body: str | None = None,
    category: str | None = None,
    trait: str | None = None,
) -> None:
    """Set floor_weight globally for a network, or for one specific trait."""
    network = network or config.XRPL_NETWORK
    if trait is not None:
        conn.execute(
            """UPDATE trait_rarity SET floor_weight=?
               WHERE network=? AND body=? AND category=? AND trait=?""",
            (floor, network, body, category, trait),
        )
    else:
        conn.execute("UPDATE trait_rarity SET floor_weight=? WHERE network=?", (floor, network))
    conn.commit()


def set_enabled(
    conn: sqlite3.Connection,
    body: str,
    category: str,
    trait: str,
    enabled: bool | int,
    *,
    network: str | None = None,
) -> None:
    network = network or config.XRPL_NETWORK
    conn.execute(
        """UPDATE trait_rarity SET enabled=?
           WHERE network=? AND body=? AND category=? AND trait=?""",
        (1 if enabled else 0, network, body, category, trait),
    )
    conn.commit()


def get_odds(
    conn: sqlite3.Connection,
    body: str,
    category: str,
    *,
    network: str | None = None,
    now: datetime | None = None,
) -> list[tuple[str, int, float, float, str]]:
    """Rows for admin display: (trait, live_count, share%, weight, status)
    sorted by effective weight descending."""
    network = network or config.XRPL_NETWORK
    now = now or utcnow()
    rows = conn.execute(
        """SELECT trait, live_count, floor_weight, boost_initial,
                  boost_step_hours, boost_started_at, enabled
           FROM trait_rarity WHERE network=? AND body=? AND category=?""",
        (network, body, category),
    ).fetchall()
    total = sum(r[1] for r in rows)
    out: list[tuple[str, int, float, float, str]] = []
    for trait, count, floor, bi, bs, bsa, enabled in rows:
        share = (count / total * 100) if total else 0.0
        weight = effective_weight(count, total, floor, bi, bs, bsa, now)
        status = "disabled" if not enabled else boost_status(bi, bs, bsa, now)
        out.append((trait, count, share, weight, status))
    return sorted(out, key=lambda r: -r[3])
