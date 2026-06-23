# lfg_core/bucket_token.py
# The per-user on-ledger Bucket NFToken. Its metadata JSON is the authoritative
# on-chain record of a user's loose assets + bodies (the DB tables mirror it).
# This module builds/parses that metadata (pure) and wraps the mint-on-first-use
# + modify lifecycle (injectable, so tests need no network).

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lfg_core import config, economy_store

# Asset triples are (slot, value, count); bodies are edition ints.
Asset = tuple[str, str, int]

# Injected XRPL/CDN operations (real wrappers in EconomyDeps; fakes in tests):
UploadFn = Callable[[dict[str, Any]], Awaitable[str]]  # metadata dict -> CDN url
MintFn = Callable[[str], Awaitable[str | None]]  # url -> nft_id
OfferFn = Callable[[str, str], Awaitable[str | None]]  # (nft_id, owner) -> offer_id
AcceptFn = Callable[[str], Awaitable[dict[str, Any] | None]]  # offer_id -> XUMM payload
ModifyFn = Callable[[str, str, str], Awaitable[str | None]]  # (nft_id, owner, url) -> tx hash


class BucketError(RuntimeError):
    """A Bucket NFToken lifecycle step (mint/offer/modify) failed."""


@dataclass
class BucketRef:
    """A user's Bucket NFToken. `accept_payload` is set only when the bucket was
    just minted (the user must accept the offer to take custody); it is None for
    an already-existing bucket or when the XUMM payload could not be built."""

    nft_id: str
    uri_hex: str
    accept_payload: dict[str, Any] | None = None
    minted: bool = False


def _hex(url: str) -> str:
    return url.encode("utf-8").hex().upper()


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


async def ensure_bucket(
    conn: Any,
    owner: str,
    *,
    upload_fn: UploadFn,
    mint_fn: MintFn,
    offer_fn: OfferFn,
    accept_payload_fn: AcceptFn,
) -> BucketRef:
    """Return the owner's Bucket NFToken, minting it on first use. A fresh bucket
    is minted empty, offered to the owner, and recorded; the returned
    `accept_payload` lets the caller surface the XUMM accept to the user. This is
    a reversible step (an empty bucket simply sits in the wallet), so flows call
    it before any irreversible action. Raises BucketError on mint/offer failure."""
    existing = economy_store.get_bucket_token(conn, owner)
    if existing is not None:
        return BucketRef(nft_id=existing[0], uri_hex=existing[1], accept_payload=None)

    url = await upload_fn(build_bucket_metadata(owner, [], []))
    nft_id = await mint_fn(url)
    if not nft_id:
        raise BucketError("failed to mint Bucket NFToken")
    offer_id = await offer_fn(nft_id, owner)
    if not offer_id:
        raise BucketError("failed to offer Bucket NFToken to owner")
    payload = await accept_payload_fn(offer_id)  # None is non-fatal (accept later)
    economy_store.set_bucket_token(conn, owner, nft_id, _hex(url))
    return BucketRef(nft_id=nft_id, uri_hex=_hex(url), accept_payload=payload, minted=True)


async def sync_bucket(
    conn: Any,
    owner: str,
    assets: list[Asset],
    bodies: list[int],
    *,
    upload_fn: UploadFn,
    modify_fn: ModifyFn,
) -> None:
    """Recompose the Bucket NFToken's metadata from the given contents and
    NFTokenModify its URI in place (the token id is stable). Persists the new
    URI. Raises BucketError if the bucket is unknown or the modify fails."""
    existing = economy_store.get_bucket_token(conn, owner)
    if existing is None:
        raise BucketError(f"no Bucket NFToken on record for {owner}")
    nft_id = existing[0]
    url = await upload_fn(build_bucket_metadata(owner, assets, bodies))
    tx_hash = await modify_fn(nft_id, owner, url)
    if not tx_hash:
        raise BucketError("failed to modify Bucket NFToken URI")
    economy_store.set_bucket_token(conn, owner, nft_id, _hex(url))
