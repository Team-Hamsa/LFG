"""Rebuild the collection's image archive: local-first, CDN repaired as a side effect.

The Bunny storage zone turned out to hold images for only ~half the live
mainnet editions (the rest were json-only or never uploaded — their images
lived solely on unpinned IPFS). The XRPL is our reference, not our image
host, so this script makes US the source of truth for every live edition:

  1. Sweep every live edition in the on-chain index (`onchain_<net>.db`).
  2. If the CDN already has an image for it — download it into the local
     archive `images_<network>/<edition>.<ext>` (plus the mp4 if present).
  3. If not — recompose the image from the edition's on-chain traits
     (attributes/body from the index, the same `swap_compose` pipeline a
     trait swap uses), save it into the archive, and upload it back to the
     CDN at `LFGO/<edition>/<stem>.png` (+ `.mp4` for animated editions).
  4. Repair the edition's CDN metadata json: fill a null/ipfs image field
     with the CDN URL (json-only dirs), or create the json outright
     (missing dirs), so every edition resolves on the CDN too.

Idempotent and resumable: progress persists to `<archive>/manifest.json`
after every edition; a re-run skips editions whose archive files exist.

Usage:
    .venv/bin/python scripts/rebuild_cdn_images.py --network mainnet
    .venv/bin/python scripts/rebuild_cdn_images.py --network mainnet --editions 1,8 --dry-run
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

import aiohttp

from lfg_core import cdn, config, layer_store, nft_index, swap_compose, swap_meta

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VIDEO_EXTS = (".mp4",)

# ffmpeg composition is CPU-heavy; network transfers are cheap.
_COMPOSE_CONCURRENCY = 2
_NETWORK_CONCURRENCY = 8


# --------------------------------------------------------------------------
# Pure decision helpers (unit-tested in tests/test_rebuild_cdn_images.py)
# --------------------------------------------------------------------------


def classify_files(files: list[str] | None) -> tuple[str, str | None]:
    """What a CDN edition dir holds: ('image', <stem>) when an image file is
    present, ('json_only', <json stem>) when only metadata survives, or
    ('missing', None) for an absent/empty/junk-only dir."""
    if not files:
        return "missing", None
    for f in files:
        if f.lower().endswith(IMAGE_EXTS):
            return "image", os.path.splitext(f)[0]
    for f in files:
        if f.lower().endswith(".json"):
            return "json_only", os.path.splitext(f)[0]
    return "missing", None


def target_basename(edition: int, files: list[str] | None, burn_count: int) -> str:
    """Upload stem for a rebuilt image: pair with the dir's surviving json if
    there is one (so `<stem>.png` sits beside `<stem>.json`), else the mint
    flow's `<edition>_<burncount>` convention."""
    for f in files or []:
        if f.lower().endswith(".json"):
            return os.path.splitext(f)[0]
    return f"{edition}_{burn_count}"


def pick_archive_source(files: list[str] | None) -> tuple[str | None, str | None]:
    """(image filename, video filename) worth downloading from a CDN dir —
    png preferred over other stills; the mp4 rides along when present."""
    stills = [f for f in files or [] if f.lower().endswith(IMAGE_EXTS)]
    stills.sort(key=lambda f: (not f.lower().endswith(".png"), f))
    video = next((f for f in files or [] if f.lower().endswith(VIDEO_EXTS)), None)
    return (stills[0] if stills else None), video


def patched_metadata(
    meta: dict[str, Any], image_url: str, video_url: str | None
) -> dict[str, Any] | None:
    """A copy of `meta` with its image (and video) pointed at our CDN, or
    None when the json already resolves there and needs no rewrite."""
    image = meta.get("image")
    if isinstance(image, str) and image.startswith(f"{config.BUNNY_CDN_PUBLIC_BASE}/"):
        return None
    out = dict(meta)
    out["image"] = image_url
    if video_url:
        out["video"] = video_url
    return out


