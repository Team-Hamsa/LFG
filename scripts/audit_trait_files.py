#!/usr/bin/env python3
"""Definitive reconciliation of every stored trait value against the local
``layers/`` files — the runtime source of truth when ``LAYER_SOURCE=local``.

A swap/mint aborts (fail-safe, before any payment or burn) when a trait value on
a token has no backing layer file: ``swap_flow`` calls
``swap_compose.missing_layers`` and refuses to proceed on any gap
("Missing trait layer files: ..."). This audit reproduces that exact check
across EVERY trait value the app knows about, so a gap can be found and
backfilled before a user ever hits it.

Why a new script (existing audits don't cover this):
  * ``audit_layer_coverage.py`` checks only LIVE on-chain tokens, and via
    ``store.list_values`` (body dir + shared) — it never exercises the swap
    path's cross-body foreign fallback or the ape structural extras.
  * ``audit_body_affinity`` / ``audit_collection_integrity`` are affinity /
    census tools, not a multi-source value->file reconciliation.

This script instead calls the REAL ``swap_compose.missing_layers`` (own dir ->
shared -> matrix-permitted foreign dir, plus ape Nose/Mask assets) for every
character, and an "exists for any body" check for every loose trait, sweeping
four sources:
  * ``LFG`` app table trait columns (maps its legacy ``Hat`` column -> ``Head``).
  * ``onchain_<net>`` ``onchain_nfts`` (live tokens — captures swap/remint
    duplicates the edition-keyed ``LFG`` table cannot represent).
  * ``closet_assets`` + ``trait_tokens`` (loose / tradeable trait values).

Exit code: 0 = clean, 1 = one or more blocking gaps, 2 = required index DB
missing. CI/pre-deploy-ready.

  python scripts/audit_trait_files.py --network testnet
  python scripts/audit_trait_files.py --network mainnet --report-dir reports
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import (  # noqa: E402
    config,
    economy_store,
    layer_store,
    nft_index,
    swap_compose,
    swap_meta,
    trait_config,
)

# LFG app-table trait column -> layer-tree trait_type. The table predates the
# Head rename and still stores that slot as "Hat"; the layer tree / metadata /
# TRAIT_ORDER all use "Head". Every other column name already matches.
LFG_COLUMN_TO_SLOT = {
    "Background": "Background",
    "Back": "Back",
    "Body": "Body",
    "Clothing": "Clothing",
    "Eyes": "Eyes",
    "Eyebrows": "Eyebrows",
    "Mouth": "Mouth",
    "Hat": "Head",
    "Accessory": "Accessory",
}

# How many example refs (editions / nft_ids / owners) to keep per gap.
MAX_REFS = 8


@dataclass
class CharacterRecord:
    """A full character whose layers resolve as a set (body + attributes),
    checked with the real swap-path resolver."""

    source: str  # "LFG" | "onchain"
    ref: str  # edition number or nft_id
    body: str
    attributes: list[dict[str, str]]


@dataclass
class LooseTrait:
    """A single, body-agnostic (slot, value) trait held loose in a Closet or as
    a tradeable trait token. It just needs a backing file under SOME body/shared."""

    source: str  # "closet" | "trait_token"
    owner: str
    slot: str
    value: str
    ref: str = ""  # nft_id for trait tokens


@dataclass
class MissingEntry:
    """One distinct missing asset, aggregated across every reference to it."""

    path: str  # "body/slot/value", "body/Asset.png" (structural), or "slot/value" (loose)
    slot: str
    value: str
    kind: str  # "character" | "structural" | "loose"
    missing_bodies: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    refs: list[str] = field(default_factory=list)
    count: int = 0
    resolved_bodies: list[str] = field(default_factory=list)  # bodies where the file DOES exist

    def add_ref(self, ref: str) -> None:
        self.count += 1
        if ref and ref not in self.refs and len(self.refs) < MAX_REFS:
            self.refs.append(ref)


@dataclass
class AuditResult:
    missing: list[MissingEntry]
    unreadable: list[str]  # onchain nft_ids with no cached metadata (couldn't be checked)
    character_count: int
    loose_count: int

    @property
    def ok(self) -> bool:
        return not self.missing


def _parse_missing_path(path: str) -> tuple[str, str, str, str]:
    """Split a ``missing_layers`` path into (body, slot, value, kind).

    Character gaps are "body/slot/value"; the ape structural extras are
    "body/Asset.png" (two segments) — surfaced as kind "structural"."""
    parts = path.split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], "/".join(parts[2:]), "character"
    # Structural asset (e.g. "ape/Nose.png"): body + a fixed filename.
    return parts[0], "(structural)", parts[-1], "structural"


async def resolved_bodies(store: Any, slot: str, value: str) -> list[str]:
    """Bodies whose own dir (or the shared/ fallback baked into store.resolve)
    provides this (slot, value). Empty => the art is absent everywhere; a
    non-empty list on a character gap means the value exists for another body
    but the swap matrix doesn't route it to the failing one."""
    out: list[str] = []
    for body in await store.list_bodies():
        if await store.resolve(body, slot, value):
            out.append(body)
    return out


