# lfg_core/nft_listener.py
# Apply live XRPL NFToken transactions to the per-nft_id on-chain index, keeping
# it fresh as the chain changes. Handles Mint / AcceptOffer (ownership) / Burn /
# Modify (in-place trait change — the case LFG swaps produce). Pure classifiers
# plus an apply_tx that takes injected resolvers so it is unit-testable.

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from xrpl.core import addresscodec

from lfg_core import (
    closet_token,
    config,
    economy_store,
    market_ops,
    market_store,
    nft_index,
    swap_meta,
    trait_economy,
    trait_token,
)

_TYPE_TO_KIND = {
    "NFTokenMint": "mint",
    "NFTokenAcceptOffer": "accept",
    "NFTokenBurn": "burn",
    "NFTokenModify": "modify",
    "NFTokenCreateOffer": "offer_create",
    "NFTokenCancelOffer": "offer_cancel",
}

# Resolver signatures used by apply_tx (injected so tests need no network):
#   fetch_token_fn(nft_id) -> {nft_id, owner, flags, uri_hex, is_burned} | None
#   fetch_meta_fn(uri_hex) -> metadata dict | None
FetchTokenFn = Callable[[str], Awaitable[dict[str, Any] | None]]
FetchMetaFn = Callable[[str], Awaitable[dict[str, Any] | None]]


def classify_tx(tx: dict[str, Any]) -> str | None:
    """Map an NFToken transaction to mint/accept/burn/modify, or None."""
    return _TYPE_TO_KIND.get(str(tx.get("TransactionType", "")))