# --------------------------------------------------------------------------
# Bunny storage helpers
# --------------------------------------------------------------------------


def _storage_base() -> str:
    return f"{config.BUNNY_CDN_BASE_URL}/{config.BUNNY_CDN_STORAGE_ZONE}"


async def list_cdn_dir(session: aiohttp.ClientSession, edition: int) -> list[str] | None:
    """File names in the storage zone's LFGO/<edition>/ dir ([] when absent),
    or None on persistent API failure (caller must NOT treat as missing)."""
    url = f"{_storage_base()}/LFGO/{edition}/"
    for attempt in range(3):
        try:
            async with session.get(url, headers={"AccessKey": config.BUNNY_CDN_ACCESS_KEY}) as r:
                if r.status == 200:
                    entries = await r.json()
                    return [e["ObjectName"] for e in entries if not e.get("IsDirectory")]
                if r.status == 404:
                    return []
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.warning(f"list LFGO/{edition}/ attempt {attempt + 1} failed: {e!r}")
        await asyncio.sleep(1 + attempt)
    return None


async def fetch_cdn_file(session: aiohttp.ClientSession, path: str) -> bytes | None:
    """GET LFGO/<path> from the pull zone; None on any failure."""
    url = f"{config.BUNNY_CDN_PUBLIC_BASE}/LFGO/{path}"
    for attempt in range(3):
        try:
            async with session.get(url) as r:
                if r.status == 200:
                    return await r.read()
                if r.status == 404:
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.warning(f"fetch {path} attempt {attempt + 1} failed: {e!r}")
        await asyncio.sleep(1 + attempt)
    return None


# --------------------------------------------------------------------------
# Per-edition work
# --------------------------------------------------------------------------


