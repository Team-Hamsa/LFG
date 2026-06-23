"""Wire the real XRPL/CDN/XUMM operations into an EconomyDeps for the CLI
drivers. Kept out of lfg_core so the core flows stay free of CDN/compose imports
and remain unit-testable with fakes. (scripts/ is excluded from mypy --strict.)"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from lfg_core import (
    cdn,
    config,
    economy_flow,
    layer_store,
    nft_index,
    swap_compose,
    swap_meta,
    xrpl_ops,
    xumm_ops,
)

NFT_FLAG_BURNABLE = 0x0001


async def _upload(path_on_cdn: str, data: bytes, content_type: str) -> str:
    return await cdn.upload_to_bunny(config.ECONOMY_CDN_FOLDER, path_on_cdn, data, content_type)


async def _upload_bucket(meta: dict[str, Any]) -> str:
    """Upload bucket metadata JSON to a fresh CDN path (unique per sync so the
    modified URI is never a stale cache hit)."""
    path = f"buckets/{uuid.uuid4().hex}.json"
    return await _upload(path, json.dumps(meta, indent=2).encode(), "application/json")


async def _compose_char(
    attrs: list[dict[str, str]], body: str, edition: int, rev: int
) -> tuple[str, str | None, str]:
    """Compose a character image from its trait layers, upload image + metadata,
    return (image_url, video_url, metadata_url)."""
    store = layer_store.get_layer_store()
    basename = f"{edition}_{rev}_{uuid.uuid4().hex[:8]}"
    path, is_video = await swap_compose.compose_nft(attrs, body, store, basename)
    image_url, video_url = await swap_compose.upload_output(
        path, is_video, _upload, f"{edition}/{basename}"
    )
    season = swap_meta.season_for_number(edition)
    meta: dict[str, Any] = {
        "schema": config.NFT_SCHEMA_URL,
        "name": f"{config.NFT_COLLECTION_NAME} #{edition}",
        "description": f"Season {season}",
        "image": image_url,
        "external_link": config.EXTERNAL_WEBSITE_URL,
        "collection": {"name": config.NFT_COLLECTION_NAME, "family": f"Season {season}"},
        "edition": edition,
        "attributes": attrs,
    }
    if video_url:
        meta["video"] = video_url
    meta_url = await _upload(
        f"{edition}/{basename}.json", json.dumps(meta, indent=2).encode(), "application/json"
    )
    return image_url, video_url, meta_url


def build_economy_deps(conn: sqlite3.Connection) -> economy_flow.EconomyDeps:
    """An EconomyDeps backed by the real testnet/mainnet operations."""
    return economy_flow.EconomyDeps(
        conn=conn,
        bucket_upload_fn=_upload_bucket,
        bucket_mint_fn=lambda url: xrpl_ops.mint_nft(
            url, config.BUCKET_TAXON, config.SWAP_ISSUER_ADDRESS, flags=config.BUCKET_NFT_FLAGS
        ),
        bucket_offer_fn=lambda nft_id, owner: xrpl_ops.create_nft_offer(nft_id, owner, amount="0"),
        bucket_accept_fn=lambda offer_id: xumm_ops.create_accept_offer_payload(offer_id),
        bucket_modify_fn=lambda nft_id, owner, url: xrpl_ops.modify_nft(nft_id, owner, url),
        char_compose_fn=_compose_char,
        char_mint_fn=lambda url: xrpl_ops.mint_nft(
            url, config.SWAP_TAXON, config.SWAP_ISSUER_ADDRESS, flags=config.ECONOMY_NFT_FLAGS
        ),
        char_modify_fn=lambda nft_id, owner, url: xrpl_ops.modify_nft(nft_id, owner, url),
        char_burn_fn=lambda nft_id, owner: xrpl_ops.burn_nft(nft_id, owner or None),
        char_offer_fn=lambda nft_id, owner: xrpl_ops.create_nft_offer(nft_id, owner, amount="0"),
        char_accept_fn=lambda offer_id: xumm_ops.create_accept_offer_payload(offer_id),
    )


def load_index_character(conn: sqlite3.Connection, nft_id: str) -> nft_index.OnchainNft | None:
    """The character record from the on-chain index, by nft_id."""
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM onchain_nfts WHERE nft_id = ?", (nft_id,)).fetchone()
    return nft_index._row_to_nft(row) if row else None


async def fetch_burnable(owner: str, nft_id: str) -> bool:
    """Whether `nft_id` (held by `owner`) carries the on-ledger burnable flag —
    required before a harvest can issuer-burn it."""
    for nft in await xrpl_ops.get_account_nfts(owner, config.SWAP_ISSUER_ADDRESS):
        if nft["nft_id"] == nft_id:
            return bool(int(nft.get("flags") or 0) & NFT_FLAG_BURNABLE)
    return False


def open_index(network: str) -> sqlite3.Connection:
    """Open the per-network index DB and ensure the economy schema exists."""
    from lfg_core import economy_store

    conn = nft_index.init_db(nft_index.index_db_path(network))
    economy_store.init_economy_schema(conn)
    return conn
