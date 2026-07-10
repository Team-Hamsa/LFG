"""Local-first archive of every live edition's artwork (#153).

The XRPL (and the metadata it points at) is a *reference*, not our image
host: most legacy mainnet editions carry unpinned `ipfs://` image URIs, and
the CDN turned out to hold stills for only about half the collection. The
archive directory — `images_<network>/<edition>.<ext>`, built and kept
current by `scripts/rebuild_cdn_images.py` — is the copy the app actually
serves. `/api/img` maps a requested image URL back to its edition through
the on-chain index and serves the archived file from disk; the CDN/IPFS
proxy is only a fallback for editions the archive doesn't have yet."""

from __future__ import annotations

import os
import sqlite3

CONTENT_TYPES = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def archive_dir(network: str) -> str:
    """Per-network archive directory; IMAGES_DIR overrides."""
    override = os.getenv("IMAGES_DIR")
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, f"images_{network}")


# Roster/grid tiles render at ~120px (240px at 2x DPR); a 256px WebP is
# visually lossless there at ~10 KB vs the ~634 KB full still. Pre-built by
# scripts/generate_thumbnails.py into <archive>/thumbs/, served by /api/img?w=.
THUMB_SUBDIR = "thumbs"
THUMB_SIZE = 256


def local_thumb(network: str, edition: int) -> tuple[str, str] | None:
    """(path, content_type) of the pre-built thumbnail for `edition`, or None."""
    path = os.path.join(archive_dir(network), THUMB_SUBDIR, f"{edition}.webp")
    if os.path.exists(path):
        return path, "image/webp"
    return None


def local_image(network: str, edition: int) -> tuple[str, str] | None:
    """(path, content_type) of the archived still for `edition`, or None."""
    base = archive_dir(network)
    for ext, ctype in CONTENT_TYPES.items():
        path = os.path.join(base, f"{edition}{ext}")
        if os.path.exists(path):
            return path, ctype
    return None


def edition_for_url(conn: sqlite3.Connection, url: str) -> int | None:
    """The live edition whose on-chain `image` is exactly `url`, or None.

    Only live rows count: a burned duplicate's URL must not shadow-serve.
    Identical URLs across editions mean identical art, so MIN is a safe,
    deterministic pick."""
    if not url:
        return None
    row = conn.execute(
        "SELECT MIN(nft_number) FROM onchain_nfts"
        " WHERE image = ? AND is_burned = 0 AND nft_number IS NOT NULL",
        (url,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None