def tx_succeeded(tx: dict[str, Any], *, log_skip_as: str | None = None) -> bool:
    """True iff a normalized tx actually executed: meta.TransactionResult is
    exactly "tesSUCCESS". tec-class transactions are validated into the ledger
    but only claim a fee — the operation did NOT happen — so every derive/apply
    entry point gates on this before mutating anything (#210/#235). Strict: a
    missing meta or result code is not provably successful and fails the gate
    (real ledger records always carry it). Raw ARCHIVING stays result-agnostic
    (backfill_history stores tec txs for audit) — only event derivation and
    index/economy/market mutation are gated. With `log_skip_as` set, a failing
    tx logs one concise debug line; the listener consumes the network-wide
    firehose where tec traffic is routine, so never warn per tx."""
    result = (tx.get("meta") or {}).get("TransactionResult")
    if result == "tesSUCCESS":
        return True
    if log_skip_as is not None:
        logging.debug(f"{log_skip_as}: skipping non-tesSUCCESS tx {tx.get('hash')} ({result})")
    return False


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
    tx can't kill the stream. A non-tesSUCCESS tx performed nothing on-ledger
    and must not touch the index (#210: a tecNO_ENTRY burn once flipped
    is_burned on a live token)."""
    kind = classify_tx(tx)
    if kind is None:
        return
    if not tx_succeeded(tx, log_skip_as="apply_tx"):
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
            # Warm the uri metadata cache with the fetch we just paid for:
            # the local-first roster serves cache hits with the token's REAL
            # metadata (incl. burnCount for swap outputs) and never fetches
            # inline, so a fresh mint/modify must be cached by browse time.
            if isinstance(metadata, dict):
                nft_index.meta_cache_put_many(conn, {uri_hex: metadata})
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


def _apply_trait_token(
    conn: sqlite3.Connection, kind: str, token: dict[str, Any], metadata: Any
) -> None:
    """Maintain the trait_tokens table from a standalone trait NFToken's chain
    events: mint/accept upsert (current owner), burn deletes."""
    nft_id = token["nft_id"]
    if kind == "burn" or token.get("is_burned"):
        economy_store.delete_trait_token(conn, nft_id)
        return
    owner = token.get("owner")
    parsed = trait_token.parse_trait_metadata(metadata if isinstance(metadata, dict) else {})
    if owner and parsed:
        economy_store.upsert_trait_token(conn, nft_id, owner, parsed[0], parsed[1])


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
    genesis: trait_economy.Genesis | None = None,
) -> None:
    """Apply a Mint/Modify/Accept/Burn to the trait-economy tables. A Closet NFToken
    (taxon == config.CLOSET_TAXON or config.LEGACY_BUCKET_TAXON) rebuilds its
    owner's closet from metadata and, on accept, promotes pending_accept → active;
    a standalone trait NFToken (taxon == config.TRAIT_TAXON) is upserted on
    mint/accept or deleted on burn. Closet/trait mirror maintenance runs regardless
    of genesis. Only the supply-growth path (a character mint of an unknown edition)
    needs `genesis`; pass the EFFECTIVE genesis (so recorded editions are recognised)
    to enable it, or `None` when no genesis is frozen to skip growth.

    Every write path is gated on `token["issuer"] == config.SWAP_ISSUER_ADDRESS`:
    the taxon is forgeable, so a foreign-issuer NFT reusing our economy taxons — or
    a foreign `#N`-named mint — must never forge closet/trait rows or poison the
    supply ledger. Per-id errors are logged, never raised. A non-tesSUCCESS tx
    performed nothing on-ledger and must not touch the economy tables (a tec
    burn would delete a live trait_tokens row; a tec mint could log spurious
    supply growth)."""
    kind = classify_tx(tx)
    if kind not in ("mint", "modify", "accept", "burn"):
        return
    if not tx_succeeded(tx, log_skip_as="apply_economy_tx"):
        return
    for nft_id in affected_nft_ids(tx):
        try:
            if kind == "burn":
                # A burn may leave nft_info returning None (token gone from ledger),
                # so route the burn by nft_id alone. delete_trait_token is idempotent
                # and a no-op for non-trait tokens; characters/closets need no economy
                # action on burn (the harvest flow already updated the Closet).
                economy_store.delete_trait_token(conn, nft_id)
                continue
            token = await fetch_token_fn(nft_id)
            if not token:
                continue
            # Trust only tokens minted by OUR issuer. The taxon is attacker-
            # controlled, so routing on it alone lets a foreign-issuer NFT reusing
            # CLOSET_TAXON/TRAIT_TAXON forge closet/trait rows, and a foreign #N-named
            # mint poison the supply-growth ledger. Mirror the market membership gate
            # (_nft_id_is_ours) — the issuer bytes are the authoritative collection
            # signal — and drop anything not from us before any economy write.
            if token.get("issuer") != config.SWAP_ISSUER_ADDRESS:
                continue
            uri_hex = token.get("uri_hex") or ""
            metadata = await fetch_meta_fn(uri_hex) if uri_hex else None
            taxon = int(token.get("taxon") or -1)
            if taxon in (config.CLOSET_TAXON, config.LEGACY_BUCKET_TAXON):
                _apply_closet(conn, token, metadata)
            elif taxon == config.TRAIT_TAXON:
                _apply_trait_token(conn, kind, token, metadata)
            elif kind == "mint" and genesis is not None:
                _apply_possible_growth(conn, token, metadata, genesis)
        except Exception:
            logging.exception(f"apply_economy_tx failed for {nft_id} ({kind})")


# --- In-app marketplace: offer_create / offer_cancel / accept ---------------
#
# Membership ("ours") is decided by lookup in onchain_nfts / trait_tokens,
# NEVER by decoding the taxon out of the NFTokenID -- XLS-20 obfuscation
# scrambles it. Only the issuer's account bytes (hex chars 8..48) are
# unscrambled, so they are usable as a cheap pre-filter (below), but the DB
# membership check remains the actual gate.

_RIPPLE_EPOCH = 946684800


def _issuer_account_hex() -> str:
    """40-hex uppercase AccountID for our NFT-collection issuer
    (config.SWAP_ISSUER_ADDRESS), as embedded in NFTokenIDs at hex chars
    8..48."""
    return addresscodec.decode_classic_address(config.SWAP_ISSUER_ADDRESS).hex().upper()


def _nft_id_is_ours(nft_id: str, issuer_hex: str) -> bool:
    """Cheap pre-filter only: the issuer bytes of an NFTokenID are NOT
    scrambled, so this short-circuits the overwhelmingly-foreign network-wide
    tx firehose before any DB round-trip. `_classify_membership`'s table
    lookup remains the authoritative gate -- an our-issuer nft_id we never
    indexed still resolves to "not ours" there."""
    return isinstance(nft_id, str) and len(nft_id) == 64 and nft_id[8:48].upper() == issuer_hex


def _classify_membership(
    conn: sqlite3.Connection, nft_id: str
) -> tuple[str, str | None, str | None] | None:
    """(kind, slot, value) for a market listing's nft_id, or None if it isn't
    ours. `onchain_nfts` membership -> ('character', None, None); else
    `trait_tokens` membership -> ('trait', slot, value) from that row. Neither
    (including a foreign-issuer nft_id, or an our-issuer nft_id we simply
    never indexed) -> None."""
    if not _nft_id_is_ours(nft_id, _issuer_account_hex()):
        return None
    if conn.execute("SELECT 1 FROM onchain_nfts WHERE nft_id=?", (nft_id,)).fetchone():
        return ("character", None, None)
    row = conn.execute("SELECT slot, value FROM trait_tokens WHERE nft_id=?", (nft_id,)).fetchone()
    if row is not None:
        return ("trait", str(row[0]), str(row[1]))
    return None


def _tx_unix_time(tx: dict[str, Any]) -> int | None:
    """Best-effort unix timestamp for a streamed tx envelope: the ripple-epoch
    `date` field (nft_history / clio entries) or `close_time_iso` (the live
    subscribe stream). Deliberately duplicated from history_events.tx_unix_time
    rather than imported -- history_events imports nft_listener, so importing
    it back here would be circular."""
    date = tx.get("date")
    if isinstance(date, int):
        return date + _RIPPLE_EPOCH
    iso = tx.get("close_time_iso")
    if isinstance(iso, str):
        try:
            return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _deleted_nft_offer_nodes(tx: dict[str, Any]) -> list[dict[str, Any]]:
    """Every DeletedNode wrapper of LedgerEntryType NFTokenOffer in a tx's
    meta.AffectedNodes."""
    meta = tx.get("meta")
    nodes = meta.get("AffectedNodes") if isinstance(meta, dict) else None
    if not isinstance(nodes, list):
        return []
    out: list[dict[str, Any]] = []
    for node in nodes:
        wrapper = node.get("DeletedNode") if isinstance(node, dict) else None
        if isinstance(wrapper, dict) and wrapper.get("LedgerEntryType") == "NFTokenOffer":
            out.append(wrapper)
    return out


def _apply_offer_create(conn: sqlite3.Connection, tx: dict[str, Any]) -> None:
    """NFTokenCreateOffer: upsert a live market_listings row iff the created
    offer is sell-flagged, XRP-denominated (market_ops.extract_created_sell_offer
    already enforces both), and `nft_id` is ours by membership."""
    nft_id = tx.get("NFTokenID")
    if not isinstance(nft_id, str) or not nft_id:
        return
    membership = _classify_membership(conn, nft_id)
    if membership is None:
        return
    kind, slot, value = membership
    seller = tx.get("Account")
    if not isinstance(seller, str) or not seller:
        return
    meta_raw = tx.get("meta")
    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    extracted = market_ops.extract_created_sell_offer(meta, nft_id)
    if extracted is None:
        return  # buy offer, IOU amount, or no matching CreatedNode
    offer_index = extracted.get("offer_index")
    if not isinstance(offer_index, str) or not offer_index:
        return
    market_store.upsert_listing(
        conn,
        market_store.MarketListing(
            offer_index=offer_index,
            nft_id=nft_id,
            kind=kind,
            seller=seller,
            amount_drops=extracted["amount_drops"],  # already an int (extract_created_sell_offer)
            destination=extracted.get("destination"),
            slot=slot,
            value=value,
            created_ledger=tx.get("ledger_index"),
            created_ts=_tx_unix_time(tx),
            is_live=1,
        ),
    )


def _close_deleted_offers(conn: sqlite3.Connection, tx: dict[str, Any], reason: str) -> None:
    """Close the market_listings row (if any) behind every NFTokenOffer ledger
    object this tx deleted. A DeletedNode for an offer_index we never indexed
    is a harmless no-op -- close_listing tolerates an unknown offer_index.
    Per-item errors are logged, never raised, so one bad deleted-offer entry
    can't abort the rest of the tx's deletions (same isolation convention as
    apply_market_tx, one level down)."""
    for wrapper in _deleted_nft_offer_nodes(tx):
        offer_index = wrapper.get("LedgerIndex")
        if not (isinstance(offer_index, str) and offer_index):
            continue
        try:
            market_store.close_listing(conn, offer_index, reason)
        except Exception:
            logging.exception(
                f"deleted-offer close failed (offer_index={offer_index}, reason={reason})"
            )


def _apply_offer_cancel(conn: sqlite3.Connection, tx: dict[str, Any]) -> None:
    """NFTokenCancelOffer: every deleted NFTokenOffer ledger object closes its
    market_listings row (if any) as cancelled."""
    _close_deleted_offers(conn, tx, "cancelled")


def _owner_of(conn: sqlite3.Connection, nft_id: str) -> str | None:
    """Current owner-of-record for `nft_id` from whichever membership table
    carries it (onchain_nfts for characters, trait_tokens for traits)."""
    row = conn.execute("SELECT owner FROM onchain_nfts WHERE nft_id=?", (nft_id,)).fetchone()
    if row is not None:
        return row[0]  # type: ignore[no-any-return]
    row = conn.execute("SELECT owner FROM trait_tokens WHERE nft_id=?", (nft_id,)).fetchone()
    return row[0] if row is not None else None


def _accept_new_owner(
    tx: dict[str, Any], deleted_offers: list[dict[str, Any]], seller: str | None
) -> str | None:
    """The account that RECEIVES the NFT in an NFTokenAcceptOffer, read from the
    transaction itself (authoritative, index-independent). In a brokered accept
    both a sell and a buy offer are deleted and the new owner is the buy offer's
    `Owner`; in a direct sell-offer accept only the sell offer is deleted and the
    accepting account (`tx.Account`) is the new owner. Returns None only when the
    result can't be resolved to a real non-seller account (e.g. a malformed tx)."""
    buy_wrapper = next(
        (
            w
            for w in deleted_offers
            if not (
                int((w.get("FinalFields") or {}).get("Flags") or 0) & market_ops.LSF_SELL_NFTOKEN
            )
        ),
        None,
    )
    if buy_wrapper is not None:
        new_owner = (buy_wrapper.get("FinalFields") or {}).get("Owner")
    else:
        new_owner = tx.get("Account")
    if isinstance(new_owner, str) and new_owner and new_owner != seller:
        return new_owner
    return None


