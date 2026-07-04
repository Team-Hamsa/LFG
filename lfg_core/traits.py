# lfg_core/traits.py
# Rarity-weighted trait selection from the unified layer store (used by the
# webapp mint flow). The classic bot's directory-based helpers live in
# main.py. Weights come from lfg_core.rarity (proportional-with-floor).

import random
import sqlite3
from datetime import datetime
from typing import Any

from lfg_core import rarity, trait_config
from lfg_core.swap_meta import TRAIT_ORDER


async def select_random_attributes(
    store: Any,
    body: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    network: str | None = None,
    now: datetime | None = None,
    rng: Any = random,
) -> tuple[str, list[dict[str, str]]]:
    """Pick a body (rarity-weighted unless given) and one rarity-weighted
    value per trait type from the unified layer store. Returns
    (body, attributes) where attributes is a metadata-style
    [{trait_type, value}] list in layer order."""
    own_conn = conn is None
    if own_conn:
        conn = rarity.connect()
    assert conn is not None
    try:
        if body is None:
            bodies = await store.list_bodies()
            if not bodies:
                raise ValueError("Layer store has no body directories")
            body = rarity.weighted_pick(
                conn,
                rarity.BODY_SENTINEL,
                rarity.BODY_CATEGORY,
                bodies,
                network=network,
                now=now,
                rng=rng,
            )
        attributes: list[dict[str, str]] = []
        cfg = trait_config.get_config()
        # Layers added only to trait_config.yaml won't mint until TRAIT_ORDER is updated too.
        # The parity test (test_default_config_parity_with_legacy_constants) fails on divergence.
        for trait_type in TRAIT_ORDER:
            values = await store.list_values(body, trait_type)
            values = [
                v
                for v in values
                if cfg.value_allowed(body, trait_type, v)
                and not cfg.conflicts(attributes, trait_type, v)
            ]
            if values:
                value = rarity.weighted_pick(
                    conn, body, trait_type, values, network=network, now=now, rng=rng
                )
                attributes.append({"trait_type": trait_type, "value": value})
        if not attributes:
            raise ValueError(f"No trait layers found for body '{body}'")
        return body, attributes
    finally:
        if own_conn:
            conn.close()
