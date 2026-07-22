# lfg_core/economy_flow.py
# Trait-economy operation flows (the swap_flow.py analogue for the dress-up
# game): harvest / assemble / equip as async state machines with on-disk
# journaling and partial-failure recovery. All on-chain effects go through
# injected callables (EconomyDeps) so the flows are unit-testable without a
# network; the CLI drivers wire the real xrpl_ops/cdn/xumm_ops/closet_token.
#
# Ordering principle: the irreversible character step is taken once everything
# reversible is in place, and the Closet NFToken (the on-chain source of truth)
# is always modified BEFORE the local DB mirror — so a crash leaves the DB
# rebuildable from the token by the listener, never the reverse.
#
# Phase-aware failure classification (#107): a failure around
# _sync_then_persist is typed by whether the on-chain Closet modify committed
# (closet_token.ClosetError = no; ClosetMirrorError(tx_hash) = yes, DB-only;
# ClosetIndeterminateError = unknown), and each flow picks its compensation
# accordingly. On-chain compensation (burn-back / modify-back) is ONLY safe on
# the ledger-failed branch. Records carry two sticky fields: `sync_tx_hash`
# (set the moment the modify commits) and `mirror_pending` (set on the
# mirror-failed branch, never cleared — it survives later-step statuses).
#
# Journal statuses and operator actions:
#
#   status                        meaning                          operator action
#   ---------------------------   ------------------------------   ----------------------------
#   <op>ing / burned / minted     progress checkpoints             none (in-flight)
#   complete                      fully done                       none
#   complete_pending_mirror       chain fully consistent; local    none — the listener rebuilds
#                                 DB mirror write failed           the mirror from the Closet
#                                                                  token (or restart/backfill)
#   harvested_pending_closet      ledger deposit did NOT commit    re-apply the deposit from the
#   deposited_pending_closet      (burn/blank already happened)    journal — safe, no
#                                                                  double-credit possible
#   <op>_sync_indeterminate       Closet modify outcome UNKNOWN    reconcile from chain (check
#                                 (modify raised mid-flight)       the Closet token's URI /
#                                                                  metadata) — NEVER blind
#                                                                  re-apply
#   failed_burn / failed_mint /   pre-ledger or reversible step    none (nothing lost) or
#   failed_modify / minted_no_*   failed; see the record error     re-offer per the record
#   reverted_mint /               ledger-failed drain/swap; the    none (compensated)
#   reverted_modify               on-chain compensation succeeded
#   failed_revert_mint /          compensation ALSO failed         admin: locate the journaled
#   failed_revert                                                  token and resolve manually
#
# Harvest-specific statuses (strip-to-blank, #<blank-harvest>):
#
#   harvesting                    assets snapshotted, no chain     none (in-flight)
#                                 effect yet
#   burned                        legacy path: burn committed,     none (in-flight) — the pair
#                                 -1 supply_changes row written     nets zero once reminted
#   reminted                      legacy path: blank remint +      none (in-flight) — offer/
#                                 +1 supply_changes row written,    accept surfaced to the user
#                                 delivery offer created
#   reminted_no_offer             legacy path: remint OK but the   intermediate checkpoint (has
#                                 delivery offer FAILED; journaled  new_nft_id) written BEFORE
#                                 BEFORE the Closet credit          the credit; not terminal
#   reminted_no_offer_failed      TERMINAL/FAILED: parts credited   admin: re-offer the reminted
#                                 to the Closet but the reminted    blank (record has new_nft_id
#                                 blank was never delivered — the   + sync_tx_hash) so the owner
#                                 session FAILS honestly, never a   can claim it; parts are
#                                 success screen                    already safe in the Closet
#   burned_no_remint               legacy path: burn committed,    admin: remint the edition as a
#                                 remint failed                    blank (journal has the -1 row)
#   failed_modify                 mutable path: NFTokenModify to   none (nothing changed)
#                                 blank failed pre-commit
#   reverted_modify                mutable path: deposit failed,   none (compensated — character
#                                 modify-back to the old URI OK    restored to its old traits)
#   harvest_sync_indeterminate    Closet deposit outcome UNKNOWN   reconcile from chain — NEVER
#                                 after burn/blank                blind re-apply
#   harvested_pending_closet       burn/blank committed, Closet    re-apply the deposit from the
#                                 deposit definitively did NOT     journal (moved_assets) — safe
#                                 commit
#
# Assemble-specific statuses (dress-a-blank-in-place, Task 6): assemble no
# longer mints/offers/accepts — it modifies a caller-owned BLANK in place.
#
#   assembling                    precheck passed, compose done,   none (in-flight)
#                                 no chain effect yet
#   modified                      NFTokenModify to the dressed     none (in-flight)
#                                 metadata committed
#   complete                      modify + Closet debit both       none
#                                 committed
#   complete_pending_mirror       modify + Closet debit committed  none — the listener rebuilds
#                                 on-chain; local DB mirror failed  the mirror from the token
#   reverted_modify                Closet debit definitively did   none (compensated — character
#                                 NOT commit; modify-back to the    modified back to its blank URI)
#                                 pre-assemble (blank) URI OK
#   failed_revert                 Closet debit failed AND the      admin: character may retain the
#                                 modify-back ALSO failed           new traits — reconcile manually
#   assemble_sync_indeterminate   Closet debit outcome UNKNOWN      reconcile from chain — NEVER
#                                 (modify raised mid-flight)        blind re-apply

from __future__ import annotations

import functools
import json
import logging
import os
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from lfg_core import closet_token as bt
from lfg_core import config, db_helpers, nft_index, owner_lock
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core import trait_token as tt
from lfg_core.nft_index import OnchainNft

# Any session that owns a Closet read-modify-write carries `.owner`; a `.state`
# machine driven by run_* below. Only used to type the serialization decorator.
_S = TypeVar("_S")


def _serialize_by_owner(
    runner: Callable[[_S, EconomyDeps], Awaitable[None]],
) -> Callable[[_S, EconomyDeps], Awaitable[None]]:
    """Hold the per-owner Closet lock (#180) for the WHOLE flow. sync_closet
    full-overwrites the Closet token, so two flows for one owner that interleave
    read -> modify -> mirror lose an update; serializing the entire op (a
    superset of read->sync->mirror) is the obviously-correct fix and still lets
    different owners run concurrently. The lock is per event loop, so this adds
    no cross-owner contention and no ordering constraint between wallets."""

    @functools.wraps(runner)
    async def wrapper(session: _S, deps: EconomyDeps) -> None:
        async with owner_lock.owner_lock(session.owner):  # type: ignore[attr-defined]
            await runner(session, deps)

    return wrapper


RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Character composition: (attributes, body_class, edition, rev) -> (image_url,
# video_url, metadata_url). The real impl composes layers + uploads image and
# metadata JSON; tests return canned URLs.
ComposeFn = Callable[[list[dict[str, str]], str, int, int], Awaitable[tuple[str, str | None, str]]]
BurnFn = Callable[[str, str], Awaitable[str | None]]  # (nft_id, owner) -> tx hash
TraitComposeFn = Callable[[str, str], Awaitable[str]]  # (slot, value) -> image_url
TraitInfoFn = Callable[
    [str], Awaitable[dict[str, Any] | None]
]  # nft_id -> {taxon, issuer, owner} | None
TraitMetaFn = Callable[[str], Awaitable[dict[str, Any] | None]]  # nft_id -> metadata dict | None


@dataclass
class EconomyDeps:
    """Injected operations. The closet_* callables are forwarded to
    closet_token.ensure_closet/sync_closet; the char_* callables act on the
    character NFToken; char_compose_fn builds+uploads image+metadata."""

    conn: Any  # sqlite3.Connection
    closet_upload_fn: bt.UploadFn
    closet_mint_fn: bt.MintFn
    closet_offer_fn: bt.OfferFn
    closet_accept_fn: bt.AcceptFn
    closet_modify_fn: bt.ModifyFn
    char_compose_fn: ComposeFn
    char_mint_fn: bt.MintFn
    char_modify_fn: bt.ModifyFn
    char_burn_fn: BurnFn
    char_offer_fn: bt.OfferFn
    char_accept_fn: bt.AcceptFn
    # Verifies a recorded Closet NFToken still exists on-ledger before it is
    # trusted (see closet_token.ensure_closet). Optional so existing test
    # constructions that omit it keep the legacy trust-the-record behavior.
    closet_exists_fn: bt.ExistsFn | None = None
    # Resolves the current owner of a Closet NFToken; used to promote
    # pending_accept → active before checking the precondition. Optional so
    # existing test constructions that omit it skip the confirmation step.
    closet_owner_fn: bt.OwnerFn | None = None
    trait_compose_fn: TraitComposeFn | None = None
    trait_upload_fn: bt.UploadFn | None = None
    trait_mint_fn: bt.MintFn | None = None
    trait_burn_fn: BurnFn | None = None
    # On-ledger trait token lookup fns (used by run_deposit for fail-closed
    # ownership/issuer checks before the irreversible burn).
    trait_info_fn: TraitInfoFn | None = None
    trait_meta_fn: TraitMetaFn | None = None
    # App-DB connection factory for rarity bookkeeping (#305): harvest burns
    # and assemble rebirths move characters in/out of the live population the
    # Trait Shop price formula reads. Optional so test/CLI constructions that
    # omit it simply skip the bookkeeping.
    app_conn_factory: Callable[[], Any] | None = None
    # Uploads blank-character metadata (silhouette image URL, blank_attributes(),
    # edition-numbered name) for the given edition and returns its metadata URL,
    # or None on failure. Optional/None so existing test constructions that omit
    # it keep compiling; real wiring lands in Task 8.
    blank_meta_fn: Callable[[int], Awaitable[str | None]] | None = None
    records_dir: str = config.ECONOMY_RECORDS_DIR


def _write_record(records_dir: str, op: str, session_id: str, record: dict[str, Any]) -> None:
    """Journal a flow's progress to disk (best-effort; the in-memory session
    does not survive a restart, this does — so an admin can recover a partial
    op)."""
    try:
        os.makedirs(records_dir, exist_ok=True)
        path = os.path.join(records_dir, f"{op}-{session_id}.json")
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
    except Exception:
        logging.error(f"Failed to write economy record: {traceback.format_exc()}")


def _apply_rarity_change(deps: EconomyDeps, op: str, apply: Callable[[Any], Any]) -> None:
    """Best-effort rarity bookkeeping in the app DB (#305): record the burn /
    revival and recount so the Trait Shop price reflects it immediately. Must
    never fail the economy session — the on-chain op already committed."""
    if deps.app_conn_factory is None:
        return
    try:
        from lfg_core import rarity  # local import: rarity pulls in db_helpers

        conn = deps.app_conn_factory()
        try:
            apply(conn)
            rarity.recalculate_rarity(conn, network=config.ECONOMY_NETWORK)
        finally:
            conn.close()
    except Exception:
        logging.error(f"{op}: rarity bookkeeping failed (non-fatal): {traceback.format_exc()}")


def _invalidate_archived_art(op: str, edition: int | None) -> None:
    """Drop `edition`'s locally archived still after an op changed its art.

    Harvest (to the shared blank silhouette) and assemble (to freshly
    composed art uploaded straight to the CDN) never write a still into the
    images_<network>/ archive the way mint/swap do, but the edition's
    on-chain `image` URL still maps back to it — so /api/img would keep
    serving the pre-op artwork. Best-effort: the ledger op already
    committed, and a stale thumbnail must never fail a session.

    Skipped when the economy runs on a different chain than the archive the
    proxy serves (the documented mainnet-characters / testnet-economy
    split): the edition numbers collide across networks, so invalidating
    would delete an unrelated edition's art."""
    if edition is None or config.ECONOMY_NETWORK != config.XRPL_NETWORK:
        return
    try:
        from lfg_core import image_archive  # local import: keeps the flows import-light

        image_archive.drop_archived(config.XRPL_NETWORK, edition)
    except Exception:
        logging.error(
            f"{op}: archived-art invalidation failed (non-fatal): {traceback.format_exc()}"
        )


def _owner_contents(conn: Any, owner: str) -> dict[tuple[str, str], int]:
    """The owner's current loose-asset counts, read from the DB mirror.
    Bodies are Task 2+ loose assets too — they live in `closet_assets` as
    ("Body", value) rows now, not the separate `closet_bodies` table."""
    return {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == owner}


def _assets_to_list(assets: dict[tuple[str, str], int]) -> list[bt.Asset]:
    return [(slot, value, count) for (slot, value), count in assets.items() if count > 0]


async def _sync_then_persist(
    deps: EconomyDeps, owner: str, assets: dict[tuple[str, str], int]
) -> str:
    """Write the new Closet contents to the on-chain token FIRST (authoritative),
    then mirror to the local DB. Returns the Closet modify tx hash.

    Bodies are loose assets now (("Body", value) rows) — the separate `bodies`
    param is gone; `[]` is passed through to `sync_closet`/`set_closet_contents`
    for the now-unused body-editions list.

    Phase-aware raises (#107) — callers pick the compensation by type:
    - bt.ClosetError (plain): the modify did NOT commit; on-chain compensation
      (burn-back / modify-back) is safe.
    - bt.ClosetIndeterminateError: the modify outcome is unknown; fail-closed —
      no on-chain compensation, reconcile from chain.
    - bt.ClosetMirrorError(tx_hash): the modify COMMITTED, only a local DB
      write failed. The shared connection is rolled back before the raise, so
      the mirror is left stale-but-consistent (never half-applied) until the
      listener rebuilds it from the token's on-chain metadata. Do NOT undo
      anything on-chain."""
    asset_list = _assets_to_list(assets)
    tx_hash = await bt.sync_closet(
        deps.conn,
        owner,
        asset_list,
        [],
        upload_fn=deps.closet_upload_fn,
        modify_fn=deps.closet_modify_fn,
    )
    try:
        es.set_closet_contents(deps.conn, owner, asset_list, [])
    except Exception as e:
        deps.conn.rollback()
        raise bt.ClosetMirrorError(f"closet contents mirror failed: {e}", tx_hash) from e
    return tx_hash


def _effective_genesis(conn: Any) -> te.Genesis:
    return te.effective_genesis(es.read_genesis(conn), es.read_supply_changes(conn))


async def _require_active_closet(deps: EconomyDeps, owner: str) -> str | None:
    """Error string if the owner has no usable ACTIVE Closet, else None. Runs an
    on-demand accept confirmation first (pending->active), then — before any
    irreversible economy op — verifies the recorded Closet is still owned by the
    user on-ledger. Fail-closed: any non-match / indeterminate lookup refuses the
    op rather than risk burning a character against a Closet that is gone (#101)."""
    if deps.closet_owner_fn is not None:
        await bt.confirm_accept(deps.conn, owner, owner_fn=deps.closet_owner_fn)
    rec = es.get_closet_record(deps.conn, owner)
    if rec is None or rec[2] != bt.ACTIVE:
        return "Create and claim your Closet first."
    if deps.closet_owner_fn is not None and (await deps.closet_owner_fn(rec[0])) != owner:
        return "Your Closet could not be verified on-ledger. Re-create it before continuing."
    return None


def _mirror_pending_error(deps: EconomyDeps, owner: str) -> str | None:
    """Error string if the owner's Closet DB mirror is flagged stale (#184), else
    None. A prior op committed an on-chain Closet change but its local mirror
    write failed (`complete_pending_mirror`); until the listener/backfill rebuilds
    the mirror from the token and clears the flag, `_owner_contents` here would
    read the STALE mirror and `_sync_then_persist` would full-overwrite the token,
    erasing the unmirrored change. Fail-closed: refuse and let the user retry once
    the mirror catches up.

    NB: this guards against a SEQUENTIAL stale read only. Serializing CONCURRENT
    ops on one owner (a per-owner lock across read→sync→mirror) is a separate
    concern owned by #180 — deliberately not added here."""
    if es.get_mirror_pending(deps.conn, owner):
        return (
            "Your Closet is still finishing a sync from a previous action. Try again in a moment."
        )
    return None


# --- Harvest: strip a live character to a BLANK; its 9 slot values (8
# non-body incl. "None", plus Body) drop into the Closet as loose assets ---


def _legacy_deltas(rec: OnchainNft, sign: int) -> dict[str, int]:
    """{"slot|value": sign} over all 9 TRAIT_ORDER slots (Body + the 8
    non-body slots) for the legacy burn+remint pair's supply_changes row —
    the exact key format `trait_economy.effective_genesis` parses."""
    deltas = {f"Body|{te.slot_value(rec, 'Body')}": sign}
    deltas.update({f"{s}|{te.slot_value(rec, s)}": sign for s in te.NON_BODY_SLOTS})
    return deltas


@dataclass
class HarvestSession:
    owner: str
    character: OnchainNft
    burnable: bool
    state: str = RUNNING
    error: str | None = None
    burn_hash: str | None = None
    modify_hash: str | None = None
    legacy_upgrade: bool = False
    new_nft_id: str | None = None
    accept: dict[str, Any] | None = None
    moved_assets: list[tuple[str, str]] = field(default_factory=list)
    sync_tx_hash: str | None = None
    mirror_pending: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def edition(self) -> int:
        return self.character.nft_number or 0

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "harvest",
            "id": self.id,
            "owner": self.owner,
            "edition": self.edition,
            "nft_id": self.character.nft_id,
            "moved_assets": self.moved_assets,
            "burn_hash": self.burn_hash,
            "modify_hash": self.modify_hash,
            "legacy_upgrade": self.legacy_upgrade,
            "new_nft_id": self.new_nft_id,
            "sync_tx_hash": self.sync_tx_hash,
            "mirror_pending": self.mirror_pending,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


@_serialize_by_owner
async def run_harvest(session: HarvestSession, deps: EconomyDeps) -> None:
    """Strip a character to a BLANK. Mutable path: NFTokenModify to blank
    metadata (reversible: modify back), then credit all assets + the body to
    the Closet. Legacy (non-mutable, burnable) path: one-time upgrade — burn,
    remint the same edition as a mutable blank, offer it back (one accept),
    then credit the Closet. Supply-neutral; the legacy pair is journaled as
    -1/+1 for audit clarity."""
    conn, rec, owner = deps.conn, session.character, session.owner
    legacy_offer_failed = False
    try:
        stale = _mirror_pending_error(deps, owner)
        if stale:
            session.fail(stale)
            return
        chk = te.can_harvest(rec, mutable=bool(rec.mutable), burnable=session.burnable)
        if not chk.ok:
            session.fail(f"cannot harvest: {chk.reason}")
            return
        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return

        body_value = te.slot_value(rec, "Body")
        session.moved_assets = [(s, te.slot_value(rec, s)) for s in te.NON_BODY_SLOTS]
        session.moved_assets.append(("Body", body_value))
        _write_record(deps.records_dir, "harvest", session.id, session._record("harvesting"))

        blank_meta_url = await deps.blank_meta_fn(session.edition) if deps.blank_meta_fn else None
        if not blank_meta_url:
            session.fail("failed to prepare blank metadata; nothing was changed")
            return

        if rec.mutable:
            modify_hash = await deps.char_modify_fn(rec.nft_id, owner, blank_meta_url)
            if not modify_hash:
                session.fail(f"failed to blank character {rec.nft_id}; nothing was changed")
                _write_record(
                    deps.records_dir, "harvest", session.id, session._record("failed_modify")
                )
                return
            session.modify_hash = modify_hash
        else:
            session.legacy_upgrade = True
            burn_hash = await deps.char_burn_fn(rec.nft_id, owner)
            if not burn_hash:
                session.fail(f"failed to burn character {rec.nft_id}; nothing was lost")
                _write_record(
                    deps.records_dir, "harvest", session.id, session._record("failed_burn")
                )
                return
            session.burn_hash = burn_hash
            es.record_supply_change(
                conn,
                kind="burn",
                edition=session.edition,
                body_value=body_value,
                body_class=rec.body,
                trait_deltas=_legacy_deltas(rec, sign=-1),
                actor="harvest",
                reason=f"legacy harvest upgrade {session.id}",
            )
            _write_record(deps.records_dir, "harvest", session.id, session._record("burned"))

            new_id = await deps.char_mint_fn(blank_meta_url)
            if not new_id:
                session.fail(
                    f"character burned but the blank remint failed — admin must remint "
                    f"edition {session.edition} (journal {session.id})"
                )
                _write_record(
                    deps.records_dir, "harvest", session.id, session._record("burned_no_remint")
                )
                return
            session.new_nft_id = new_id
            es.record_supply_change(
                conn,
                kind="mint",
                edition=session.edition,
                body_value=body_value,
                body_class=rec.body,
                trait_deltas=_legacy_deltas(rec, sign=+1),
                actor="harvest",
                reason=f"legacy harvest upgrade {session.id}",
            )
            offer_id = await deps.char_offer_fn(new_id, owner)
            if offer_id:
                session.accept = await deps.char_accept_fn(offer_id)
                if not session.accept:
                    # #262: ONLY the XUMM delivery payload failed — the sell
                    # offer is on-chain, so the blank is still claimable from
                    # Xaman's Events tab. Warn, don't fail (same posture as
                    # run_extract); failing here would strand a delivery that
                    # actually succeeded.
                    logging.warning(
                        "Harvest %s: accept payload creation failed for offer %s; "
                        "the offer is on-chain and claimable via Xaman Events",
                        session.id,
                        offer_id,
                    )
                _write_record(deps.records_dir, "harvest", session.id, session._record("reminted"))
            else:
                # Remint succeeded but the delivery offer could not be created:
                # the blank is stranded in the issuer wallet with nothing
                # claimable. The harvested parts still belong to the owner, so
                # we credit the Closet, but journal a DISTINCT checkpoint (with
                # new_nft_id) BEFORE the credit — `reminted_no_offer` — and end
                # the session FAILED as `reminted_no_offer_failed` so an admin
                # can locate and re-offer the blank.
                session.accept = None
                legacy_offer_failed = True
                logging.warning(
                    "Harvest %s: blank remint %s succeeded but offer creation "
                    "failed — the reminted blank for edition %s is stranded in the "
                    "issuer wallet; admin must re-offer it",
                    session.id,
                    new_id,
                    session.edition,
                )
                _write_record(
                    deps.records_dir,
                    "harvest",
                    session.id,
                    session._record("reminted_no_offer"),
                )

        # The character is blank on-chain from this point regardless of how the
        # Closet deposit below goes — take its traits out of the rarity
        # live-count now so shop prices reflect the harvest (#305).
        _apply_rarity_change(
            deps,
            "harvest",
            lambda c: db_helpers.record_harvest_burn(c, session.edition, rec.nft_id, owner),
        )
        # The edition now wears the shared blank silhouette; its archived
        # dressed still would otherwise keep being served as its thumbnail.
        _invalidate_archived_art("harvest", session.edition)

        # Deposit: closet token first (authoritative), then DB mirror.
        assets = _owner_contents(conn, owner)
        for slot, value in session.moved_assets:
            assets[(slot, value)] = assets.get((slot, value), 0) + 1
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, owner, assets)
        except bt.ClosetMirrorError as e:
            # The Closet token IS updated on-chain; only the DB mirror lags.
            # No compensation — the listener rebuilds the mirror from the token.
            session.sync_tx_hash = e.tx_hash
            session.mirror_pending = True
            es.set_mirror_pending(conn, owner, True)
            if legacy_offer_failed:
                # Parts landed (mirror will self-heal), but the upgraded blank was
                # never delivered — this is NOT a success. Fail honestly.
                session.fail(
                    f"your parts were credited to your Closet, but delivering your "
                    f"upgraded blank #{session.edition} failed — an admin will re-offer "
                    f"it (journal {session.id}); your Closet is still syncing"
                )
                _write_record(
                    deps.records_dir,
                    "harvest",
                    session.id,
                    session._record("reminted_no_offer_failed"),
                )
                return
            session.state = DONE
            _write_record(
                deps.records_dir, "harvest", session.id, session._record("complete_pending_mirror")
            )
            return
        except bt.ClosetIndeterminateError as e:
            # Unknown whether the deposit committed: fail-closed — no re-apply.
            session.fail(
                f"character blanked but the Closet deposit outcome is unknown ({e}); "
                f"reconcile from chain (journal {session.id})"
            )
            _write_record(
                deps.records_dir,
                "harvest",
                session.id,
                session._record("harvest_sync_indeterminate"),
            )
            return
        except Exception as e:
            # Ledger-failed: the deposit definitively did not commit. On the
            # mutable path (not the legacy burn+remint upgrade, which has no
            # character left to revert), modify the character back to its
            # original URI — otherwise the assets ride in the journal.
            if rec.mutable and not session.legacy_upgrade:
                old_uri = _raw_uri(rec.uri_hex)
                revert = await deps.char_modify_fn(rec.nft_id, owner, old_uri) if old_uri else None
                if revert:
                    session.fail(
                        f"harvest failed depositing to the Closet ({e}); "
                        f"your character was restored"
                    )
                    _write_record(
                        deps.records_dir, "harvest", session.id, session._record("reverted_modify")
                    )
                    return
            session.fail(
                f"harvest failed depositing to the Closet ({e}); assets are in "
                f"the journal ({session.id}) for recovery"
            )
            _write_record(
                deps.records_dir, "harvest", session.id, session._record("harvested_pending_closet")
            )
            return

        if legacy_offer_failed:
            # Parts are safely in the Closet, but the reminted blank could not be
            # delivered — do NOT show a success screen. Fail with an admin-recovery
            # message; the terminal journal carries new_nft_id + sync_tx_hash.
            session.fail(
                f"your parts were credited to your Closet, but delivering your "
                f"upgraded blank #{session.edition} failed — an admin will re-offer "
                f"it (journal {session.id})"
            )
            _write_record(
                deps.records_dir,
                "harvest",
                session.id,
                session._record("reminted_no_offer_failed"),
            )
            return

        session.state = DONE
        _write_record(deps.records_dir, "harvest", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Harvest {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
        # A raise AFTER the Closet modify committed (e.g. delivery offer/accept
        # blowing up post-mirror-fail) must not leave the journal at an earlier
        # checkpoint without the sticky mirror fields — recovery would treat
        # the committed change as never-happened. Persist a terminal record
        # (best-effort: journaling must never mask the original error).
        try:
            _write_record(deps.records_dir, "harvest", session.id, session._record("failed"))
        except Exception:
            logging.error(
                f"Harvest {session.id} terminal record write failed: {traceback.format_exc()}"
            )


def _character_attributes(body_value: str, chosen: dict[str, str]) -> list[dict[str, str]]:
    """A full normalized attribute list: the body plus one chosen value per
    non-body slot (canonical order)."""
    attrs = [{"trait_type": "Body", "value": body_value}]
    attrs += [{"trait_type": slot, "value": chosen[slot]} for slot in te.NON_BODY_SLOTS]
    return attrs


# --- Assemble: take a body + a full set from the Closet and mint the edition ---


@dataclass
class AssembleSession:
    owner: str
    character: OnchainNft  # the caller-owned BLANK being dressed in place
    body_value: str
    body_class: str
    chosen: dict[str, str]  # slot -> value for each non-body slot
    state: str = RUNNING
    error: str | None = None
    modify_hash: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    sync_tx_hash: str | None = None
    mirror_pending: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def edition(self) -> int:
        return self.character.nft_number or 0

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "assemble",
            "id": self.id,
            "owner": self.owner,
            "edition": self.edition,
            "nft_id": self.character.nft_id,
            "chosen": self.chosen,
            "modify_hash": self.modify_hash,
            "sync_tx_hash": self.sync_tx_hash,
            "mirror_pending": self.mirror_pending,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


@_serialize_by_owner
async def run_assemble(session: AssembleSession, deps: EconomyDeps) -> None:
    """Drive an assemble (dress-a-blank) to a terminal state. Order: precheck ->
    compose+upload -> MODIFY the caller-owned blank in place (reversible: modify
    back to the old blank URI) -> debit the Closet (body + the chosen set; token
    then DB). No mint, no offer, no accept — the character keeps its existing
    nft_id throughout."""
    conn, rec, owner = deps.conn, session.character, session.owner
    try:
        stale = _mirror_pending_error(deps, owner)
        if stale:
            session.fail(stale)
            return
        assets = _owner_contents(conn, owner)
        chk = te.can_assemble(
            rec, session.body_value, session.chosen, assets, mutable=bool(rec.mutable)
        )
        if not chk.ok:
            session.fail(f"cannot assemble: {chk.reason}")
            return

        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return

        attrs = _character_attributes(session.body_value, session.chosen)
        image_url, video_url, meta_url = await deps.char_compose_fn(
            attrs, session.body_class, session.edition, 0
        )
        _write_record(deps.records_dir, "assemble", session.id, session._record("assembling"))

        # Reversible: NFTokenModify keeps the nft_id; we can modify back.
        modify_hash = await deps.char_modify_fn(rec.nft_id, owner, meta_url)
        if not modify_hash:
            session.fail(f"failed to dress character {rec.nft_id}; your Closet is untouched")
            _write_record(
                deps.records_dir, "assemble", session.id, session._record("failed_modify")
            )
            return
        session.modify_hash = modify_hash
        # The character's art changed the moment this modify committed —
        # invalidate the archived still HERE, not after the Closet/index
        # writes below, so the mirror-failed and index-failed branches
        # (which return early yet leave the new art on-chain) can't skip
        # it. Dropping a cache entry is never wrong, so doing it before a
        # possible revert is safe: the revert just re-proxies from the CDN.
        _invalidate_archived_art("assemble", session.edition)
        _write_record(deps.records_dir, "assemble", session.id, session._record("modified"))

        # Debit the Closet: the body plus each chosen slot value (incl.
        # "None" values — they debit too). Token first, then DB mirror.
        assets[("Body", session.body_value)] = assets.get(("Body", session.body_value), 0) - 1
        for slot in te.NON_BODY_SLOTS:
            key = (slot, session.chosen[slot])
            assets[key] = assets.get(key, 0) - 1
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, owner, assets)
        except bt.ClosetMirrorError as e:
            # The debit COMMITTED on-chain; only the DB mirror failed. The
            # character already carries its new traits — do NOT revert it;
            # the listener rebuilds the mirror from the Closet token.
            session.sync_tx_hash = e.tx_hash
            session.mirror_pending = True
            es.set_mirror_pending(conn, owner, True)
            session.state = DONE
            session.results.append(
                {
                    "nft_id": rec.nft_id,
                    "image_url": image_url,
                    "video_url": video_url,
                    "metadata_url": meta_url,
                    "accept": None,
                }
            )
            _write_record(
                deps.records_dir, "assemble", session.id, session._record("complete_pending_mirror")
            )
            return
        except bt.ClosetIndeterminateError as e:
            # Debit outcome unknown: fail-closed — no revert against an
            # unknown Closet; an admin reconciles from chain.
            session.fail(
                f"assemble closet debit outcome unknown ({e}); the character keeps its new "
                f"traits — reconcile from chain (journal {session.id})"
            )
            _write_record(
                deps.records_dir,
                "assemble",
                session.id,
                session._record("assemble_sync_indeterminate"),
            )
            return
        except Exception as e:
            # Ledger-failed: the debit definitively did not commit. Modify the
            # character back to its pre-assemble (blank) URI; the Closet is
            # untouched.
            old_uri = _raw_uri(rec.uri_hex)
            revert_hash = await deps.char_modify_fn(rec.nft_id, owner, old_uri) if old_uri else None
            if revert_hash:
                session.fail(
                    f"assemble failed updating the closet ({e}); your character was reverted"
                )
                _write_record(
                    deps.records_dir, "assemble", session.id, session._record("reverted_modify")
                )
            else:
                session.fail(
                    f"assemble failed updating the closet ({e}); the character could NOT be "
                    f"reverted to blank — it may retain the new traits (journal {session.id})"
                )
                _write_record(
                    deps.records_dir, "assemble", session.id, session._record("failed_revert")
                )
            return

        # The character is dressed and the Closet drained — the edition is
        # back in the live population, so return it to the rarity live-count
        # (#305; balances the record_harvest_burn in run_harvest).
        _apply_rarity_change(
            deps,
            "assemble",
            lambda c: db_helpers.revive_harvested_edition(c, session.edition),
        )
        # Freshly composed art went straight to the CDN — drop the archived
        # (blank or pre-assemble) still so the new artwork is what shows.
        _invalidate_archived_art("assemble", session.edition)
        session.state = DONE
        session.results.append(
            {
                "nft_id": rec.nft_id,
                "image_url": image_url,
                # Animated assembles (#250): the .mp4 next to the PNG still.
                "video_url": video_url,
                "metadata_url": meta_url,
                "accept": None,
            }
        )
        _write_record(deps.records_dir, "assemble", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Assemble {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
        # A raise after the Closet modify committed must not leave the journal
        # at an earlier checkpoint without the sticky mirror fields — recovery
        # would treat the committed change as never-happened. Persist a
        # terminal record (best-effort: journaling must never mask the
        # original error).
        try:
            _write_record(deps.records_dir, "assemble", session.id, session._record("failed"))
        except Exception:
            logging.error(
                f"Assemble {session.id} terminal record write failed: {traceback.format_exc()}"
            )


def _raw_uri(uri_hex: str) -> str:
    """Decode an on-chain hex URI to its exact plain string (NOT ipfs-resolved,
    so re-hexing reproduces the same on-chain URI for a revert)."""
    try:
        return bytes.fromhex(uri_hex).decode("ascii")
    except ValueError:
        return ""


def _persist_equip_to_index(
    deps: EconomyDeps,
    session: EquipSession,
    new_attrs: list[dict[str, str]],
    image_url: str,
    meta_url: str,
) -> None:
    """Write the post-modify ledger truth straight into the on-chain index
    (mirrors swap_flow._persist_remint_to_index / the #211 pattern), so a
    client that refetches the roster immediately after Save sees the new
    traits instead of racing the listener. Unlike a swap remint, an equip is
    an in-place NFTokenModify: the nft_id, owner, and mutability are
    UNCHANGED — this is a single upsert of the same record with its new
    attributes/uri_hex/image, never a mark_burned + new row.

    deps.conn is already an open connection to the same per-network index DB
    that holds onchain_nfts (opened via _economy_deps.open_index), so this
    reuses it directly rather than opening a second connection.

    Best-effort only: failures are LOUD (a CRITICAL log line, mirroring
    swap_flow) but must never fail the session — the chain is truth and the
    listener/backfill self-heals the index. Callers must only invoke this on
    a confirmed-committed success path (DONE or complete_pending_mirror),
    never on a reverted/indeterminate branch."""
    rec = session.character
    try:
        nft_index.upsert(
            deps.conn,
            OnchainNft(
                nft_id=rec.nft_id,
                nft_number=rec.nft_number,
                owner=rec.owner,
                is_burned=False,
                mutable=rec.mutable,
                # .hex() is lowercase — the index's canonical case (matches
                # swap_flow._persist_remint_to_index).
                uri_hex=meta_url.encode("utf-8").hex(),
                body=rec.body,
                attributes=new_attrs,
                image=image_url,
                ledger_index=None,
            ),
        )
    except Exception:
        logging.critical(
            f"post-equip index persist FAILED for {rec.nft_id} (modify {session.modify_hash} "
            f"committed on-chain): the roster will serve stale traits until the "
            f"listener/backfill catches up. {traceback.format_exc()}"
        )
        # Unlike swap_flow._persist_remint_to_index (which opens and owns a
        # private connection that a failure here simply leaves for the caller
        # to close/discard), deps.conn is SHARED with the rest of the economy
        # flow — nft_index.upsert commits internally, but if it blows up
        # partway through its own commit() rather than its execute(), a
        # half-applied transaction can be left open on deps.conn, which the
        # caller goes on to reuse. Roll it back explicitly so a failed index
        # stamp can never leak an implicit transaction onto shared state. This
        # itself must never raise — a rollback on an already-closed or
        # otherwise broken connection must not turn a logged warning into a
        # thrown exception.
        try:
            deps.conn.rollback()
        except Exception:
            logging.critical(
                f"post-equip index persist rollback ALSO failed for {rec.nft_id}: "
                f"{traceback.format_exc()}"
            )


# --- Equip: move a loose asset onto a live character; displaced -> Closet ---


@dataclass
class EquipSession:
    owner: str
    character: OnchainNft
    changes: list[tuple[str, str]]  # ordered (slot, incoming_value) pairs
    state: str = RUNNING
    error: str | None = None
    displaced: dict[str, str] = field(default_factory=dict)  # slot -> value pushed out
    modify_hash: str | None = None
    sync_tx_hash: str | None = None
    mirror_pending: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "equip",
            "id": self.id,
            "owner": self.owner,
            "nft_id": self.character.nft_id,
            "changes": [
                {"slot": slot, "incoming": value, "displaced": self.displaced.get(slot, "")}
                for slot, value in self.changes
            ],
            "modify_hash": self.modify_hash,
            "sync_tx_hash": self.sync_tx_hash,
            "mirror_pending": self.mirror_pending,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


@_serialize_by_owner
async def run_equip(session: EquipSession, deps: EconomyDeps) -> None:
    """Drive a batch equip to a terminal state. Order: precheck every change ->
    compose+upload ONCE -> ONE MODIFY of the character in place (reversible:
    modify back to the old URI) -> ONE Closet swap carrying every
    (-incoming, +displaced) delta. If the closet swap fails after the modify,
    the character is reverted (a whole-URI revert restores all slots at once)
    and the Closet untouched."""
    conn, owner, rec = deps.conn, session.owner, session.character
    try:
        stale = _mirror_pending_error(deps, owner)
        if stale:
            session.fail(stale)
            return
        if not session.changes:
            session.fail("cannot equip: no changes to apply")
            return
        assets = _owner_contents(conn, owner)
        # Precheck every change, accumulating each (-incoming, +displaced) delta
        # into ONE asset dict for the single Closet sync below. Assets are keyed
        # (slot, value) and a slot may appear at most once, so the changes are
        # independent; any failing precheck aborts the batch before the ledger
        # is touched.
        seen: set[str] = set()
        for slot, incoming in session.changes:
            if slot in seen:
                session.fail(f"cannot equip: duplicate slot in one batch ({slot})")
                return
            seen.add(slot)
            chk = te.can_equip(rec, slot, incoming, assets, mutable=bool(rec.mutable))
            if not chk.ok:
                session.fail(f"cannot equip: {chk.reason}")
                return
            displaced = te.slot_value(rec, slot)
            session.displaced[slot] = displaced
            assets[(slot, incoming)] = assets.get((slot, incoming), 0) - 1
            assets[(slot, displaced)] = assets.get((slot, displaced), 0) + 1

        incoming_by_slot = dict(session.changes)
        new_attrs = [
            {
                "trait_type": a["trait_type"],
                "value": incoming_by_slot.get(a["trait_type"], a["value"]),
            }
            for a in rec.attributes
        ]
        image_url, _video_url, meta_url = await deps.char_compose_fn(
            new_attrs, rec.body, rec.nft_number or 0, 0
        )
        _write_record(deps.records_dir, "equip", session.id, session._record("equipping"))

        # Reversible: NFTokenModify keeps the nft_id; we can modify back.
        modify_hash = await deps.char_modify_fn(rec.nft_id, owner, meta_url)
        if not modify_hash:
            session.fail(f"failed to update character {rec.nft_id}; your character is unchanged")
            _write_record(deps.records_dir, "equip", session.id, session._record("failed_modify"))
            return
        session.modify_hash = modify_hash
        # The character's art changed the moment this modify committed —
        # invalidate the archived still HERE, not after the Closet/index
        # writes below, so the mirror-failed and index-failed branches
        # (which return early yet leave the new art on-chain) can't skip
        # it. Dropping a cache entry is never wrong, so doing it before a
        # possible revert is safe: the revert just re-proxies from the CDN.
        _invalidate_archived_art("equip", rec.nft_number)

        # Swap the closet with every delta at once. Token first, then DB.
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, owner, assets)
        except bt.ClosetMirrorError as e:
            # The Closet swap COMMITTED on-chain; only the DB mirror failed.
            # Reverting the character would strand the swapped Closet — keep the
            # new traits; the listener converges the mirror.
            session.sync_tx_hash = e.tx_hash
            session.mirror_pending = True
            es.set_mirror_pending(conn, owner, True)
            session.state = DONE
            _persist_equip_to_index(deps, session, new_attrs, image_url, meta_url)
            _write_record(
                deps.records_dir, "equip", session.id, session._record("complete_pending_mirror")
            )
            return
        except bt.ClosetIndeterminateError as e:
            # Swap outcome unknown: fail-closed — no revert against an unknown
            # Closet; an admin reconciles from chain.
            session.fail(
                f"equip closet swap outcome unknown ({e}); the character keeps its new traits — "
                f"reconcile from chain (journal {session.id})"
            )
            _write_record(
                deps.records_dir, "equip", session.id, session._record("equip_sync_indeterminate")
            )
            return
        except Exception as e:
            # Ledger-failed: the swap definitively did not commit. Roll the
            # character back to its old traits; the closet is untouched.
            old_uri = _raw_uri(rec.uri_hex)
            # Check the revert modify actually LANDED: a falsy hash (or no
            # decodable old URI to revert to) means the character may still carry
            # the new traits while the Closet was not updated — that is the
            # failed_revert case (admin recovery), not a clean reverted_modify.
            revert_hash = await deps.char_modify_fn(rec.nft_id, owner, old_uri) if old_uri else None
            if revert_hash:
                session.fail(f"equip failed updating the closet ({e}); your character was reverted")
                _write_record(
                    deps.records_dir, "equip", session.id, session._record("reverted_modify")
                )
            else:
                session.fail(
                    f"equip failed updating the closet ({e}); the character could NOT be reverted "
                    f"to its old traits — it may retain the new traits (journal {session.id})"
                )
                _write_record(
                    deps.records_dir, "equip", session.id, session._record("failed_revert")
                )
            return

        session.state = DONE
        _persist_equip_to_index(deps, session, new_attrs, image_url, meta_url)
        _write_record(deps.records_dir, "equip", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Equip {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
        # A raise AFTER the Closet modify committed (e.g. delivery offer/accept
        # blowing up post-mirror-fail) must not leave the journal at an earlier
        # checkpoint without the sticky mirror fields — recovery would treat
        # the committed change as never-happened. Persist a terminal record
        # (best-effort: journaling must never mask the original error).
        try:
            _write_record(deps.records_dir, "equip", session.id, session._record("failed"))
        except Exception:
            logging.error(
                f"Equip {session.id} terminal record write failed: {traceback.format_exc()}"
            )


# --- Extract: turn a loose Closet trait into a standalone tradeable NFToken ---


@dataclass
class ExtractSession:
    owner: str
    slot: str
    value: str
    state: str = RUNNING
    error: str | None = None
    nft_id: str | None = None
    accept: dict[str, Any] | None = None
    sync_tx_hash: str | None = None
    mirror_pending: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "extract",
            "id": self.id,
            "owner": self.owner,
            "slot": self.slot,
            "value": self.value,
            "nft_id": self.nft_id,
            "sync_tx_hash": self.sync_tx_hash,
            "mirror_pending": self.mirror_pending,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


@_serialize_by_owner
async def run_extract(session: ExtractSession, deps: EconomyDeps) -> None:
    """Extract a loose Closet trait into a standalone tradeable NFToken. Order:
    precheck (active Closet + trait present) -> compose+mint (reversible) ->
    decrement Closet + record trait_token -> burn-back on Closet failure ->
    offer+accept. Supply-neutral (no supply_changes)."""
    conn, owner, slot, value = deps.conn, session.owner, session.slot, session.value
    try:
        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return
        stale = _mirror_pending_error(deps, owner)
        if stale:
            session.fail(stale)
            return
        assets = _owner_contents(conn, owner)
        if assets.get((slot, value), 0) < 1:
            session.fail(f"no loose '{value}' {slot} in your Closet to extract")
            return

        image_url = await deps.trait_compose_fn(slot, value)  # type: ignore[misc]
        meta_url = await deps.trait_upload_fn(tt.build_trait_metadata(slot, value, image_url))  # type: ignore[misc]
        _write_record(deps.records_dir, "extract", session.id, session._record("minting"))

        nft_id = await deps.trait_mint_fn(meta_url)  # type: ignore[misc]
        if not nft_id:
            session.fail(f"failed to mint trait token for {value} {slot}; your Closet is untouched")
            _write_record(deps.records_dir, "extract", session.id, session._record("failed_mint"))
            return
        session.nft_id = nft_id
        _write_record(deps.records_dir, "extract", session.id, session._record("minted"))

        assets[(slot, value)] = assets.get((slot, value), 0) - 1
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, owner, assets)
        except bt.ClosetMirrorError as e:
            # The decrement COMMITTED on-chain; only the DB mirror failed.
            # Burning the token back would destroy it against a Closet that
            # already gave up the trait — no compensation; deliver instead.
            session.sync_tx_hash = e.tx_hash
            session.mirror_pending = True
            es.set_mirror_pending(conn, owner, True)
        except bt.ClosetIndeterminateError as e:
            # Decrement outcome unknown: fail-closed. Keep the token (id
            # journaled), no burn — an admin reconciles from chain.
            session.fail(
                f"extract Closet decrement outcome unknown ({e}); trait token {nft_id} is kept — "
                f"reconcile from chain (journal {session.id})"
            )
            _write_record(
                deps.records_dir,
                "extract",
                session.id,
                session._record("extract_sync_indeterminate"),
            )
            return
        except Exception as e:
            # Ledger-failed: the decrement definitively did not commit.
            revert = await deps.trait_burn_fn(nft_id, "")  # type: ignore[misc]
            if revert:
                session.nft_id = None
                session.fail(f"extract failed updating the Closet ({e}); your Closet is untouched")
                _write_record(
                    deps.records_dir, "extract", session.id, session._record("reverted_mint")
                )
            else:
                session.fail(
                    f"extract failed updating the Closet ({e}) and the compensating burn of "
                    f"{nft_id} failed — admin must burn it (journal {session.id})"
                )
                _write_record(
                    deps.records_dir, "extract", session.id, session._record("failed_revert_mint")
                )
            return

        # Durable checkpoint BEFORE delivery (assemble twin): the decrement is
        # committed with its sync_tx_hash — a crash in the offer/accept awaits
        # below must not leave the journal at the pre-decrement "minted"
        # record, or recovery burns the token back against a drained Closet.
        _write_record(deps.records_dir, "extract", session.id, session._record("closet_synced"))

        # Closet decremented on-chain + DB. Mirror the trait token in the DB
        # best-effort: the listener rebuilds trait_tokens from the on-chain mint, so a
        # failure here is non-fatal — journal it for the auditor rather than reverting
        # a successful on-chain extract.
        #
        # The freshly-minted token is held by the ISSUER until the owner accepts the
        # offer in Xaman, so the current on-ledger owner is the issuer (not `owner`).
        # Recording the issuer here keeps it out of the owner's deposit candidates
        # (read_economy_state filters by wallet); the listener flips the owner to the
        # wallet when it observes the AcceptOffer, at which point it becomes depositable.
        try:
            es.upsert_trait_token(conn, nft_id, config.SWAP_ISSUER_ADDRESS, slot, value)
        except Exception:
            logging.error(
                f"extract {session.id} trait_tokens mirror failed: {traceback.format_exc()}"
            )
            # A single pending-mirror flag covers both the Closet and the
            # trait_tokens mirror — the terminal record reports it.
            session.mirror_pending = True

        offer_id = await deps.closet_offer_fn(nft_id, owner)
        if not offer_id:
            # The Closet decrement already COMMITTED on-chain but the delivery
            # offer failed — the trait token is stranded in the issuer wallet.
            # Mirror assemble's minted_no_offer: this is NOT "complete" (the
            # user hasn't received the token), it's a RECOVERABLE state with a
            # re-offer path. Do NOT burn back — the Closet already gave up the
            # trait, so a burn would destroy it. Sticky mirror_pending/
            # sync_tx_hash are preserved in the record for reconciliation.
            session.fail(
                f"trait token {nft_id} was minted and your Closet drained, but the delivery "
                f"offer failed — contact an admin to re-offer it (journal {session.id})"
            )
            _write_record(
                deps.records_dir, "extract", session.id, session._record("minted_no_offer")
            )
            return
        session.accept = await deps.closet_accept_fn(offer_id)
        if not session.accept:
            # #262: only the XUMM delivery payload failed — the offer is
            # on-chain and claimable via Xaman Events, so warn, don't fail.
            logging.warning(
                f"Extract {session.id}: accept payload creation failed for offer "
                f"{offer_id}; offer is on-chain, claimable via Xaman Events"
            )
        session.state = DONE
        status = "complete_pending_mirror" if session.mirror_pending else "complete"
        _write_record(deps.records_dir, "extract", session.id, session._record(status))
    except Exception as e:
        logging.error(f"Extract {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
        # A raise AFTER the Closet modify committed (e.g. delivery offer/accept
        # blowing up post-mirror-fail) must not leave the journal at an earlier
        # checkpoint without the sticky mirror fields — recovery would treat
        # the committed change as never-happened. Persist a terminal record
        # (best-effort: journaling must never mask the original error).
        try:
            _write_record(deps.records_dir, "extract", session.id, session._record("failed"))
        except Exception:
            logging.error(
                f"Extract {session.id} terminal record write failed: {traceback.format_exc()}"
            )


# --- Deposit: burn a standalone trait NFToken back into the owner's Closet ---


@dataclass
class DepositSession:
    owner: str
    nft_id: str
    state: str = RUNNING
    error: str | None = None
    slot: str | None = None
    value: str | None = None
    burn_hash: str | None = None
    sync_tx_hash: str | None = None
    mirror_pending: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "deposit",
            "id": self.id,
            "owner": self.owner,
            "nft_id": self.nft_id,
            "slot": self.slot,
            "value": self.value,
            "burn_hash": self.burn_hash,
            "sync_tx_hash": self.sync_tx_hash,
            "mirror_pending": self.mirror_pending,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


@_serialize_by_owner
async def run_deposit(session: DepositSession, deps: EconomyDeps) -> None:
    """Deposit a standalone trait NFToken back into the owner's Closet. Order:
    precheck (active Closet + token is ours + on-ledger owner == depositor) ->
    issuer BURN (irreversible) -> credit Closet + delete trait_token row ->
    journal on credit failure. Supply-neutral. Fail-closed on any ownership
    uncertainty."""
    conn, owner, nft_id = deps.conn, session.owner, session.nft_id
    try:
        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return
        stale = _mirror_pending_error(deps, owner)
        if stale:
            session.fail(stale)
            return
        info = await deps.trait_info_fn(nft_id)  # type: ignore[misc]
        if not info:
            session.fail("could not verify the trait token on-ledger; nothing was changed")
            return
        if (
            int(info.get("taxon") or -1) != config.TRAIT_TAXON
            or info.get("issuer") != config.SWAP_ISSUER_ADDRESS
        ):
            session.fail("that NFToken is not an LFG trait token")
            return
        if info.get("owner") != owner:
            session.fail("you do not own that trait token on-ledger; nothing was changed")
            return
        meta = await deps.trait_meta_fn(nft_id)  # type: ignore[misc]
        parsed = tt.parse_trait_metadata(meta or {})
        if parsed is None:
            session.fail("that trait token has unreadable metadata; nothing was changed")
            return
        session.slot, session.value = parsed
        _write_record(deps.records_dir, "deposit", session.id, session._record("depositing"))

        burn_hash = await deps.trait_burn_fn(nft_id, owner)  # type: ignore[misc]
        if not burn_hash:
            session.fail(f"failed to burn trait token {nft_id}; nothing was lost")
            _write_record(deps.records_dir, "deposit", session.id, session._record("failed_burn"))
            return
        session.burn_hash = burn_hash
        es.delete_trait_token(conn, nft_id)
        _write_record(deps.records_dir, "deposit", session.id, session._record("burned"))

        assets = _owner_contents(conn, owner)
        assets[(session.slot, session.value)] = assets.get((session.slot, session.value), 0) + 1
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, owner, assets)
        except bt.ClosetMirrorError as e:
            # The credit COMMITTED on-chain; only the DB mirror lags. The
            # operator must NOT re-credit (double-credit) — the listener
            # rebuilds the mirror from the Closet token.
            session.sync_tx_hash = e.tx_hash
            session.mirror_pending = True
            es.set_mirror_pending(conn, owner, True)
            session.state = DONE
            _write_record(
                deps.records_dir, "deposit", session.id, session._record("complete_pending_mirror")
            )
            return
        except bt.ClosetIndeterminateError as e:
            # Credit outcome unknown: fail-closed — reconcile from chain
            # before any re-credit (never blind re-apply).
            session.fail(
                f"trait burned but the Closet credit outcome is unknown ({e}); "
                f"reconcile from chain before any re-credit (journal {session.id})"
            )
            _write_record(
                deps.records_dir,
                "deposit",
                session.id,
                session._record("deposit_sync_indeterminate"),
            )
            return
        except Exception as e:
            # Ledger-failed: the credit definitively did not commit — the
            # operator re-apply recipe is safe.
            session.fail(
                f"trait burned but Closet credit failed ({e}); recorded in the journal "
                f"({session.id}) for recovery"
            )
            _write_record(
                deps.records_dir,
                "deposit",
                session.id,
                session._record("deposited_pending_closet"),
            )
            return

        session.state = DONE
        _write_record(deps.records_dir, "deposit", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Deposit {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
        # A raise AFTER the Closet modify committed (e.g. delivery offer/accept
        # blowing up post-mirror-fail) must not leave the journal at an earlier
        # checkpoint without the sticky mirror fields — recovery would treat
        # the committed change as never-happened. Persist a terminal record
        # (best-effort: journaling must never mask the original error).
        try:
            _write_record(deps.records_dir, "deposit", session.id, session._record("failed"))
        except Exception:
            logging.error(
                f"Deposit {session.id} terminal record write failed: {traceback.format_exc()}"
            )
