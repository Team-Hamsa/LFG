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
import re
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


# Subdomain gateway (https://<cid>.ipfs.<host>/<path>) and path gateway
# (https://<host>/ipfs/<cid>/<path>) URL shapes — the forms resolve_ipfs /
# nft_index.IPFS_GATEWAYS produce from an ipfs:// URI. Path optional: a
# path-less CID (the CID is the file itself) resolves to the bare host.
_SUBDOMAIN_GATEWAY = re.compile(r"^https://([a-zA-Z0-9]+)\.ipfs\.[a-zA-Z0-9.-]+(?:/(.*))?$")
_PATH_GATEWAY = re.compile(r"^https://[a-zA-Z0-9.-]+/ipfs/([a-zA-Z0-9]+)(?:/(.*))?$")


def _cid_variants(cid: str, path: str) -> list[str]:
    """Every stored shape one (cid, path) can take. With a path, raw and
    resolved agree on structure; path-less URIs (the CID is the file — six
    live mainnet editions) disagree about the trailing slash between the
    on-chain `ipfs://<cid>` and resolve_ipfs's `https://<cid>.../`, so all
    slash variants must be equivalent."""
    if path:
        return [f"ipfs://{cid}/{path}", f"https://{cid}.ipfs.dweb.link/{path}"]
    return [
        f"ipfs://{cid}",
        f"ipfs://{cid}/",
        f"https://{cid}.ipfs.dweb.link/",
        f"https://{cid}.ipfs.dweb.link",
    ]


def url_forms(url: str) -> list[str]:
    """Every equivalent shape of an image URL as the index may store it.

    The index's `image` column is mixed-shape: Bithomp-imported rows keep the
    on-chain `ipfs://` URI verbatim while listener-written rows store the
    dweb.link-resolved form (nft_index.token_record), and surfaces serve
    whichever shape their source row has. Returns the URL plus its raw
    ipfs:// and dweb.link forms, deduped, order-preserving; a non-IPFS URL is
    just [url]. The dweb format string mirrors swap_meta.resolve_ipfs (kept
    local: this module must stay importable without lfg_core.config)."""
    if not url:
        return []
    forms = [url]
    if url.startswith("ipfs://"):
        cid, _, path = url[len("ipfs://") :].partition("/")
        forms.extend(_cid_variants(cid, path))
    else:
        m = _SUBDOMAIN_GATEWAY.match(url) or _PATH_GATEWAY.match(url)
        if m:
            forms.extend(_cid_variants(m.group(1), m.group(2) or ""))
    return list(dict.fromkeys(forms))


def edition_for_url(conn: sqlite3.Connection, url: str) -> int | None:
    """The live edition whose on-chain `image` matches `url` in any of its
    equivalent shapes (see url_forms), or None.

    Only live rows count: a burned duplicate's URL must not shadow-serve.
    Identical URLs across editions mean identical art, so MIN is a safe,
    deterministic pick."""
    forms = url_forms(url)
    if not forms:
        return None
    placeholders = ",".join("?" * len(forms))
    row = conn.execute(
        "SELECT MIN(nft_number) FROM onchain_nfts"
        f" WHERE image IN ({placeholders}) AND is_burned = 0 AND nft_number IS NOT NULL",
        forms,
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None