class Runner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        archive_dir: str,
        *,
        dry_run: bool = False,
        no_upload: bool = False,
    ) -> None:
        self.conn = conn
        self.archive_dir = archive_dir
        self.dry_run = dry_run
        self.no_upload = no_upload
        self.manifest_path = os.path.join(archive_dir, "manifest.json")
        self.manifest: dict[str, dict[str, Any]] = {}
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path) as f:
                self.manifest = json.load(f)
        self.compose_sem = asyncio.Semaphore(_COMPOSE_CONCURRENCY)
        self.net_sem = asyncio.Semaphore(_NETWORK_CONCURRENCY)
        self.lock = asyncio.Lock()
        self.stats = {"cdn": 0, "rebuilt": 0, "skipped": 0, "failed": 0}

    def _archive_path(self, edition: int, ext: str) -> str:
        return os.path.join(self.archive_dir, f"{edition}{ext}")

    def _done(self, edition: int) -> bool:
        entry = self.manifest.get(str(edition))
        if not entry or entry.get("status") != "ok":
            return False
        img = entry.get("image_file")
        return bool(img) and os.path.exists(os.path.join(self.archive_dir, img))

    async def _record(self, edition: int, entry: dict[str, Any]) -> None:
        async with self.lock:
            self.manifest[str(edition)] = entry
            tmp = self.manifest_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.manifest, f, indent=1, sort_keys=True)
            os.replace(tmp, self.manifest_path)

    def _burn_count(self, edition: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM onchain_nfts WHERE nft_number=? AND is_burned=1",
            (edition,),
        ).fetchone()
        return int(row[0]) if row else 0

    async def _upload(self, path_on_cdn: str, data: bytes, content_type: str) -> str:
        if self.dry_run or self.no_upload:
            return f"{config.BUNNY_CDN_PUBLIC_BASE}/LFGO/{path_on_cdn}"
        return await cdn.upload_to_bunny("LFGO", path_on_cdn, data, content_type)

    async def _archive_from_cdn(
        self, session: aiohttp.ClientSession, edition: int, files: list[str]
    ) -> dict[str, Any] | None:
        img_name, vid_name = pick_archive_source(files)
        assert img_name is not None
        entry: dict[str, Any] = {"status": "ok", "source": "cdn"}
        for name, key in ((img_name, "image_file"), (vid_name, "video_file")):
            if not name:
                continue
            ext = os.path.splitext(name)[1].lower()
            dest = self._archive_path(edition, ext)
            if not os.path.exists(dest):
                data = await fetch_cdn_file(session, f"{edition}/{name}")
                if data is None:
                    if key == "image_file":
                        return None  # image download failed — retry next run
                    continue  # tolerate a lost video; the still is what matters
                if not self.dry_run:
                    tmp = dest + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, dest)
            entry[key] = os.path.basename(dest)
            if key == "image_file":
                entry["cdn_image"] = f"{config.BUNNY_CDN_PUBLIC_BASE}/LFGO/{edition}/{name}"
        return entry

    async def _rebuild(
        self, session: aiohttp.ClientSession, edition: int, files: list[str]
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT body, attributes_json FROM onchain_nfts"
            " WHERE nft_number=? AND is_burned=0 ORDER BY ledger_index DESC LIMIT 1",
            (edition,),
        ).fetchone()
        body = row["body"] if row else None
        attrs = json.loads(row["attributes_json"] or "[]") if row else []
        if not body or not attrs:
            logging.error(f"edition {edition}: no traits in index — cannot rebuild")
            return None

        store = layer_store.get_layer_store()
        missing = await swap_compose.missing_layers(attrs, body, store)
        if missing:
            logging.error(f"edition {edition}: missing layers {missing}")
            return None

        stem = target_basename(edition, files, self._burn_count(edition))
        async with self.compose_sem:
            out_path, is_video = await swap_compose.compose_nft(
                attrs, body, store, f"rebuild_{edition}"
            )

        # Archive copies FIRST — upload_output deletes its inputs, and with
        # --no-upload there is nothing on the CDN to re-fetch afterwards.
        entry: dict[str, Any] = {"status": "ok", "source": "rebuilt"}
        img_dest = self._archive_path(edition, ".png")
        if is_video:
            vid_dest = self._archive_path(edition, ".mp4")
            if not self.dry_run:
                shutil.copyfile(out_path, vid_dest)
                await asyncio.to_thread(swap_compose.extract_first_frame, out_path, img_dest)
            entry["video_file"] = os.path.basename(vid_dest)
        elif not self.dry_run:
            shutil.copyfile(out_path, img_dest)
        entry["image_file"] = os.path.basename(img_dest)

        async with self.net_sem:
            image_url, video_url = await swap_compose.upload_output(
                out_path, is_video, self._upload, f"{edition}/{stem}"
            )
        entry["cdn_image"] = image_url
        if video_url:
            entry["cdn_video"] = video_url

        if self.dry_run or self.no_upload:
            return entry  # nothing landed on the CDN — the local copy is it

        # The CDN copy is canonical (upload_output re-encodes the video
        # poster) — pull it back so local == CDN byte-for-byte; on a fetch
        # blip the locally-extracted copy stands in.
        async with self.net_sem:
            data = await fetch_cdn_file(session, f"{edition}/{stem}.png")
        if data:
            tmp = img_dest + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, img_dest)
        elif not os.path.exists(img_dest):
            logging.error(f"edition {edition}: uploaded but could not re-fetch still")
            return None

        async with self.net_sem:
            await self._repair_metadata(session, edition, files, stem, image_url, video_url)
        return entry

    async def _repair_metadata(
        self,
        session: aiohttp.ClientSession,
        edition: int,
        files: list[str],
        stem: str,
        image_url: str,
        video_url: str | None,
    ) -> None:
        """Point the edition's CDN metadata json at the (re)uploaded image —
        patch the surviving json in place, or create one from the index."""
        json_name = next((f for f in files or [] if f.lower().endswith(".json")), None)
        if json_name:
            raw = await fetch_cdn_file(session, f"{edition}/{json_name}")
            try:
                meta = json.loads(raw) if raw else None
            except ValueError:
                meta = None
            if meta is None:
                logging.warning(f"edition {edition}: unreadable CDN json {json_name}")
                return
            patched = patched_metadata(meta, image_url, video_url)
            if patched is None:
                return
            target = json_name
        else:
            row = self.conn.execute(
                "SELECT body, attributes_json FROM onchain_nfts"
                " WHERE nft_number=? AND is_burned=0 ORDER BY ledger_index DESC LIMIT 1",
                (edition,),
            ).fetchone()
            season = swap_meta.season_for_number(edition)
            patched = {
                "schema": config.NFT_SCHEMA_URL,
                "name": f"{config.NFT_COLLECTION_NAME} #{edition}",
                "description": f"Season {season}",
                "image": image_url,
                "external_link": config.EXTERNAL_WEBSITE_URL,
                "collection": {"name": config.NFT_COLLECTION_NAME, "family": f"Season {season}"},
                "edition": edition,
                "attributes": json.loads(row["attributes_json"] or "[]") if row else [],
            }
            if video_url:
                patched["video"] = video_url
            target = f"{stem}.json"
        await self._upload(
            f"{edition}/{target}", json.dumps(patched, indent=2).encode(), "application/json"
        )

    async def process(self, session: aiohttp.ClientSession, edition: int) -> None:
        if self._done(edition):
            self.stats["skipped"] += 1
            return
        try:
            async with self.net_sem:
                files = await list_cdn_dir(session, edition)
            if files is None:
                logging.error(f"edition {edition}: CDN listing failed — skipping this run")
                self.stats["failed"] += 1
                return
            kind, _ = classify_files(files)
            if kind == "image":
                async with self.net_sem:
                    entry = await self._archive_from_cdn(session, edition, files)
            else:
                entry = await self._rebuild(session, edition, files)
            if entry is None:
                self.stats["failed"] += 1
                await self._record(edition, {"status": "failed"})
                return
            self.stats["cdn" if entry["source"] == "cdn" else "rebuilt"] += 1
            await self._record(edition, entry)
        except Exception as e:
            logging.error(f"edition {edition}: {e!r}")
            self.stats["failed"] += 1


