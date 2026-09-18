# lfg_core/closet_token.py
# The per-user on-ledger Closet NFToken. Its metadata JSON is the authoritative
# on-chain record of a user's loose assets + bodies (the DB tables mirror it).
# This module builds/parses that metadata (pure) and wraps the mint-on-first-use
# + modify lifecycle (injectable, so tests need no network).

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lfg_core import config, economy_store, history_store, owner_lock

PENDING_ACCEPT = "pending_accept"
ACTIVE = "active"

# ClosetError.code of the stale-mirror refusal (#522).
CLOSET_MIRROR_BEHIND = "closet_mirror_behind"
MIRROR_BEHIND_MESSAGE = (
    "Your Closet is still catching up with a recent change on the ledger. "
    "Nothing was changed — please try again shortly."
)

# Asset triples are (slot, value, count); bodies are edition ints.
Asset = tuple[str, str, int]


@dataclass(frozen=True)
class ModifyReceipt:
    """A validated Closet NFTokenModify: its hash and the ledger it validated in
    (None when the submit result did not say). The ledger stamps the mirrored
    version so the listener applies Closet versions monotonically (#522)."""

    tx_hash: str
    ledger_index: int | None = None


# Injected XRPL/CDN operations (real wrappers in EconomyDeps; fakes in tests):
UploadFn = Callable[[dict[str, Any]], Awaitable[str]]  # metadata dict -> CDN url
MintFn = Callable[[str], Awaitable[str | None]]  # url -> nft_id
OfferFn = Callable[[str, str], Awaitable[str | None]]  # (nft_id, owner) -> offer_id
AcceptFn = Callable[[str], Awaitable[dict[str, Any] | None]]  # offer_id -> XUMM payload
ModifyFn = Callable[[str, str, str], Awaitable[str | None]]  # (nft_id, owner, url) -> tx hash
# The Closet modify may also report the ledger it validated in.
ClosetModifyFn = Callable[[str, str, str], Awaitable[str | ModifyReceipt | None]]
ExistsFn = Callable[[str], Awaitable[bool]]  # nft_id -> does it exist on-ledger?
OwnerFn = Callable[[str], Awaitable[str | None]]  # nft_id -> current owner address or None


class ClosetError(RuntimeError):
    """A Closet NFToken lifecycle step (mint/offer/modify) failed.

    Raised plain (not a subclass below), it means the on-chain change was NOT
    committed — compensating on-chain actions are safe. `code` names a failure
    a caller may branch on (e.g. CLOSET_MIRROR_BEHIND); None otherwise."""

    def __init__(self, *args: object, code: str | None = None) -> None:
        super().__init__(*args)
        self.code = code


class ClosetMirrorError(ClosetError):
    """The on-chain NFTokenModify COMMITTED; only a local DB write failed.
    Do NOT run on-chain compensation — reconcile from chain instead (the
    listener rebuilds the mirror from the token's metadata)."""

    def __init__(self, msg: str, tx_hash: str):
        super().__init__(msg)
        self.tx_hash = tx_hash


class ClosetIndeterminateError(ClosetError):
    """modify_fn raised; whether the modify committed is unknown.
    Fail-closed: no on-chain compensation, admin/listener reconciliation."""


@dataclass
class ClosetRef:
    """A user's Closet NFToken. `accept_payload` is set when the closet was
    just minted or is pending acceptance (the user must accept the offer to take
    custody); it is None for an already-active closet or when the XUMM payload
    could not be built."""

    nft_id: str
    uri_hex: str
    status: str = PENDING_ACCEPT
    accept_payload: dict[str, Any] | None = None
    minted: bool = False


def _hex(url: str) -> str:
    return url.encode("utf-8").hex().upper()