# --------------------------------------------------------------------------- #
# Pure / async audit core (store injected, unit-testable)                     #
# --------------------------------------------------------------------------- #


async def run_audit(
    characters: list[CharacterRecord],
    loose: list[LooseTrait],
    store: Any,
    unreadable: list[str] | None = None,
) -> AuditResult:
    """Reconcile every character (via the real ``swap_compose.missing_layers``)
    and every loose trait (via an exists-for-any-body check) against ``store``."""
    entries: dict[str, MissingEntry] = {}

    # Characters: exactly the swap-path check (own -> shared -> foreign + ape extras).
    char_missing = await asyncio.gather(
        *(swap_compose.missing_layers(c.attributes, c.body, store) for c in characters)
    )
    for rec, missing in zip(characters, char_missing, strict=False):
        for path in missing:
            body, slot, value, kind = _parse_missing_path(path)
            entry = entries.setdefault(path, MissingEntry(path, slot, value, kind))
            entry.add_ref(rec.ref)
            entry.sources.add(rec.source)
            if body:
                entry.missing_bodies.add(body)

    # Loose traits: a body-agnostic value only needs a file under SOME body/shared.
    for lt in loose:
        if not lt.value or lt.value == "None":
            continue
        if await resolved_bodies(store, lt.slot, lt.value):
            continue
        key = f"loose::{lt.slot}/{lt.value}"
        entry = entries.setdefault(
            key, MissingEntry(f"{lt.slot}/{lt.value}", lt.slot, lt.value, "loose")
        )
        entry.add_ref(lt.ref or lt.owner)
        entry.sources.add(lt.source)

    # Annotate each distinct (slot, value) with where the art DOES live (backfill hint).
    for entry in entries.values():
        if entry.kind != "structural":
            entry.resolved_bodies = await resolved_bodies(store, entry.slot, entry.value)

    ordered = sorted(entries.values(), key=lambda e: (e.slot, e.value, e.path))
    return AuditResult(
        missing=ordered,
        unreadable=list(unreadable or []),
        character_count=len(characters),
        loose_count=len(loose),
    )


# --------------------------------------------------------------------------- #
# Source collectors (I/O glue)                                                #
# --------------------------------------------------------------------------- #


def _attrs_from_lfg_row(row: dict[str, Any], columns: set[str]) -> list[dict[str, str]]:
    attrs: list[dict[str, str]] = []
    for col, slot in LFG_COLUMN_TO_SLOT.items():
        if col not in columns:
            continue
        val = row.get(col)
        if val is None or str(val).strip() in ("", "None"):
            continue
        attrs.append({"trait_type": slot, "value": str(val)})
    return swap_meta.normalize_attributes(attrs)


def _pick_body(body_type: Any, attributes: list[dict[str, str]]) -> str:
    if body_type and str(body_type) in trait_config.VALID_BODIES:
        return str(body_type)
    return swap_meta.detect_body(attributes)


def collect_lfg_records(app_db_path: str, network: str | None) -> list[CharacterRecord]:
    """Every edition in the ``LFG`` app table as a character record. Filters by
    the ``network`` column when present; rows predating that column (NULL) are
    kept (can't be attributed to a network)."""
    if not os.path.exists(app_db_path):
        return []
    conn = sqlite3.connect(app_db_path)
    try:
        conn.row_factory = sqlite3.Row
        columns = {r[1] for r in conn.execute("PRAGMA table_info(LFG)")}
        if not columns:
            return []
        records: list[CharacterRecord] = []
        for raw in conn.execute("SELECT * FROM LFG"):
            row = dict(raw)
            # Never-minted draft rows (nft_id NULL/empty) are not real tokens —
            # they can't be swapped, so a missing layer on one blocks nothing.
            # (The genesis-start test rows 3536-3540 are exactly these.)
            if "nft_id" in columns and not row.get("nft_id"):
                continue
            if (
                network
                and "network" in columns
                and row.get("network")
                and row["network"] != network
            ):
                continue
            attrs = _attrs_from_lfg_row(row, columns)
            if not attrs:
                continue
            body = _pick_body(row.get("body_type") if "body_type" in columns else None, attrs)
            records.append(CharacterRecord("LFG", str(row.get("nft_number")), body, attrs))
        return records
    finally:
        conn.close()