def _apply_offer_accept(conn: sqlite3.Connection, tx: dict[str, Any]) -> None:
    """NFTokenAcceptOffer: close the deleted SELL offer's market_listings row
    as sold (close_listing itself sets settled=0 for a trait-kind row), then
    delist any OTHER live row for the same nft_id whose seller no longer
    matches the new owner -- a stale sell offer left behind by the previous
    owner can no longer be honoured."""
    deleted = _deleted_nft_offer_nodes(tx)
    sell_wrapper = next(
        (
            w
            for w in deleted
            if int((w.get("FinalFields") or {}).get("Flags") or 0) & market_ops.LSF_SELL_NFTOKEN
        ),
        None,
    )
    if sell_wrapper is None:
        return  # a buy-offer-only accept; no sell listing of ours to close
    offer_index = sell_wrapper.get("LedgerIndex")
    final = sell_wrapper.get("FinalFields") or {}
    nft_id = final.get("NFTokenID")
    seller = final.get("Owner")
    # Resolve the post-transfer owner (the buyer) from the ACCEPT TX itself so
    # the sold row can carry the buyer durably — the settlement sweep needs it
    # after run_deposit deletes the token's trait_tokens ownership row. Reading
    # the tx (not the local owner index) makes this independent of whether the
    # listener's owner refresh has landed: a stale index can no longer leave the
    # row without a durable buyer, nor persist the seller by mistake.
    buyer = _accept_new_owner(tx, deleted, seller)
    if isinstance(offer_index, str) and offer_index:
        market_store.close_listing(conn, offer_index, "sold", buyer=buyer)

    if not isinstance(nft_id, str) or not nft_id:
        return
    # Stale-delist comparison uses the same tx-derived new owner when known,
    # falling back to the owner index only if the tx couldn't resolve it.
    new_owner = buyer if buyer is not None else _owner_of(conn, nft_id)
    if new_owner is None:
        return  # unresolved owner; leave other rows alone rather than guess
    rows = conn.execute(
        "SELECT offer_index, seller FROM market_listings WHERE nft_id=? AND is_live=1",
        (nft_id,),
    ).fetchall()
    for other_offer_index, other_seller in rows:
        if other_seller != new_owner:
            market_store.close_listing(conn, other_offer_index, "stale")


