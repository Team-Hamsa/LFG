# lfg_core/supply_reconcile.py
# Genesis-growth reconciliation. The only writer of new-edition supply_changes
# growth rows is the live listener (_apply_possible_growth); a mint that lands
# while the listener is down never gets one, leaving the edition outside the
# effective genesis and permanently un-harvestable ("character has no known
# genesis edition"). This sweep writes the missing rows back from the on-chain
# index's stored metadata — the same source the listener would have used.
# Idempotent: an edition already in the effective genesis is never touched.

from __future__ import annotations

import sqlite3
from typing import Any

from lfg_core import economy_store, nft_index, swap_meta, trait_economy

ACTOR = "reconciler"


def reconcile_growth(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    """Write a 'mint' supply_changes row for every live, cleanly-parsing
    character edition missing from the effective genesis. Tokens with
    unreadable metadata (no attributes) are skipped and reported, never
    guessed at — a wrong delta row would corrupt the conservation audit.
    Returns {"written": [editions], "skipped_unreadable": [editions]}."""
    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
    )
    written: list[int] = []
    skipped_unreadable: list[int] = []
    covered = set(genesis.edition_bodies)
    for rec in nft_index.live_nfts(conn):
        edition = rec.nft_number
        if edition is None or edition in covered:
            continue
        try:
            body_value = swap_meta.get_attr(rec.attributes, "Body")
            deltas = {
                f"{slot}|{trait_economy.slot_value(rec, slot)}": 1
                for slot in trait_economy.NON_BODY_SLOTS
            }
            body_class = swap_meta.detect_body(rec.attributes) or rec.body
        except Exception:
            # Malformed stored attribute entries (e.g. missing keys) read as
            # unreadable — report, never guess, never abort the sweep.
            body_value = None
        if not rec.attributes or not body_value:
            skipped_unreadable.append(edition)
            continue
        covered.add(edition)
        if not dry_run:
            economy_store.record_supply_change(
                conn,
                "mint",
                edition,
                body_value,
                body_class,
                deltas,
                ACTOR,
                f"growth reconcile {rec.nft_id}",
            )
        written.append(edition)
    return {"written": sorted(written), "skipped_unreadable": sorted(set(skipped_unreadable))}