def collect_onchain_records(conn: sqlite3.Connection) -> tuple[list[CharacterRecord], list[str]]:
    """Every LIVE on-chain token (all per-edition duplicates). Tokens whose
    metadata never resolved (empty cached attributes) can't be checked and are
    returned separately as ``unreadable`` rather than counted as gaps."""
    records: list[CharacterRecord] = []
    unreadable: list[str] = []
    for n in nft_index.live_nfts(conn):
        if not n.attributes:
            unreadable.append(n.nft_id)
            continue
        attrs = swap_meta.normalize_attributes(n.attributes)
        body = _pick_body(n.body, attrs)
        records.append(CharacterRecord("onchain", n.nft_id, body, attrs))
    return records, unreadable


def collect_loose_traits(conn: sqlite3.Connection) -> list[LooseTrait]:
    """Loose Closet assets + tradeable trait tokens (economy stores share the
    per-network onchain DB)."""
    loose: list[LooseTrait] = []
    for owner, slot, value, _count in economy_store.read_closet_assets(conn):
        loose.append(LooseTrait("closet", owner, LFG_COLUMN_TO_SLOT.get(slot, slot), value))
    for nft_id, owner, slot, value in economy_store.read_trait_tokens(conn):
        loose.append(
            LooseTrait("trait_token", owner, LFG_COLUMN_TO_SLOT.get(slot, slot), value, nft_id)
        )
    return loose


# --------------------------------------------------------------------------- #
# Reporting + CLI                                                             #
# --------------------------------------------------------------------------- #


def format_report(result: AuditResult, network: str, timestamp: str) -> str:
    lines: list[str] = [
        f"# Trait-file reconciliation — {network}",
        "",
        f"Generated: {timestamp}",
        f"Checked: {result.character_count} characters, {result.loose_count} loose traits.",
        "",
    ]
    if result.unreadable:
        lines += [
            f"⚠️ {len(result.unreadable)} live token(s) had no cached metadata and were "
            "NOT checked (re-backfill the index): "
            + ", ".join(result.unreadable[:MAX_REFS])
            + (" …" if len(result.unreadable) > MAX_REFS else ""),
            "",
        ]
    if result.ok:
        lines += ["✅ **0 missing (slot, value) pairs.** Every stored trait resolves to a file."]
        return "\n".join(lines)

    lines += [f"❌ **{len(result.missing)} distinct missing asset(s).**", ""]
    by_slot: dict[str, list[MissingEntry]] = {}
    for entry in result.missing:
        by_slot.setdefault(entry.slot, []).append(entry)
    for slot in sorted(by_slot):
        lines.append(f"## {slot} ({len(by_slot[slot])})")
        lines.append("")
        for e in by_slot[slot]:
            where = (
                f" — art exists for [{', '.join(e.resolved_bodies)}] "
                f"(missing for {', '.join(sorted(e.missing_bodies)) or 'loose'}; matrix doesn't route it)"
                if e.resolved_bodies
                else " — **absent everywhere (needs art)**"
            )
            src = "/".join(sorted(e.sources)) or e.kind
            lines.append(f"- `{e.path}` ×{e.count} [{src}]{where}")
            if e.refs:
                lines.append(f"  - e.g. {', '.join(e.refs)}")
        lines.append("")
    return "\n".join(lines)


async def _amain(args: argparse.Namespace) -> int:
    if config.LAYER_SOURCE != "local":
        print(
            f"WARNING: LAYER_SOURCE={config.LAYER_SOURCE!r} (not 'local'). This audit reconciles "
            "against the LOCAL layer tree — set LAYER_SOURCE=local for an authoritative run.",
            file=sys.stderr,
        )

    index_path = nft_index.index_db_path(args.network)
    if not os.path.exists(index_path):
        print(f"ERROR: on-chain index DB not found: {index_path}", file=sys.stderr)
        return 2

    store = layer_store.get_layer_store()

    conn = nft_index.init_db(index_path)
    economy_store.init_economy_schema(conn)
    try:
        onchain, unreadable = collect_onchain_records(conn)
        loose = collect_loose_traits(conn)
    finally:
        conn.close()

    lfg = collect_lfg_records(args.app_db, args.network)
    characters = lfg + onchain

    result = await run_audit(characters, loose, store, unreadable=unreadable)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = format_report(result, args.network, timestamp)
    print(report)

    os.makedirs(args.report_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(args.report_dir, f"trait-files-{args.network}-{stamp}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(f"\nReport written to {out_path}", file=sys.stderr)

    return 1 if result.missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        choices=["testnet", "mainnet"],
        default=config.XRPL_NETWORK,
        help="Which on-chain index DB to sweep (default: %(default)s).",
    )
    parser.add_argument(
        "--app-db",
        default=config.DB_PATH,
        help="Path to the LFG app SQLite DB (default: %(default)s).",
    )
    parser.add_argument(
        "--report-dir",
        default=os.path.join(REPO_ROOT, "reports"),
        help="Directory for the markdown report (default: %(default)s).",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