def build_closet_metadata(
    owner: str, assets: list[Asset], bodies: list[int], orders: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The Closet NFToken metadata JSON. `lfg_closet` enumerates the loose
    contents deterministically (assets sorted by (slot, value)) so the same
    state always produces byte-identical metadata.

    Schema v2: bodies are ordinary `slot="Body"` rows inside `assets` (keyed by
    body VALUE, e.g. "Milady", not by edition number), so `"bodies"` is normally
    written empty. It carries integer editions only when a caller explicitly
    passes UNRESOLVED legacy editions (ones not yet convertible via the frozen
    genesis) — those must stay on the authoritative token until resolvable
    rather than be dropped.

    `orders` (#443) is an optional display-only block of the owner's open
    Closet Market orders — never parsed back (parse_closet_metadata ignores
    it) and omitted entirely when empty, so existing metadata is unchanged."""
    meta: dict[str, Any] = {
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
            # `type(b) is int` (not isinstance): bool subclasses int, so an
            # untrusted True/False would otherwise serialize as edition 1/0.
            "bodies": sorted(b for b in bodies if type(b) is int),
        },
    }
    if orders:
        # #443: open Closet Market orders, display-only. parse_closet_metadata
        # ignores this key; closet_orders in the DB stays authoritative.
        meta["lfg_closet"]["orders"] = orders
    return meta


def parse_closet_metadata(
    meta: dict[str, Any], genesis: Any | None = None
) -> tuple[list[Asset], list[int]]:
    """Inverse of build_closet_metadata: read (assets, legacy_editions) back out
    of a Closet NFToken's metadata. Tolerant of missing/garbage fields —
    anything malformed yields empty lists rather than raising (the listener
    consumes untrusted on-chain metadata). Tries lfg_closet first, falls back
    to lfg_bucket.

    Schema v2: `"bodies"` may still hold legacy integer editions from a
    pre-migration token. When `genesis` is given, each KNOWN legacy edition is
    resolved via `genesis.edition_bodies` into a `("Body", value, count)` asset
    row; editions NOT in the genesis are UNRESOLVED and returned in
    `legacy_editions` (never dropped — the token is authoritative, so a
    listener rebuild that discarded them would permanently lose those bodies).
    Without a genesis, all legacy editions are returned unconverted so a caller
    can decide how to handle them."""
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
    legacy_editions: list[int] = []
    raw_bodies = block.get("bodies")
    if isinstance(raw_bodies, list):
        # `type(b) is int` (not isinstance): exclude bools before any genesis
        # resolution — a True in an untrusted on-chain list must never parse as
        # edition 1 and inject a Body asset.
        legacy_editions = [b for b in raw_bodies if type(b) is int]
    if genesis is not None and legacy_editions:
        from collections import Counter

        body_counts: Counter[str] = Counter()
        unresolved: list[int] = []
        for edition in legacy_editions:
            pair = genesis.edition_bodies.get(edition)
            if pair:
                body_counts[pair[0]] += 1
            else:
                unresolved.append(edition)
        assets += [("Body", value, count) for value, count in sorted(body_counts.items())]
        # Keep UNKNOWN editions on-record so a listener rebuild doesn't lose them.
        legacy_editions = sorted(unresolved)
    return assets, legacy_editions


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
    """Return the owner's Closet, minting on first use. A fresh Closet is minted
    empty, offered to the owner, and recorded `pending_accept` with its offer id.
    A recorded but on-ledger-absent Closet (verified via `exists_fn`) is treated
    as stale and re-minted. While pending, this is idempotent and regenerates the
    Xaman accept payload (from the stored offer id, or a fresh offer when that id
    is missing/unusable — offer ids are not on-chain) so the UI can re-show it.

    Serialized per owner (#180): the check-then-mint (`get_closet_record` -> mint
    -> `set_closet_token`) is a read-modify-write with a window where a double-tap
    or a register/start-closet race could each see "no Closet" and both mint one.
    The per-owner lock (the same one the economy flows hold) collapses that to a
    single winner — the second caller re-reads the now-recorded Closet."""
    async with owner_lock.owner_lock(owner):
        existing = economy_store.get_closet_record(conn, owner)
        if existing is not None:
            nft_id, uri_hex, status, offer_id = existing
            stale = exists_fn is not None and not await exists_fn(nft_id)
            if not stale:
                payload = None
                if status == PENDING_ACCEPT:
                    # Re-show the Xaman accept for a pending Closet. The offer id is NOT
                    # on-chain, so a listener-rebuilt record can have lost it (offer_id
                    # is None), and a stored offer can expire. If we can't regenerate a
                    # payload from the recorded offer, create a FRESH offer for the
                    # existing (still issuer-held) token and persist the new id so the
                    # QR works again.
                    if offer_id:
                        payload = await accept_payload_fn(offer_id)
                    if payload is None:
                        new_offer_id = await offer_fn(nft_id, owner)
                        # Mirror the first-time path: a falsey id is a real offer
                        # failure — raise so the caller surfaces it rather than
                        # silently returning accept=None (the bug this path fixes).
                        # (A truthy self-offer-skipped sentinel passes through and
                        # legitimately yields no payload — the issuer needs no accept.)
                        if not new_offer_id:
                            raise ClosetError("failed to create a fresh Closet offer to owner")
                        payload = await accept_payload_fn(new_offer_id)
                        economy_store.set_closet_token(
                            conn, owner, nft_id, uri_hex, status=status, offer_id=new_offer_id
                        )
                return ClosetRef(
                    nft_id=nft_id, uri_hex=uri_hex, status=status, accept_payload=payload
                )

        url = await upload_fn(build_closet_metadata(owner, [], []))
        new_nft_id = await mint_fn(url)
        if not new_nft_id:
            raise ClosetError("failed to mint Closet NFToken")
        nft_id = new_nft_id
        # Record the minted token BEFORE the offer step (#386 review): the mint
        # has committed on-chain, so a failure past this point must not look
        # like "nothing happened" — a retry that saw no record would mint a
        # duplicate issuer-held Closet. With the record in place, a retry
        # re-enters the pending_accept self-heal path above and creates a
        # fresh offer for THIS token instead of re-minting.
        try:
            economy_store.set_closet_token(
                conn, owner, nft_id, _hex(url), status=PENDING_ACCEPT, offer_id=None
            )
        except Exception as e:
            # The mint committed on-chain but the record write itself failed:
            # there is now NO durable pointer to the minted token, so a later
            # request would follow the no-record path and mint a duplicate.
            # Fail closed as indeterminate-class — the service maps this to an
            # opaque 502 with no retryable flag, never an advertised retry.
            raise ClosetIndeterminateError(
                f"Closet mint {nft_id} committed but recording it failed: {e}"
            ) from e
        offer_id = await offer_fn(nft_id, owner)
        if not offer_id:
            raise ClosetError("failed to offer Closet NFToken to owner")
        payload = await accept_payload_fn(offer_id)  # None is non-fatal (accept later)
        economy_store.set_closet_token(
            conn, owner, nft_id, _hex(url), status=PENDING_ACCEPT, offer_id=offer_id
        )
        return ClosetRef(
            nft_id=nft_id,
            uri_hex=_hex(url),
            status=PENDING_ACCEPT,
            accept_payload=payload,
            minted=True,
        )


async def confirm_accept(conn: Any, owner: str, *, owner_fn: OwnerFn) -> str:
    """Promote `pending_accept → active` once the Closet is owned by `owner`
    (offer accepted on-ledger). Returns the resulting status; `none` if no Closet
    is recorded. Idempotent."""
    rec = economy_store.get_closet_record(conn, owner)
    if rec is None:
        return "none"
    nft_id, _uri, status, _offer = rec
    if status == ACTIVE:
        return ACTIVE
    if await owner_fn(nft_id) == owner:
        economy_store.set_closet_status(conn, owner, ACTIVE)
        return ACTIVE
    return status


def _orders_for_meta(conn: Any, owner: str) -> list[dict[str, Any]] | None:
    """Best-effort: the orders block is display-only, so a read failure (a
    test double conn, a DB without the table) must never block a Closet sync."""
    from lfg_core import closet_market_store  # local: closet_market_store must stay import-light

    try:
        return closet_market_store.open_orders_for_meta(conn, owner)
    except (sqlite3.Error, AttributeError, TypeError):
        logging.warning(f"closet orders unreadable for {owner}; syncing without an orders block")
        return None


def ensure_mirror_current(conn: Any, owner: str, *, history_db_path: str | None) -> None:
    """Refuse a full Closet overwrite while the owner's mirror is behind the
    chain (#522).

    The economy flows rebuild the whole Closet token from the local mirror
    (`closet_assets`); a mirror missing a version that is already on-chain makes
    that overwrite erase it. The chain-side reference is the ledger history
    archive: the listener derives it from the tx stream, so it cannot lag a
    validated modify the way clio `nft_info` can. The mirror is behind when the
    archive's newest version of the Closet is newer than the version the mirror
    holds — dated by the mirror's ledger stamp or by the ledger at which the
    archive saw the mirror's URI. A mirror that is the newest archived version,
    or whose version nothing dates, is not refused.

    Raises a plain, not-committed ClosetError with code CLOSET_MIRROR_BEHIND:
    call it before anything is submitted; the user retries once the listener
    catches up. An unavailable archive keeps today's behavior (no check) and
    logs a WARNING; `history_db_path=None` means no archive is wired."""
    if history_db_path is None:
        return
    record = economy_store.get_closet_record(conn, owner)
    if record is None:
        return
    nft_id, mirror_uri, _status, _offer_id = record
    try:
        with contextlib.closing(history_store.connect_readonly(history_db_path)) as archive:
            newest = history_store.latest_uri_version(archive, nft_id)
            seen = history_store.uri_version_ledger(archive, nft_id, mirror_uri)
    except (OSError, sqlite3.Error) as e:
        logging.warning(
            f"Closet stale-mirror check skipped for {owner}: history archive "
            f"{history_db_path} unavailable ({e})"
        )
        return
    if newest is None:
        return
    dated = [
        ledger
        for ledger in (economy_store.get_closet_applied_ledger(conn, owner), seen)
        if ledger is not None
    ]
    if not dated or newest.ledger_index <= max(dated):
        return
    logging.warning(
        f"Closet mirror for {owner} ({nft_id}) holds the ledger-{max(dated)} version but the "
        f"archive has ledger {newest.ledger_index}; refusing a full overwrite "
        f"({CLOSET_MIRROR_BEHIND})"
    )
    raise ClosetError(MIRROR_BEHIND_MESSAGE, code=CLOSET_MIRROR_BEHIND)


async def sync_closet(
    conn: Any,
    owner: str,
    assets: list[Asset],
    bodies: list[int],
    *,
    upload_fn: UploadFn,
    modify_fn: ClosetModifyFn,
) -> str:
    """Recompose the Closet NFToken's metadata from the given contents and
    NFTokenModify its URI in place (the token id is stable). Persists the new
    URI and returns the modify tx hash. When `modify_fn` returns a
    `ModifyReceipt`, the version's validated ledger is stamped with the URI
    (#522), so a lagging listener cannot roll this write back.

    Phase-aware failure taxonomy (#107):
    - plain ClosetError — the modify definitively did NOT commit (no record,
      or modify_fn returned falsy); on-chain compensation is safe.
    - ClosetIndeterminateError — modify_fn raised; commit status unknown.
    - ClosetMirrorError(tx_hash) — the modify COMMITTED but the local
      closet_tokens write failed (rolled back first, so nothing half-applied
      is left pending on the shared connection).
    A raise from upload_fn happens before any ledger effect and propagates
    RAW (callers treat it as ledger-failed)."""
    record = economy_store.get_closet_record(conn, owner)
    if record is None:
        raise ClosetError(f"no Closet NFToken on record for {owner}")
    nft_id, _uri_hex, status, offer_id = record
    url = await upload_fn(
        build_closet_metadata(owner, assets, bodies, _orders_for_meta(conn, owner))
    )
    try:
        result = await modify_fn(nft_id, owner, url)
    except Exception as e:
        raise ClosetIndeterminateError(
            f"Closet NFTokenModify outcome unknown (modify raised: {e})"
        ) from e
    tx_hash: str | None = result.tx_hash if isinstance(result, ModifyReceipt) else result
    ledger_index = result.ledger_index if isinstance(result, ModifyReceipt) else None
    if not tx_hash:
        raise ClosetError("failed to modify Closet NFToken URI")
    try:
        economy_store.set_closet_token(
            conn,
            owner,
            nft_id,
            _hex(url),
            status=status,
            offer_id=offer_id,
            applied_ledger_index=ledger_index,
        )
    except Exception as e:
        conn.rollback()
        raise ClosetMirrorError(
            f"Closet URI modified on-chain but the closet_tokens mirror write failed: {e}",
            tx_hash,
        ) from e
    return tx_hash
