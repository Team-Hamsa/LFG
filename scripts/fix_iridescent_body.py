#!/usr/bin/env python3
"""#301 — correct the misspelled Body value "Iridescent Skeleton" (single-r,
no layer art) to the canonical "Irridescent Skeleton" (double-r) on live
tokens via NFTokenModify, then sync the onchain_nfts index and the LFG app
table from the confirmed result.

Dry-run by default; pass --apply to mutate. Idempotent: already-corrected
tokens are not rediscovered. A non-mutable (legacy flag-24) token is SKIPped
and reported — convert it first with scripts/convert_to_mutable.py, then
re-run this script.

All ledger writes route through lfg_core.xrpl_ops.modify_nft, which stamps
SourceTag 2606160021 and the provenance memos (initiator=backend,
platform=backend, action=modify) — this script never builds a raw tx.

Usage:
    .venv/bin/python scripts/fix_iridescent_body.py --network mainnet
    .venv/bin/python scripts/fix_iridescent_body.py --network mainnet --apply
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

from lfg_core import cdn, config, db_path, nft_index, xrpl_ops  # noqa: E402
from lfg_core.body_fix import BAD, GOOD, rewrite_body_value  # noqa: E402

REPORTS_DIR = "reports"


@dataclass
class Target:
    nft_id: str
    nft_number: int | None
    owner: str | None
    uri_hex: str
    attributes: list[dict[str, Any]]
    image: str
    ledger_index: int | None


@dataclass
class Targets:
    tokens: list[Target] = field(default_factory=list)
    editions: list[int] = field(default_factory=list)


@dataclass
class Result:
    nft_id: str
    edition: int | None
    status: (
        str  # planned | corrected | skipped_non_mutable | skipped_burned | failed | indeterminate
    )
    detail: str = ""
    tx_hash: str | None = None
    new_url: str | None = None


def discover_targets(index_conn: sqlite3.Connection, app_conn: sqlite3.Connection) -> Targets:
    """Live index tokens whose attributes carry the single-r spelling, plus
    app-DB editions whose Body column carries it."""
    index_conn.row_factory = sqlite3.Row
    rows = index_conn.execute(
        "SELECT nft_id, nft_number, owner, uri_hex, attributes_json, image, ledger_index"
        " FROM onchain_nfts"
        " WHERE (is_burned IS NULL OR is_burned=0)"
        "   AND attributes_json LIKE ?",
        (f"%{BAD}%",),
    ).fetchall()
    tokens = []
    for r in rows:
        attrs = json.loads(r["attributes_json"]) if r["attributes_json"] else []
        # LIKE '%Iridescent Skeleton%' also matches the double-r string
        # (it contains the single-r one as a substring) — filter exactly.
        if not any(a.get("trait_type") == "Body" and a.get("value") == BAD for a in attrs):
            continue
        tokens.append(
            Target(
                nft_id=r["nft_id"],
                nft_number=r["nft_number"],
                owner=r["owner"],
                uri_hex=r["uri_hex"] or "",
                attributes=attrs,
                image=r["image"] or "",
                ledger_index=r["ledger_index"],
            )
        )
    editions = [
        row[0]
        for row in app_conn.execute(
            "SELECT nft_number FROM LFG WHERE Body = ? ORDER BY nft_number", (BAD,)
        ).fetchall()
    ]
    return Targets(tokens=tokens, editions=editions)


async def correct_token(
    target: Target,
    http: aiohttp.ClientSession,
    index_conn: sqlite3.Connection,
    app_conn: sqlite3.Connection,
    *,
    apply: bool,
) -> Result:
    nft_id, edition = target.nft_id, target.nft_number

    # 1. Fail-closed on-ledger mutability check — the index's `mutable` column
    #    can be stale/NULL, so never trust it for a modify decision.
    info = await xrpl_ops.nft_info(nft_id)
    if info is None:
        return Result(nft_id, edition, "failed", "nft_info unavailable — fail-closed, no modify")
    if info.get("is_burned"):
        return Result(nft_id, edition, "skipped_burned", "token burned on-ledger")
    if not (int(info.get("flags") or 0) & nft_index.NFT_FLAG_MUTABLE):
        return Result(
            nft_id,
            edition,
            "skipped_non_mutable",
            "legacy non-mutable token — convert with scripts/convert_to_mutable.py, then re-run",
        )
    owner = info.get("owner") or target.owner or ""

    # 2. Rebuild metadata from the live URI (fall back to the index copy).
    uri_hex = info.get("uri_hex") or target.uri_hex
    meta = await nft_index.fetch_metadata_multi(http, uri_hex)
    if meta is None:
        return Result(nft_id, edition, "failed", "metadata fetch failed — no modify attempted")
    new_meta, changed = rewrite_body_value(meta)
    if not changed:
        # On-CDN metadata already carries the double-r spelling; only the
        # mirrors are stale. Still surgical: no upload/modify needed.
        if apply:
            _sync_mirrors(index_conn, app_conn, target, new_meta, uri_hex, owner)
        return Result(
            nft_id,
            edition,
            "corrected" if apply else "planned",
            "metadata already correct; mirrors synced only",
        )

    if not apply:
        return Result(
            nft_id,
            edition,
            "planned",
            f"would upload corrected metadata + NFTokenModify (owner {owner})",
        )

    # 3. Upload under a fresh CDN stem (BunnyCDN caches ~30d per URL — never
    #    reuse the old metadata path).
    stem = (
        f"{edition}/{edition}_fix_{uuid.uuid4().hex[:8]}.json"
        if edition
        else (f"fix/{nft_id[:16]}_{uuid.uuid4().hex[:8]}.json")
    )
    payload = json.dumps(new_meta, indent=2).encode()
    try:
        new_url = await cdn.upload_to_bunny(
            config.SWAP_CDN_FOLDER, stem, payload, "application/json"
        )
    except Exception as e:  # upload failure = no ledger write attempted
        return Result(nft_id, edition, "failed", f"CDN upload failed: {e}")

    # 4. Ledger first — modify_nft stamps SourceTag + provenance memos.
    # Fail-closed on an unconfirmable submission (#107 taxonomy): journal it,
    # touch no mirrors, never blind-retry — reconcile from chain, then re-run.
    try:
        tx_hash = await xrpl_ops.modify_nft(nft_id, owner, new_url)
    except xrpl_ops.IndeterminateResultError as e:
        return Result(
            nft_id,
            edition,
            "indeterminate",
            f"NFTokenModify outcome unknown ({e}) — mirrors untouched; verify the tx "
            "on-ledger (nft_info) before re-running; never blind-retry",
            new_url=new_url,
        )
    if tx_hash is None:
        return Result(
            nft_id,
            edition,
            "failed",
            "NFTokenModify failed (definitive) — mirrors untouched",
            new_url=new_url,
        )

    # 5. Mirrors from the confirmed result.
    new_uri_hex = xrpl_ops.convert_str_to_hex(new_url)
    _sync_mirrors(index_conn, app_conn, target, new_meta, new_uri_hex, owner)
    return Result(nft_id, edition, "corrected", tx_hash=tx_hash, new_url=new_url)


def _sync_mirrors(
    index_conn: sqlite3.Connection,
    app_conn: sqlite3.Connection,
    target: Target,
    new_meta: dict[str, Any],
    uri_hex: str,
    owner: str,
) -> None:
    # LFG first, index second. Either ordering can be interrupted, but
    # discovery keys off BOTH mirrors (index attributes AND LFG.Body), so a
    # rerun after a partial failure re-finds whichever mirror is still stale
    # and converges — the app-only repair path handles a corrected index with
    # a stale LFG row, and a stale index row is rediscovered as a token.
    if target.nft_number is not None:
        app_conn.execute(
            "UPDATE LFG SET Body = ? WHERE nft_number = ? AND Body = ?",
            (GOOD, target.nft_number, BAD),
        )
        app_conn.commit()
    attributes = new_meta.get("attributes") or []
    rec = nft_index.OnchainNft(
        nft_id=target.nft_id,
        nft_number=target.nft_number,
        # The nft_info-confirmed current owner — never the possibly-stale
        # index snapshot (the token may have transferred since).
        owner=owner or target.owner,
        is_burned=False,
        mutable=True,
        uri_hex=uri_hex,
        body="skeleton",
        attributes=attributes,
        image=new_meta.get("image") or target.image,
        ledger_index=target.ledger_index,
    )
    nft_index.upsert(index_conn, rec)
    index_conn.commit()


def _journal(network: str, results: list[Result]) -> None:
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        path = os.path.join(
            REPORTS_DIR, f"fix_iridescent_body-{network}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        )
        with open(path, "w") as fh:
            json.dump([asdict(r) for r in results], fh, indent=2)
        logging.info(f"journal written: {path}")
    except Exception as e:  # journaling must never mask a completed correction
        logging.warning(f"journal write failed: {e}")


async def run(
    network: str,
    apply: bool,
    index_db: str | None = None,
    app_db: str | None = None,
) -> list[Result]:
    index_path = index_db or nft_index.index_db_path(network)
    app_path = app_db or db_path.app_db_path(network)
    results: list[Result] = []
    with sqlite3.connect(index_path) as index_conn, sqlite3.connect(app_path) as app_conn:
        targets = discover_targets(index_conn, app_conn)
        print(
            f"[{network}] targets: {len(targets.tokens)} live token(s) "
            f"{[t.nft_id for t in targets.tokens]}, app-DB editions {targets.editions}"
        )
        if not targets.tokens and not targets.editions:
            print("nothing to do — already clean.")
            return results
        if targets.tokens:
            async with aiohttp.ClientSession() as http:
                for target in targets.tokens:
                    res = await correct_token(target, http, index_conn, app_conn, apply=apply)
                    results.append(res)
                    print(
                        f"  {res.nft_id} (edition {res.edition}): {res.status}"
                        + (f" tx={res.tx_hash}" if res.tx_hash else "")
                        + (f" — {res.detail}" if res.detail else "")
                    )
        # App-DB-only editions: LFG.Body is stale but no live index token
        # carries the typo (index record unreadable/empty, or already
        # canonical — e.g. a rerun after a partial mirror failure). A pure
        # local-DB repair; no ledger op is needed or attempted.
        covered = {t.nft_number for t in targets.tokens if t.nft_number is not None}
        for edition in targets.editions:
            if edition in covered:
                continue
            if apply:
                app_conn.execute(
                    "UPDATE LFG SET Body = ? WHERE nft_number = ? AND Body = ?",
                    (GOOD, edition, BAD),
                )
                app_conn.commit()
            res = Result(
                "",
                edition,
                "corrected" if apply else "planned",
                "app-DB-only repair: LFG.Body rewritten, no matching live index token"
                " (ledger/index already canonical or index metadata unreadable)",
            )
            results.append(res)
            print(f"  edition {res.edition} (app-DB only): {res.status} — {res.detail}")
    if apply:
        _journal(network, results)
    else:
        print("dry-run: nothing was uploaded, modified, or written. Re-run with --apply.")
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network", default="mainnet", choices=["mainnet", "testnet"])
    p.add_argument("--apply", action="store_true", help="actually modify + sync (default: dry-run)")
    args = p.parse_args()
    if args.network != config.XRPL_NETWORK:
        print(
            f"refusing: --network {args.network} != XRPL_NETWORK={config.XRPL_NETWORK} "
            "(xrpl_ops signs against the configured network)"
        )
        return 2
    results = asyncio.run(run(args.network, args.apply))
    return 1 if any(r.status in ("failed", "indeterminate") for r in results) else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
