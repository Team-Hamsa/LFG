# lfg_core/closet_token.py
# The per-user on-ledger Closet NFToken. Its metadata JSON is the authoritative
# on-chain record of a user's loose assets + bodies (the DB tables mirror it).
# This module builds/parses that metadata (pure) and wraps the mint-on-first-use
# + modify lifecycle (injectable, so tests need no network).

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lfg_core import config, economy_store

PENDING_ACCEPT = "pending_accept"
ACTIVE = "active"

# Asset triples are (slot, value, count); bodies are edition ints.
Asset = tuple[str, str, int]

# Injected XRPL/CDN operations (real wrappers in EconomyDeps; fakes in tests):
UploadFn = Callable[[dict[str, Any]], Awaitable[str]]  # metadata dict -> CDN url
MintFn = Callable[[str], Awaitable[str | None]]  # url -> nft_id
OfferFn = Callable[[str, str], Awaitable[str | None]]  # (nft_id, owner) -> offer_id
AcceptFn = Callable[[str], Awaitable[dict[str, Any] | None]]  # offer_id -> XUMM payload
ModifyFn = Callable[[str, str, str], Awaitable[str | None]]  # (nft_id, owner, url) -> tx hash
ExistsFn = Callable[[str], Awaitable[bool]]  # nft_id -> does it exist on-ledger?


class ClosetError(RuntimeError):
    """A Closet NFToken lifecycle step (mint/offer/modify) failed."""


@dataclass
class ClosetRef:
    """A user's Closet NFToken. `accept_payload` is set only when the closet was
    just minted (the user must accept the offer to take custody); it is None for
    an already-existing closet or when the XUMM payload could not be built."""

    nft_id: str
    uri_hex: str
    accept_payload: dict[str, Any] | None = None
    minted: bool = False


def _hex(url: str) -> str:
    return url.encode("utf-8").hex().upper()


def build_closet_metadata(owner: str, assets: list[Asset], bodies: list[int]) -> dict[str, Any]:
    """The Closet NFToken metadata JSON. `lfg_closet` enumerates the loose
    contents deterministically (assets sorted by (slot, value), bodies sorted)
    so the same state always produces byte-identical metadata."""
    return {
        "schema": config.NFT_SCHEMA_URL,
        "name": f"LFG Closet — {owner}",
        "description": f"Loose traits and bodies held by {owner}.",
        "image": config.CLOSET_IMAGE_URL,
        "external_link": config.EXTERNAL_WEBSITE_URL,
        "lfg_closet": {
            "assets": [
                {"slot": slot, "value": value, "count": count}
                for slot, value, count in sorted(assets)
            ],
            "bodies": sorted(bodies),
        },
    }


def parse_closet_metadata(meta: dict[str, Any]) -> tuple[list[Asset], list[int]]:
    """Inverse of build_closet_metadata: read (assets, bodies) back out of a
    Closet NFToken's metadata. Tolerant of missing/garbage fields — anything
    malformed yields empty lists rather than raising (the listener consumes
    untrusted on-chain metadata). Tries lfg_closet first, falls back to lfg_bucket."""
    block = meta.get("lfg_closet")
    if not isinstance(block, dict):
        block = meta.get("lfg_bucket")  # backward compat: old Bucket tokens
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


async def ensure_closet(
    conn: Any,
    owner: str,
    *,
    upload_fn: UploadFn,
    mint_fn: MintFn,
    offer_fn: OfferFn,
    accept_payload_fn: AcceptFn,
    exists_fn: ExistsFn | None = None,
) -> ClosetRef:
    """Return the owner's Closet NFToken, minting it on first use. A fresh closet
    is minted empty, offered to the owner, and recorded; the returned
    `accept_payload` lets the caller surface the XUMM accept to the user. This is
    a reversible step (an empty closet simply sits in the wallet), so flows call
    it before any irreversible action. Raises ClosetError on mint/offer failure.

    When `exists_fn` is supplied, a DB record is verified against the ledger
    before it is trusted: a recorded token that no longer exists on-chain (e.g.
    after a testnet reset, or a closet that never actually landed) is treated as
    STALE and a fresh closet is minted, overwriting the dead row. This prevents a
    later NFTokenModify from targeting a non-existent token (tecNO_ENTRY) and, in
    harvest, losing the just-burned character's assets (#101). With `exists_fn`
    None, the record is trusted (legacy/test behavior)."""
    existing = economy_store.get_closet_token(conn, owner)
    if existing is not None and (exists_fn is None or await exists_fn(existing[0])):
        return ClosetRef(nft_id=existing[0], uri_hex=existing[1], accept_payload=None)

    url = await upload_fn(build_closet_metadata(owner, [], []))
    nft_id = await mint_fn(url)
    if not nft_id:
        raise ClosetError("failed to mint Closet NFToken")
    offer_id = await offer_fn(nft_id, owner)
    if not offer_id:
        raise ClosetError("failed to offer Closet NFToken to owner")
    payload = await accept_payload_fn(offer_id)  # None is non-fatal (accept later)
    economy_store.set_closet_token(conn, owner, nft_id, _hex(url))
    return ClosetRef(nft_id=nft_id, uri_hex=_hex(url), accept_payload=payload, minted=True)


async def sync_closet(
    conn: Any,
    owner: str,
    assets: list[Asset],
    bodies: list[int],
    *,
    upload_fn: UploadFn,
    modify_fn: ModifyFn,
) -> None:
    """Recompose the Closet NFToken's metadata from the given contents and
    NFTokenModify its URI in place (the token id is stable). Persists the new
    URI. Raises ClosetError if the closet is unknown or the modify fails."""
    existing = economy_store.get_closet_token(conn, owner)
    if existing is None:
        raise ClosetError(f"no Closet NFToken on record for {owner}")
    nft_id = existing[0]
    url = await upload_fn(build_closet_metadata(owner, assets, bodies))
    tx_hash = await modify_fn(nft_id, owner, url)
    if not tx_hash:
        raise ClosetError("failed to modify Closet NFToken URI")
    economy_store.set_closet_token(conn, owner, nft_id, _hex(url))
