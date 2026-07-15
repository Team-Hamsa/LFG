"""Trait Shop pricing + overrides + derived catalog (#217).

The catalog is DERIVED: every rarity-enabled (slot, value), aggregated across
bodies (trait tokens are body-agnostic), minus shop_overrides exclusions.
Price uses the same Laplace smoothing as rarity.effective_weight:
    share = (Σ live_count + shop_count + 1) / (Σ category_total + population)
    price = clamp(round(SHOP_BASE_BRIX / share), SHOP_MIN_BRIX, SHOP_MAX_BRIX)
Lives in the app DB next to trait_rarity (same network-column pattern).
Body Type is not sellable: rows under rarity.BODY_CATEGORY are never cataloged.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import config
from .rarity import BODY_CATEGORY

_UNSET: Any = object()

_SCHEMA = """CREATE TABLE IF NOT EXISTS shop_overrides (
    network        TEXT NOT NULL,
    slot           TEXT NOT NULL,
    value          TEXT NOT NULL,
    excluded       INTEGER NOT NULL DEFAULT 0,
    price_override INTEGER,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (network, slot, value)
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def derived_price(
    live_total: int, category_total: int, shop_count: int, population_size: int
) -> int:
    """Apply the Laplace-smoothed price formula.

    share = (live_total + shop_count + 1) / (category_total + population_size)
    price = clamp(round(SHOP_BASE_BRIX / share), SHOP_MIN_BRIX, SHOP_MAX_BRIX)
    """
    share = (live_total + shop_count + 1) / (category_total + population_size)
    price = round(config.SHOP_BASE_BRIX / share)
    return max(config.SHOP_MIN_BRIX, min(config.SHOP_MAX_BRIX, price))


def _rarity_aggregate(
    conn: sqlite3.Connection, network: str, slot: str, value: str
) -> tuple[int, int, int, int, bool] | None:
    """(live_total, category_total, shop_count, population, any_enabled) for a
    trait aggregated across bodies; None if the trait has no rows."""
    row = conn.execute(
        "SELECT SUM(live_count), MAX(shop_count), MAX(enabled) FROM trait_rarity"
        " WHERE network=? AND category=? AND trait=?",
        (network, slot, value),
    ).fetchone()
    if row is None or row[2] is None:
        return None
    cat = conn.execute(
        "SELECT SUM(live_count), COUNT(*) FROM trait_rarity WHERE network=? AND category=?",
        (network, slot),
    ).fetchone()
    return (row[0] or 0, cat[0] or 0, row[1] or 0, cat[1] or 0, bool(row[2]))


def quote(conn: sqlite3.Connection, network: str, slot: str, value: str) -> int | None:
    """Live price for one trait; None if rarity-disabled, unknown, or excluded.

    price_override takes precedence over the formula.
    """
    ensure_schema(conn)
    ov = conn.execute(
        "SELECT excluded, price_override FROM shop_overrides"
        " WHERE network=? AND slot=? AND value=?",
        (network, slot, value),
    ).fetchone()
    if ov and ov[0]:
        return None
    agg = _rarity_aggregate(conn, network, slot, value)
    if agg is None or not agg[4] or slot == BODY_CATEGORY:
        return None
    if ov and ov[1] is not None:
        return int(ov[1])
    live_total, category_total, shop_count, population, _ = agg
    return derived_price(live_total, category_total, shop_count, population)


def set_override(
    conn: sqlite3.Connection,
    network: str,
    slot: str,
    value: str,
    *,
    excluded: bool | None = None,
    price_override: int | None = _UNSET,
) -> None:
    """Upsert one override; unspecified fields keep their stored value."""
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO shop_overrides (network, slot, value) VALUES (?,?,?)"
        " ON CONFLICT(network, slot, value) DO NOTHING",
        (network, slot, value),
    )
    if excluded is not None:
        conn.execute(
            "UPDATE shop_overrides SET excluded=?, updated_at=CURRENT_TIMESTAMP"
            " WHERE network=? AND slot=? AND value=?",
            (1 if excluded else 0, network, slot, value),
        )
    if price_override is not _UNSET:
        conn.execute(
            "UPDATE shop_overrides SET price_override=?, updated_at=CURRENT_TIMESTAMP"
            " WHERE network=? AND slot=? AND value=?",
            (price_override, network, slot, value),
        )
    conn.commit()


def get_overrides(conn: sqlite3.Connection, network: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Fetch all overrides for a network as a dict keyed by (slot, value)."""
    ensure_schema(conn)
    return {
        (r[0], r[1]): {"excluded": bool(r[2]), "price_override": r[3]}
        for r in conn.execute(
            "SELECT slot, value, excluded, price_override FROM shop_overrides WHERE network=?",
            (network,),
        )
    }


def catalog(conn: sqlite3.Connection, network: str) -> list[dict[str, Any]]:
    """Return every enabled, non-excluded (slot, value) with price.

    Aggregated across bodies (trait tokens are body-agnostic).
    Body Type is excluded from the catalog.
    """
    ensure_schema(conn)
    out: list[dict[str, Any]] = []
    pairs = conn.execute(
        "SELECT DISTINCT category, trait FROM trait_rarity"
        " WHERE network=? AND enabled=1 AND category != ?"
        " ORDER BY category, trait",
        (network, BODY_CATEGORY),
    ).fetchall()
    for slot, value in pairs:
        price = quote(conn, network, slot, value)
        if price is not None:
            out.append({"slot": slot, "value": value, "price_brix": price})
    return out
