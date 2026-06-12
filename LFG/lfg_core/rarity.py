# lfg_core/rarity.py
# Variable rarity engine: proportional-with-floor trait weights cached in
# the trait_rarity table, with a dormant-then-stepped boost for new traits.
# Pure sqlite3 + stdlib; time and randomness are injectable for tests.
# Spec: docs/superpowers/specs/2026-06-12-variable-rarity-engine-design.md

import logging
import random as _random
import sqlite3
from datetime import datetime, timezone

from lfg_core import config

BODY_SENTINEL = "*"          # legacy/ungendered rows and Body Type rows
BODY_CATEGORY = "Body Type"  # reserved category weighting the body pick

# trait_rarity.category uses layer-store trait-type names (TRAIT_ORDER);
# the LFG table's headwear column is named Hat (layer tree uses Head).
LFG_COLUMN_FOR_CATEGORY = {
    "Background": "Background", "Back": "Back", "Body": "Body",
    "Clothing": "Clothing", "Mouth": "Mouth", "Eyebrows": "Eyebrows",
    "Eyes": "Eyes", "Head": "Hat", "Accessory": "Accessory",
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


def utcnow():
    return datetime.now(timezone.utc)


def connect(db_path=None):
    return sqlite3.connect(db_path or config.DB_PATH)


def ensure_schema(conn):
    """Create trait_rarity and add network/body_type columns to LFG. Idempotent.

    Note: the LFG table already has a `Body` column (body trait value e.g.
    "Straight Dark"). The new `body_type` column stores the body class
    (male/female/skeleton/ape). Using a distinct name avoids SQLite's
    case-insensitive column name handling.
    """
    conn.execute(_SCHEMA)
    lfg_cols = {r[1] for r in conn.execute("PRAGMA table_info(LFG)")}
    if lfg_cols:  # LFG may not exist yet on a fresh DB; init_db owns it
        if "network" not in lfg_cols:
            conn.execute(
                "ALTER TABLE LFG ADD COLUMN network TEXT NOT NULL DEFAULT 'mainnet'")
        if "body_type" not in lfg_cols:
            conn.execute(
                "ALTER TABLE LFG ADD COLUMN body_type TEXT NOT NULL DEFAULT '*'")
    conn.commit()


def boost_multiplier(boost_initial, boost_step_hours, boost_started_at, now):
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


def effective_weight(live_count, category_total, floor_weight,
                     boost_initial, boost_step_hours, boost_started_at, now):
    """weight = max(live_share, floor) × boost multiplier. Relative weight,
    not a normalized probability."""
    share = (live_count / category_total) if category_total else 0.0
    base = max(share, floor_weight)
    return base * boost_multiplier(boost_initial, boost_step_hours,
                                   boost_started_at, now)


def _ensure_rows(conn, network, body, category, available, now):
    """Auto-detect: insert floor-weight rows for traits the engine hasn't
    seen (e.g. a PNG just dropped into the layer store). No boost."""
    for trait in available:
        conn.execute(
            """INSERT OR IGNORE INTO trait_rarity
               (network, body, category, trait, live_count, floor_weight,
                first_seen_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (network, body, category, trait, config.RARITY_FLOOR,
             now.isoformat()))
    conn.commit()


def weighted_pick(conn, body, category, available, *, network=None,
                  now=None, rng=_random):
    """Pick one trait from `available` (the values that exist in the layer
    store — the store stays the authority on what's mintable) using
    proportional-with-floor × boost weights from trait_rarity."""
    if not available:
        raise ValueError(f"No traits available for {body}/{category}")
    network = network or config.XRPL_NETWORK
    now = now or utcnow()
    ensure_schema(conn)
    _ensure_rows(conn, network, body, category, available, now)

    placeholders = ",".join("?" * len(available))
    rows = conn.execute(
        f"""SELECT trait, live_count, floor_weight, boost_initial,
                   boost_step_hours, boost_started_at
            FROM trait_rarity
            WHERE network=? AND body=? AND category=? AND enabled=1
              AND trait IN ({placeholders})""",
        (network, body, category, *available)).fetchall()
    if not rows:
        raise ValueError(
            f"All traits disabled for {body}/{category} on {network}")

    total = sum(r[1] for r in rows)
    traits = [r[0] for r in rows]
    weights = [effective_weight(r[1], total, r[2], r[3], r[4], r[5], now)
               for r in rows]
    return rng.choices(traits, weights=weights, k=1)[0]
