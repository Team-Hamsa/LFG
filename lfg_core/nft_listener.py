# lfg_core/nft_listener.py
# Apply live XRPL NFToken transactions to the per-nft_id on-chain index, keeping
# it fresh as the chain changes. Handles Mint / AcceptOffer (ownership) / Burn /
# Modify (in-place trait change — the case LFG swaps produce). Pure classifiers
# plus an apply_tx that takes injected resolvers so it is unit-testable.

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from lfg_core import closet_token, config, economy_store, nft_index, swap_meta, trait_economy

_TYPE_TO_KIND = {
    "NFTokenMint": "mint",
    "NFTokenAcceptOffer": "accept",
    "NFTokenBurn": "burn",
    "NFTokenModify": "modify",
}

# Resolver signatures used by apply_tx (injected so tests need no network):
#   fetch_token_fn(nft_id) -> {nft_id, owner, flags, uri_hex, is_burned} | None
#   fetch_meta_fn(uri_hex) -> metadata dict | None
FetchTokenFn = Callable[[str], Awaitable[dict[str, Any] | None]]
FetchMetaFn = Callable[[str], Awaitable[dict[str, Any] | None]]


def classify_tx(tx: dict[str, Any]) -> str | None:
    """Map an NFToken transaction to mint/accept/burn/modify, or None."""
    return _TYPE_TO_KIND.get(str(tx.get("TransactionType", "")))


def affected_nft_ids(tx: dict[str, Any]) -> list[str]:
    """The NFToken id(s) a transaction touches. Reads the explicit `NFTokenID`
    field (Burn/Modify), the `meta.nftoken_id` clio adds (Mint/AcceptOffer), and
    falls back to scanning AffectedNodes NFTokenPage diffs."""
    if classify_tx(tx) is None:
        return []
    ids: list[str] = []
    meta = tx.get("meta") or {}
    for candidate in (tx.get("NFTokenID"), meta.get("nftoken_id")):
        if isinstance(candidate, str) and candidate and candidate not in ids:
            ids.append(candidate)
    if not ids:
        for node in meta.get("AffectedNodes", []):
            wrapper = node.get("CreatedNode") or node.get("ModifiedNode") or {}
            if wrapper.get("LedgerEntryType") != "NFTokenPage":
                continue
            fields = wrapper.get("NewFields") or wrapper.get("FinalFields") or {}
            for tok in fields.get("NFTokens", []):
                tid = (tok.get("NFToken") or {}).get("NFTokenID")
                if isinstance(tid, str) and tid not in ids:
                    ids.append(tid)
    return ids


def _set_burned(conn: sqlite3.Connection, nft_id: str) -> None:
    """Flip is_burned on a known token. Unknown tokens are ignored — a burn of an
    NFT outside our collection must not add a stub row to the index."""
    conn.execute("UPDATE onchain_nfts SET is_burned=1 WHERE nft_id=?", (nft_id,))
    conn.commit()


async def apply_tx(
    conn: sqlite3.Connection,
    tx: dict[str, Any],
    fetch_token_fn: FetchTokenFn,
    fetch_meta_fn: FetchMetaFn,
    is_ours: Callable[[dict[str, Any]], bool] | None = None,
) -> None:
    """Update the index for one NFToken transaction. Burn flips the flag (only on
    tokens already in the index); mint/accept/modify (re)fetch the token's current
    owner/flags/uri (nft_info — the Kinesis pattern) and its metadata, then upsert.
    `is_ours(token)` scopes upserts to the collection (skips foreign NFTs the
    network-wide stream carries). Per-id errors are logged, never raised, so a bad
    tx can't kill the stream."""
    kind = classify_tx(tx)
    if kind is None:
        return
    for nft_id in affected_nft_ids(tx):
        try:
            if kind == "burn":
                _set_burned(conn, nft_id)
                continue
            token = await fetch_token_fn(nft_id)
            if not token:
                logging.warning(f"apply_tx: could not resolve token {nft_id} ({kind})")
                continue
            if is_ours is not None and not is_ours(token):
                continue  # NFT outside our collection; ignore
            uri_hex = token.get("uri_hex") or ""
            metadata = await fetch_meta_fn(uri_hex) if uri_hex else None
            nft_index.upsert(conn, nft_index.token_record(token, metadata))
        except Exception:
            logging.exception(f"apply_tx failed for {nft_id} ({kind})")


