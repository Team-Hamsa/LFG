# lfg_core/traits.py
# Rarity-weighted trait selection from the unified layer store (used by the
# webapp mint flow). The classic bot's directory-based helpers live in
# main.py. Weights come from lfg_core.rarity (proportional-with-floor).

import random
import sqlite3
from datetime import datetime
from typing import Any

from lfg_core import rarity, trait_config
from lfg_core.swap_meta import TRAIT_ORDER, get_attr


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
            raw_values = await store.list_values(body, trait_type)
            values = [
                v
                for v in raw_values
                if cfg.value_allowed(body, trait_type, v)
                and not cfg.conflicts(attributes, trait_type, v)
            ]
            if raw_values and not values:
                # The layer exists on this body but rules (affinity/conflict)
                # eliminated every candidate — that's an over-constrained rule
                # set, not missing coverage, so fail loud instead of silently
                # dropping the layer from the minted attributes.
                raise ValueError(f"trait rules leave no legal {trait_type} value for body '{body}'")
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


# Face slots auto-rolled for legacy apes the first time they pass through the
# Trait Swapper (#168). Ape-only: other bodies have no face art. Order follows
# TRAIT_ORDER so earlier rolls constrain later ones via cfg.conflicts.
FACE_TRAITS = [t for t in TRAIT_ORDER if t in ("Mouth", "Eyebrows", "Eyes")]


async def fill_missing_face_traits(
    store: Any,
    body: str | None,
    attributes: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection | None = None,
    network: str | None = None,
    now: datetime | None = None,
    rng: Any = random,
) -> bool:
    """Roll a rarity-weighted value into every empty ('None'/''/missing) face
    slot of an ape's attribute list, in place. Same candidate filtering and
    weighted_pick as select_random_attributes, so armed boosts/floors apply
    identically to mint. Returns True if anything was rolled. No-op for
    non-ape bodies. Raises ValueError if rules eliminate every candidate for
    a slot that has values (over-constrained config — fail loud, like mint)."""
    if body != "ape":
        return False
    empty = [t for t in FACE_TRAITS if (get_attr(attributes, t) or "None") in ("", "None")]
    if not empty:
        return False
    own_conn = conn is None
    if own_conn:
        conn = rarity.connect()
    assert conn is not None
    rolled = False
    try:
        cfg = trait_config.get_config()
        for trait_type in empty:
            raw_values = await store.list_values(body, trait_type)
            candidates = [
                v
                for v in raw_values
                if v != "None"
                and cfg.value_allowed(body, trait_type, v)
                and not cfg.conflicts(attributes, trait_type, v)
            ]
            if not candidates:
                if [v for v in raw_values if v != "None"]:
                    raise ValueError(
                        f"trait rules leave no legal {trait_type} value for body '{body}'"
                    )
                continue  # layer genuinely absent on this body: leave None
            value = rarity.weighted_pick(
                conn, body, trait_type, candidates, network=network, now=now, rng=rng
            )
            for a in attributes:
                if a["trait_type"] == trait_type:
                    a["value"] = value
                    break
            else:
                attributes.append({"trait_type": trait_type, "value": value})
            rolled = True
    finally:
        if own_conn:
            conn.close()
    return rolled
