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
    # One canonical token per uncovered edition, by the same rule as
    # dedupe_editions/nft_by_number: prefer mutable, tie-break on highest
    # ledger_index — so duplicate tokens can't pick the deltas arbitrarily.
    canonical: dict[int, nft_index.OnchainNft] = {}
    for rec in nft_index.live_nfts(conn):
        edition = rec.nft_number
        if edition is None or edition in covered:
            continue
        prev = canonical.get(edition)
        if prev is None or (
            (1 if rec.mutable else 0, rec.ledger_index or 0)
            > (1 if prev.mutable else 0, prev.ledger_index or 0)
        ):
            canonical[edition] = rec
    for edition, rec in sorted(canonical.items()):
        try:
            body_value = swap_meta.get_attr(rec.attributes, "Body")
            deltas = {
                f"{slot}|{trait_economy.slot_value(rec, slot)}": 1
                for slot in trait_economy.NON_BODY_SLOTS
            }
            body_class = swap_meta.detect_body(rec.attributes)
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


def reconcile_shrinkage(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    """Write a 'burn' supply_changes row for every historical out-of-band
    character burn the listener never accounted for (#322): a BURNED
    `onchain_nfts` character whose edition is in the effective genesis, that
    was DRESSED at burn time (a blank's assets survive in the Closet — no
    shrinkage), and has no burn row stamped with its nft_id yet. Deltas come
    from the index's preserved attributes via
    `trait_economy.burn_shrinkage_deltas` (+ body compensation when the worn
    body differs from the recorded one). Unreadable rows are skipped and
    reported, never guessed at. Idempotent per burned nft_id.
    Returns {"written": [(edition, nft_id)], "skipped_unreadable": [...]}."""
    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
    )
    written: list[tuple[int, str]] = []
    skipped_unreadable: list[tuple[int, str]] = []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM onchain_nfts WHERE is_burned=1 AND nft_number IS NOT NULL "
        "ORDER BY nft_number, nft_id"
    ).fetchall()
    for row in rows:
        rec = nft_index._row_to_nft(row)
        edition = rec.nft_number
        assert edition is not None  # filtered in SQL
        if edition not in genesis.edition_bodies:
            continue
        # A burned duplicate whose edition is still LIVE (legacy burn+remint
        # swap, reminted edition) is a replacement, not shrinkage — the live
        # token carries the edition's assets; writing -1 would wrongly pop it.
        if nft_index.nft_by_number(conn, edition) is not None:
            continue
        if economy_store.supply_change_exists_for_nft(conn, rec.nft_id):
            continue
        if not rec.attributes:
            skipped_unreadable.append((edition, rec.nft_id))
            continue
        deltas = trait_economy.burn_shrinkage_deltas(rec)
        if deltas is None:  # blank — assets conserved in the Closet
            continue
        deltas.update(trait_economy.body_compensation_deltas(rec, genesis))
        if not dry_run:
            economy_store.record_supply_change(
                conn,
                "burn",
                edition,
                swap_meta.get_attr(rec.attributes, "Body") or "",
                rec.body,
                deltas,
                ACTOR,
                f"shrinkage reconcile {rec.nft_id}",
                nft_id=rec.nft_id,
            )
            # The burn row pops the edition from the effective genesis; refresh
            # so a duplicate token at the same edition is not written twice.
            genesis = trait_economy.effective_genesis(
                economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
            )
        written.append((edition, rec.nft_id))
    return {"written": sorted(written), "skipped_unreadable": sorted(set(skipped_unreadable))}