def _apply_closet(conn: sqlite3.Connection, token: dict[str, Any], metadata: Any) -> None:
    """Rebuild an owner's closet_assets/closet_bodies rows from their Closet
    NFToken's metadata and set its lifecycle status: a token held by anyone other
    than the issuer has been accepted (active); one still in the issuer wallet is
    pending_accept. The offer_id is NOT on-chain — preserve any stored value so
    the UI can re-show the pending accept QR."""
    owner = token.get("owner")
    if not owner:
        return
    assets, bodies = closet_token.parse_closet_metadata(
        metadata if isinstance(metadata, dict) else {}
    )
    economy_store.set_closet_contents(conn, owner, assets, bodies)
    status = (
        closet_token.ACTIVE if owner != config.SWAP_ISSUER_ADDRESS else closet_token.PENDING_ACCEPT
    )
    existing = economy_store.get_closet_record(conn, owner)
    existing_offer_id = existing[3] if existing is not None else None
    economy_store.set_closet_token(
        conn,
        owner,
        token["nft_id"],
        token.get("uri_hex") or "",
        status=status,
        offer_id=existing_offer_id,
    )


def _apply_possible_growth(
    conn: sqlite3.Connection, token: dict[str, Any], metadata: Any, genesis: trait_economy.Genesis
) -> None:
    """Record a supply_changes row when a character mint introduces an edition
    not in the (effective) genesis — legitimate growth, so it never reads as
    drift. Reborn/known editions are already present and do nothing."""
    if not isinstance(metadata, dict):
        return
    attrs = swap_meta.normalize_attributes(metadata.get("attributes") or [])
    edition = swap_meta.extract_nft_number(str(metadata.get("name", "")))
    if edition is None or edition in genesis.edition_bodies:
        return
    deltas = {
        f"{slot}|{swap_meta.get_attr(attrs, slot) or 'None'}": 1
        for slot in trait_economy.NON_BODY_SLOTS
    }
    economy_store.record_supply_change(
        conn,
        "mint",
        edition,
        swap_meta.get_attr(attrs, "Body") or "",
        swap_meta.detect_body(attrs),
        deltas,
        "listener",
        f"new-edition mint {token['nft_id']}",
    )


async def apply_economy_tx(
    conn: sqlite3.Connection,
    tx: dict[str, Any],
    *,
    fetch_token_fn: FetchTokenFn,
    fetch_meta_fn: FetchMetaFn,
    genesis: trait_economy.Genesis,
) -> None:
    """Apply a Mint/Modify/Accept to the trait-economy tables. A Closet NFToken
    (taxon == config.CLOSET_TAXON or config.LEGACY_BUCKET_TAXON) rebuilds its
    owner's closet from metadata and, on accept, promotes pending_accept → active;
    a character mint of an unknown edition appends a supply_changes row. Per-id
    errors are logged, never raised. `genesis` must be the EFFECTIVE genesis so
    already-recorded editions are recognised (idempotent)."""
    kind = classify_tx(tx)
    if kind not in ("mint", "modify", "accept"):
        return
    for nft_id in affected_nft_ids(tx):
        try:
            token = await fetch_token_fn(nft_id)
            if not token:
                continue
            uri_hex = token.get("uri_hex") or ""
            metadata = await fetch_meta_fn(uri_hex) if uri_hex else None
            if int(token.get("taxon") or -1) in (config.CLOSET_TAXON, config.LEGACY_BUCKET_TAXON):
                _apply_closet(conn, token, metadata)
            elif kind == "mint":
                _apply_possible_growth(conn, token, metadata, genesis)
        except Exception:
            logging.exception(f"apply_economy_tx failed for {nft_id} ({kind})")