async def apply_market_tx(conn: sqlite3.Connection, tx: dict[str, Any]) -> None:
    """Update market_listings for one streamed tx: `offer_create` upserts a
    live listing (sell-flagged, XRP, ours by membership -- never taxon-from-
    ID); `offer_cancel` closes every deleted NFTokenOffer as cancelled;
    `accept` closes the deleted sell offer as sold (settled=0 for a trait row,
    handled by close_listing itself) and delists any other live row for that
    nft_id whose seller no longer owns it, as stale. A no-op for every other
    tx kind. Declared async for interface symmetry with apply_tx /
    apply_economy_tx (same conn/tx call-site convention in
    scripts/onchain_listener.py) even though this function itself performs no
    I/O. Per-tx errors are logged, never raised, so one bad tx can't kill the
    stream (same convention as apply_tx / apply_economy_tx). A non-tesSUCCESS
    tx must never upsert a listing or close one as sold, but it is NOT always
    zero-side-effect: since fixExpiredNFTokenOfferRemoval (fixCleanup3_1_3,
    mainnet 2026-05) an accept of an expired NFTokenOffer fails tecEXPIRED yet
    still DELETES the expired offer(s) — real DeletedNode entries in the failed
    tx's validated meta. The gate's failure path honours that ledger truth by
    stale-closing the rows behind any deleted offers before returning."""
    kind = classify_tx(tx)
    if kind not in ("offer_create", "offer_cancel", "accept"):
        return
    if not tx_succeeded(tx, log_skip_as="apply_market_tx"):
        if kind == "accept":
            # Close as stale, never sold: there is no buyer, and a trait row
            # must not enter the settled=0 settlement lifecycle. Keyed on the
            # DeletedNodes themselves (ledger truth regardless of result code)
            # rather than tecEXPIRED specifically; a failed create/cancel
            # deletes no offers, so no other kind needs this.
            _close_deleted_offers(conn, tx, "stale")
        return
    try:
        if kind == "offer_create":
            _apply_offer_create(conn, tx)
        elif kind == "offer_cancel":
            _apply_offer_cancel(conn, tx)
        else:
            _apply_offer_accept(conn, tx)
    except Exception:
        logging.exception(f"apply_market_tx failed ({kind})")
