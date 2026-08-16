"""Recompose every PRIOR version of each edition's artwork (evolution history).

Mainnet has zero NFTokenModify transactions — every legacy trait swap was a
burn+remint — so an edition's visual history is simply the ordered
succession of its tokens, and the on-chain index (Bithomp-backed) preserves
the attributes of burned tokens. This script recomposes each superseded
version with the same `swap_compose` pipeline a swap uses and archives it
under `images_<network>/history/<edition>/vNN_<nftid8>.png` (+ `.mp4` for
animated versions), writing a per-edition version manifest that a future
"how this NFT changed over time" slideshow can read directly.

The CURRENT version is not duplicated here — it already lives in the main
archive as `images_<network>/<edition>.png` (scripts/rebuild_cdn_images.py);
the manifest's final entry points there. Consecutive identical attribute
sets (a remint that changed nothing visually) share one file. Nothing is
uploaded anywhere: this archive is local-only until the feature ships.

Idempotent and resumable: progress persists to
`<archive>/history/manifest.json` after every edition.

Usage:
    .venv/bin/python scripts/rebuild_image_history.py --network mainnet
    .venv/bin/python scripts/rebuild_image_history.py --network mainnet --editions 2003 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import history_store, layer_store, nft_index, swap_compose

_COMPOSE_CONCURRENCY = 2

Version = dict[str, Any]


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_rebuild_image_history.py)
# --------------------------------------------------------------------------


def order_versions(versions: list[Version]) -> list[Version]:
    """Chronological order of an edition's versions: real mint timestamps
    first, then ledger-index fallbacks, unknowns last; the live token always
    sorts after dead ones at equal rank (it is by definition the latest)."""

    def key(v: Version) -> tuple[int, float, int]:
        if v.get("ts") is not None:
            return (0, float(v["ts"]), 1 if v.get("live") else 0)
        if v.get("ledger_index") is not None:
            return (1, float(v["ledger_index"]), 1 if v.get("live") else 0)
        return (2, 0.0, 1 if v.get("live") else 0)

    return sorted(versions, key=key)


def _attr_key(attrs: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (str(a.get("trait_type")), str(a.get("value"))) for a in attrs if isinstance(a, dict)
        )
    )


def mark_duplicates(ordered: list[Version]) -> list[Version]:
    """Flag versions whose attributes equal the immediately preceding
    version's (`same_as_prev`) — visually identical consecutive frames share
    one composed file. A later RETURN to an earlier look is a real frame."""
    prev = None
    for v in ordered:
        key = _attr_key(v.get("attrs") or [])
        v["same_as_prev"] = prev is not None and key == prev
        prev = key
    return ordered


# --------------------------------------------------------------------------


def load_versions(
    oconn: sqlite3.Connection, hconn: sqlite3.Connection, edition: int
) -> list[Version]:
    """Every known token of an edition (live + burned) with its attributes
    and mint timestamp, chronologically ordered."""
    mint_ts = {
        r["nft_id"]: r["ts"]
        for r in hconn.execute(
            "SELECT nft_id, MIN(ts) AS ts FROM nft_events WHERE event='mint'"
            " AND nft_number=? GROUP BY nft_id",
            (edition,),
        )
    }
    versions: list[Version] = []
    for r in oconn.execute(
        "SELECT nft_id, body, attributes_json, is_burned, ledger_index"
        " FROM onchain_nfts WHERE nft_number=?",
        (edition,),
    ):
        try:
            attrs = json.loads(r["attributes_json"] or "[]")
        except ValueError:
            attrs = []
        versions.append(
            {
                "nft_id": r["nft_id"],
                "attrs": attrs,
                "body": r["body"],
                "ts": mint_ts.get(r["nft_id"]),
                "ledger_index": r["ledger_index"],
                "live": not r["is_burned"],
            }
        )
    return mark_duplicates(order_versions(versions))


class Runner:
    def __init__(self, archive_dir: str, *, dry_run: bool = False) -> None:
        self.archive_dir = archive_dir
        self.history_dir = os.path.join(archive_dir, "history")
        self.dry_run = dry_run
        self.manifest_path = os.path.join(self.history_dir, "manifest.json")
        self.manifest: dict[str, Any] = {}
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path) as f:
                self.manifest = json.load(f)
        self.compose_sem = asyncio.Semaphore(_COMPOSE_CONCURRENCY)
        self.lock = asyncio.Lock()
        self.stats = {"editions": 0, "composed": 0, "reused": 0, "skipped": 0, "failed": 0}

    async def _record(self, edition: int, entry: Any) -> None:
        async with self.lock:
            self.manifest[str(edition)] = entry
            if self.dry_run:
                return
            os.makedirs(self.history_dir, exist_ok=True)
            tmp = self.manifest_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.manifest, f, indent=1, sort_keys=True)
            os.replace(tmp, self.manifest_path)

    def _done(self, edition: int, versions: list[Version]) -> bool:
        entry = self.manifest.get(str(edition))
        if not isinstance(entry, list) or len(entry) != len(versions):
            return False
        for rec in entry:
            f = rec.get("file")
            if f and not os.path.exists(os.path.join(self.archive_dir, f)):
                return False
            if rec.get("status") == "failed":
                return False
        return True

    async def _compose_version(self, edition: int, seq: int, v: Version) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "nft_id": v["nft_id"],
            "ts": v["ts"],
            "live": v["live"],
            "same_as_prev": v["same_as_prev"],
        }
        if v["live"]:
            # The current look is the main archive's file — never duplicated.
            rec["file"] = f"{edition}.png"
            rec["status"] = "ok"
            return rec
        if not v["body"] or not v["attrs"]:
            rec["status"] = "failed"
            rec["error"] = "no attributes in index"
            return rec

        store = layer_store.get_layer_store()
        missing = await swap_compose.missing_layers(v["attrs"], v["body"], store)
        if missing:
            rec["status"] = "failed"
            rec["error"] = f"missing layers: {missing}"
            return rec

        # The nft_id's TAIL is the distinguishing part (its head is the
        # flags/fee/issuer prefix shared by the whole collection).
        base = f"history/{edition}/v{seq:02d}_{v['nft_id'][-8:].lower()}"
        img_rel, vid_rel = f"{base}.png", f"{base}.mp4"
        img_dest = os.path.join(self.archive_dir, img_rel)
        if self.dry_run:
            rec["file"] = img_rel
            rec["status"] = "ok"
            return rec

        os.makedirs(os.path.dirname(img_dest), exist_ok=True)
        async with self.compose_sem:
            out_path, is_video = await swap_compose.compose_nft(
                v["attrs"], v["body"], store, f"hist_{edition}_{seq}"
            )
        try:
            if is_video:
                shutil.copyfile(out_path, os.path.join(self.archive_dir, vid_rel))
                await asyncio.to_thread(swap_compose.extract_first_frame, out_path, img_dest)
                rec["video_file"] = vid_rel
            else:
                shutil.copyfile(out_path, img_dest)
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)
        rec["file"] = img_rel
        rec["status"] = "ok"
        return rec

    async def process(
        self, oconn: sqlite3.Connection, hconn: sqlite3.Connection, edition: int
    ) -> None:
        try:
            versions = load_versions(oconn, hconn, edition)
            if len(versions) < 2:
                self.stats["skipped"] += 1
                return  # no history — the live token is the only version
            if self._done(edition, versions):
                self.stats["skipped"] += 1
                return
            records: list[dict[str, Any]] = []
            for seq, v in enumerate(versions):
                if v["same_as_prev"] and records and records[-1].get("file"):
                    rec = {
                        "nft_id": v["nft_id"],
                        "ts": v["ts"],
                        "live": v["live"],
                        "same_as_prev": True,
                        "file": records[-1]["file"],
                        "status": "ok",
                    }
                    self.stats["reused"] += 1
                else:
                    rec = await self._compose_version(edition, seq, v)
                    if rec["status"] == "ok" and not v["live"]:
                        self.stats["composed"] += 1
                    elif rec["status"] == "failed":
                        self.stats["failed"] += 1
                        logging.error(f"edition {edition} v{seq}: {rec.get('error')}")
                records.append(rec)
            await self._record(edition, records)
            self.stats["editions"] += 1
        except Exception as e:
            logging.error(f"edition {edition}: {e!r}")
            self.stats["failed"] += 1


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default="mainnet", choices=("testnet", "mainnet"))
    ap.add_argument("--archive-dir", default=None, help="default: images_<network>/ at repo root")
    ap.add_argument("--editions", default=None, help="comma-separated subset")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    archive_dir = args.archive_dir or os.path.join(repo_root, f"images_{args.network}")
    os.makedirs(archive_dir, exist_ok=True)

    oconn = nft_index.init_db(nft_index.index_db_path(args.network))
    oconn.row_factory = sqlite3.Row
    hconn = history_store.init_history_db(history_store.history_db_path(args.network))

    editions = [
        int(r[0])
        for r in oconn.execute(
            "SELECT nft_number FROM onchain_nfts WHERE nft_number IS NOT NULL"
            " GROUP BY nft_number HAVING COUNT(*) > 1 ORDER BY nft_number"
        )
    ]
    if args.editions:
        wanted = {int(e) for e in args.editions.split(",")}
        editions = [e for e in editions if e in wanted]
    if args.limit:
        editions = editions[: args.limit]

    runner = Runner(archive_dir, dry_run=args.dry_run)
    done = 0
    for chunk_start in range(0, len(editions), 25):
        chunk = editions[chunk_start : chunk_start + 25]
        await asyncio.gather(*(runner.process(oconn, hconn, e) for e in chunk))
        done += len(chunk)
        logging.info(f"progress {done}/{len(editions)} {runner.stats}")

    logging.info(f"final: {runner.stats}")
    return 1 if runner.stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