# --------------------------------------------------------------------------


def live_editions(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT nft_number FROM onchain_nfts"
            " WHERE is_burned=0 AND nft_number IS NOT NULL ORDER BY nft_number"
        )
    ]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default="mainnet", choices=("testnet", "mainnet"))
    ap.add_argument("--archive-dir", default=None, help="default: images_<network>/ at repo root")
    ap.add_argument("--editions", default=None, help="comma-separated subset (default: all live)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="no writes, no uploads")
    ap.add_argument("--no-upload", action="store_true", help="archive locally, skip CDN uploads")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    archive_dir = args.archive_dir or os.path.join(repo_root, f"images_{args.network}")
    os.makedirs(archive_dir, exist_ok=True)

    conn = nft_index.init_db(nft_index.index_db_path(args.network))
    conn.row_factory = sqlite3.Row
    editions = live_editions(conn)
    if args.editions:
        wanted = {int(e) for e in args.editions.split(",")}
        editions = [e for e in editions if e in wanted]
    if args.limit:
        editions = editions[: args.limit]

    runner = Runner(conn, archive_dir, dry_run=args.dry_run, no_upload=args.no_upload)
    timeout = aiohttp.ClientTimeout(total=120, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        done = 0
        for chunk_start in range(0, len(editions), 50):
            chunk = editions[chunk_start : chunk_start + 50]
            await asyncio.gather(*(runner.process(session, e) for e in chunk))
            done += len(chunk)
            logging.info(f"progress {done}/{len(editions)} {runner.stats}")

    logging.info(f"final: {runner.stats}")
    failed = [e for e, v in runner.manifest.items() if v.get("status") != "ok"]
    if failed:
        logging.error(f"{len(failed)} editions failed: {sorted(map(int, failed))[:50]}")
    return 1 if runner.stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
