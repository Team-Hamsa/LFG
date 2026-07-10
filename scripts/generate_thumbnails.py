#!/usr/bin/env python3
"""Pre-render roster thumbnails for the local image archive.

  python scripts/generate_thumbnails.py --network mainnet

Writes images_<network>/thumbs/<edition>.webp (256px, q80) for every archived
still. The Activity's grid tiles render at ~120px but were downloading the
full 1080px PNGs (~634 KB each — ~195 MB for a 300-NFT wallet); the thumbs
average ~10 KB. Idempotent: a thumb newer than its source is skipped, so
re-running after new mints/swaps only builds what changed. /api/img?w=256
serves these, falling back to the full still when a thumb is missing."""

from __future__ import annotations

import argparse
import logging
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, image_archive  # noqa: E402

_STILL_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def iter_editions(archive: str) -> list[tuple[int, str]]:
    """(edition, still_path) for every numeric-stem still in the archive root,
    sorted by edition. Skips subdirectories (thumbs/, history/) and non-still
    companions (mp4 animations already have a poster still alongside)."""
    out = []
    for name in os.listdir(archive):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in _STILL_EXTS or not stem.isdigit():
            continue
        path = os.path.join(archive, name)
        if os.path.isfile(path):
            out.append((int(stem), path))
    return sorted(out)


def thumb_stale(src: str, dest: str) -> bool:
    """True when `dest` is missing or older than `src`."""
    if not os.path.exists(dest):
        return True
    return os.path.getmtime(dest) < os.path.getmtime(src)


def make_thumb(src: str, dest: str, *, size: int, quality: int) -> None:
    """Resize `src` to fit in a size×size box (never upscaling) and write a
    WebP to `dest`. Animated sources contribute their first frame."""
    from PIL import Image

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with Image.open(src) as src_im:
        im = src_im.convert("RGB")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    im.save(dest, "WEBP", quality=quality)


def run(*, network: str, size: int, quality: int, force: bool = False) -> dict[str, int]:
    archive = image_archive.archive_dir(network)
    thumbs = os.path.join(archive, image_archive.THUMB_SUBDIR)
    stats = {"built": 0, "skipped": 0, "failed": 0}
    for edition, src in iter_editions(archive):
        dest = os.path.join(thumbs, f"{edition}.webp")
        if not force and not thumb_stale(src, dest):
            stats["skipped"] += 1
            continue
        try:
            make_thumb(src, dest, size=size, quality=quality)
            stats["built"] += 1
        except Exception as e:
            logging.error(f"thumbnail failed for edition {edition} ({src}): {e!r}")
            stats["failed"] += 1
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Pre-render archive thumbnails.")
    parser.add_argument("--network", default=config.XRPL_NETWORK)
    parser.add_argument("--size", type=int, default=image_archive.THUMB_SIZE)
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--force", action="store_true", help="rebuild even fresh thumbs")
    args = parser.parse_args()
    stats = run(network=args.network, size=args.size, quality=args.quality, force=args.force)
    print(f"[{args.network}] thumbnails: {stats}")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
