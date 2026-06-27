# lfg_core/trait_token.py
# Pure metadata for the standalone tradeable trait NFToken (Phase 4). The
# `lfg_trait` block is the on-chain record of which (slot, value) the token
# represents; the listener rebuilds the trait_tokens table from it.

from __future__ import annotations

from typing import Any

from lfg_core import config


def build_trait_metadata(slot: str, value: str, image_url: str) -> dict[str, Any]:
    return {
        "schema": config.NFT_SCHEMA_URL,
        "name": f"LFG Trait — {slot}: {value}",
        "description": f"A tradeable {slot} trait ({value}) extracted from an LFG Closet.",
        "image": image_url,
        "external_link": config.EXTERNAL_WEBSITE_URL,
        "collection": {"name": "LFG Traits", "family": config.NFT_COLLECTION_NAME},
        "lfg_trait": {"slot": slot, "value": value},
    }


def parse_trait_metadata(meta: dict[str, Any]) -> tuple[str, str] | None:
    """Read (slot, value) back out of a trait NFToken's metadata. Tolerant of
    missing/garbage fields (the listener consumes untrusted on-chain metadata)."""
    block = meta.get("lfg_trait")
    if not isinstance(block, dict):
        return None
    slot, value = block.get("slot"), block.get("value")
    if isinstance(slot, str) and isinstance(value, str):
        return (slot, value)
    return None
