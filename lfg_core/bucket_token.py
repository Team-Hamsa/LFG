# lfg_core/bucket_token.py
# The per-user on-ledger Bucket NFToken. Its metadata JSON is the authoritative
# on-chain record of a user's loose assets + bodies (the DB tables mirror it).
# This module builds/parses that metadata (pure) and wraps the mint-on-first-use
# + modify lifecycle (injectable, so tests need no network).

from __future__ import annotations

from typing import Any

from lfg_core import config

# Asset triples are (slot, value, count); bodies are edition ints.
Asset = tuple[str, str, int]


def build_bucket_metadata(owner: str, assets: list[Asset], bodies: list[int]) -> dict[str, Any]:
    """The Bucket NFToken metadata JSON. `lfg_bucket` enumerates the loose
    contents deterministically (assets sorted by (slot, value), bodies sorted)
    so the same state always produces byte-identical metadata."""
    return {
        "schema": config.NFT_SCHEMA_URL,
        "name": f"LFG Bucket — {owner}",
        "description": f"Loose traits and bodies held by {owner}.",
        "image": config.BUCKET_IMAGE_URL,
        "external_link": config.EXTERNAL_WEBSITE_URL,
        "lfg_bucket": {
            "assets": [
                {"slot": slot, "value": value, "count": count}
                for slot, value, count in sorted(assets)
            ],
            "bodies": sorted(bodies),
        },
    }


def parse_bucket_metadata(meta: dict[str, Any]) -> tuple[list[Asset], list[int]]:
    """Inverse of build_bucket_metadata: read (assets, bodies) back out of a
    Bucket NFToken's metadata. Tolerant of missing/garbage fields — anything
    malformed yields empty lists rather than raising (the listener consumes
    untrusted on-chain metadata)."""
    block = meta.get("lfg_bucket")
    if not isinstance(block, dict):
        return [], []
    assets: list[Asset] = []
    raw_assets = block.get("assets")
    if isinstance(raw_assets, list):
        for entry in raw_assets:
            if not isinstance(entry, dict):
                continue
            slot, value, count = entry.get("slot"), entry.get("value"), entry.get("count")
            if isinstance(slot, str) and isinstance(value, str) and isinstance(count, int):
                assets.append((slot, value, count))
    bodies: list[int] = []
    raw_bodies = block.get("bodies")
    if isinstance(raw_bodies, list):
        bodies = [b for b in raw_bodies if isinstance(b, int)]
    return assets, bodies
