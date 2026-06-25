# lfg_core/economy_flow.py
# Trait-economy operation flows (the swap_flow.py analogue for the dress-up
# game): harvest / assemble / equip as async state machines with on-disk
# journaling and partial-failure recovery. All on-chain effects go through
# injected callables (EconomyDeps) so the flows are unit-testable without a
# network; the CLI drivers wire the real xrpl_ops/cdn/xumm_ops/bucket_token.
#
# Ordering principle: the irreversible character step is taken once everything
# reversible is in place, and the Bucket NFToken (the on-chain source of truth)
# is always modified BEFORE the local DB mirror — so a crash leaves the DB
# rebuildable from the token by the listener, never the reverse.

from __future__ import annotations

import json
import logging
import os
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from lfg_core import bucket_token as bt
from lfg_core import config
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft

RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Character composition: (attributes, body_class, edition, rev) -> (image_url,
# video_url, metadata_url). The real impl composes layers + uploads image and
# metadata JSON; tests return canned URLs.
ComposeFn = Callable[[list[dict[str, str]], str, int, int], Awaitable[tuple[str, str | None, str]]]
BurnFn = Callable[[str, str], Awaitable[str | None]]  # (nft_id, owner) -> tx hash


@dataclass
class EconomyDeps:
    """Injected operations. The bucket_* callables are forwarded to
    bucket_token.ensure_bucket/sync_bucket; the char_* callables act on the
    character NFToken; char_compose_fn builds+uploads image+metadata."""

    conn: Any  # sqlite3.Connection
    bucket_upload_fn: bt.UploadFn
    bucket_mint_fn: bt.MintFn
    bucket_offer_fn: bt.OfferFn
    bucket_accept_fn: bt.AcceptFn
    bucket_modify_fn: bt.ModifyFn
    char_compose_fn: ComposeFn
    char_mint_fn: bt.MintFn
    char_modify_fn: bt.ModifyFn
    char_burn_fn: BurnFn
    char_offer_fn: bt.OfferFn
    char_accept_fn: bt.AcceptFn
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


def _owner_contents(conn: Any, owner: str) -> tuple[dict[tuple[str, str], int], set[int]]:
    """The owner's current loose-asset counts and loose-body editions, read from
    the DB mirror."""
    assets = {(s, v): n for o, s, v, n in es.read_bucket_assets(conn) if o == owner}
    bodies = {e for o, e in es.read_bucket_bodies(conn) if o == owner}
    return assets, bodies


def _assets_to_list(assets: dict[tuple[str, str], int]) -> list[bt.Asset]:
    return [(slot, value, count) for (slot, value), count in assets.items() if count > 0]


async def _sync_then_persist(
    deps: EconomyDeps, owner: str, assets: dict[tuple[str, str], int], bodies: set[int]
) -> None:
    """Write the new bucket contents to the on-chain token FIRST (authoritative),
    then mirror to the local DB. Raises bt.BucketError if the on-chain modify
    fails (caller decides recovery)."""
    asset_list = _assets_to_list(assets)
    body_list = sorted(bodies)
    await bt.sync_bucket(
        deps.conn,
        owner,
        asset_list,
        body_list,
        upload_fn=deps.bucket_upload_fn,
        modify_fn=deps.bucket_modify_fn,
    )
    es.set_bucket_contents(deps.conn, owner, asset_list, body_list)


def _effective_genesis(conn: Any) -> te.Genesis:
    return te.effective_genesis(es.read_genesis(conn), es.read_supply_changes(conn))


# --- Harvest: burn a live character; its 8 assets + body drop into the Bucket ---


@dataclass
class HarvestSession:
    owner: str
    character: OnchainNft
    burnable: bool
    state: str = RUNNING
    error: str | None = None
    burn_hash: str | None = None
    moved_assets: list[tuple[str, str]] = field(default_factory=list)
    bucket_accept: dict[str, Any] | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    platform: str = "discord"

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
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


async def run_harvest(session: HarvestSession, deps: EconomyDeps) -> None:
    """Drive a harvest to a terminal state. Order: precheck -> ensure bucket
    (reversible) -> BURN (irreversible) -> deposit assets to the Bucket token
    then DB. If the deposit fails after the burn, the journal carries the moved
    assets + burn hash for recovery; the assets are never silently lost."""
    conn = deps.conn
    rec, owner = session.character, session.owner
    try:
        chk = te.can_harvest(rec, _effective_genesis(conn), burnable=session.burnable)
        if not chk.ok:
            session.fail(f"cannot harvest: {chk.reason}")
            return

        # Reversible: an empty bucket simply sits in the wallet.
        ref = await bt.ensure_bucket(
            conn,
            owner,
            upload_fn=deps.bucket_upload_fn,
            mint_fn=deps.bucket_mint_fn,
            offer_fn=deps.bucket_offer_fn,
            accept_payload_fn=deps.bucket_accept_fn,
        )
        session.bucket_accept = ref.accept_payload

        # Snapshot the assets to move BEFORE the burn (the character is gone after).
        session.moved_assets = [(s, te.slot_value(rec, s)) for s in te.NON_BODY_SLOTS]
        _write_record(deps.records_dir, "harvest", session.id, session._record("harvesting"))

        # IRREVERSIBLE: burn the character; the edition dies.
        burn_hash = await deps.char_burn_fn(rec.nft_id, owner)
        if not burn_hash:
            session.fail(f"failed to burn character {rec.nft_id}; nothing was lost")
            _write_record(deps.records_dir, "harvest", session.id, session._record("failed_burn"))
            return
        session.burn_hash = burn_hash
        _write_record(deps.records_dir, "harvest", session.id, session._record("burned"))

        # Deposit: token first (authoritative), then DB mirror.
        assets, bodies = _owner_contents(conn, owner)
        for slot, value in session.moved_assets:
            assets[(slot, value)] = assets.get((slot, value), 0) + 1
        bodies.add(session.edition)
        try:
            await _sync_then_persist(deps, owner, assets, bodies)
        except Exception as e:
            session.fail(
                f"character burned but Bucket deposit failed ({e}); assets are recorded in "
                f"the journal ({session.id}) for recovery"
            )
            _write_record(
                deps.records_dir, "harvest", session.id, session._record("harvested_pending_bucket")
            )
            return

        session.state = DONE
        _write_record(deps.records_dir, "harvest", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Harvest {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))


def _character_attributes(body_value: str, chosen: dict[str, str]) -> list[dict[str, str]]:
    """A full normalized attribute list: the body plus one chosen value per
    non-body slot (canonical order)."""
    attrs = [{"trait_type": "Body", "value": body_value}]
    attrs += [{"trait_type": slot, "value": chosen[slot]} for slot in te.NON_BODY_SLOTS]
    return attrs


# --- Assemble: take a body + a full set from the Bucket and mint the edition ---


@dataclass
class AssembleSession:
    owner: str
    edition: int
    chosen: dict[str, str]  # slot -> value for each non-body slot
    body_value: str
    body_class: str
    live_editions: set[int] = field(default_factory=set)
    state: str = RUNNING
    error: str | None = None
    new_nft_id: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    platform: str = "discord"

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "assemble",
            "id": self.id,
            "owner": self.owner,
            "edition": self.edition,
            "chosen": self.chosen,
            "new_nft_id": self.new_nft_id,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


async def run_assemble(session: AssembleSession, deps: EconomyDeps) -> None:
    """Drive an assemble (rebirth) to a terminal state. Order: precheck ->
    compose+upload -> MINT (reversible: burn back) -> drain the Bucket (token
    then DB) -> offer+accept. If the drain fails the mint is burned back and the
    Bucket is untouched; if the offer fails after the drain the minted token is
    parked in the issuer wallet for re-offer (no asset loss)."""
    conn, owner, edition = deps.conn, session.owner, session.edition
    try:
        assets, bodies = _owner_contents(conn, owner)
        chk = te.can_assemble(
            edition,
            session.chosen,
            bodies,
            assets,
            session.live_editions,
            _effective_genesis(conn),
        )
        if not chk.ok:
            session.fail(f"cannot assemble: {chk.reason}")
            return

        attrs = _character_attributes(session.body_value, session.chosen)
        image_url, _video_url, meta_url = await deps.char_compose_fn(
            attrs, session.body_class, edition, 0
        )
        _write_record(deps.records_dir, "assemble", session.id, session._record("assembling"))

        # Reversible: a freshly minted character can be burned back.
        nft_id = await deps.char_mint_fn(meta_url)
        if not nft_id:
            session.fail(f"failed to mint edition {edition}; your bucket is untouched")
            _write_record(deps.records_dir, "assemble", session.id, session._record("failed_mint"))
            return
        session.new_nft_id = nft_id
        _write_record(deps.records_dir, "assemble", session.id, session._record("minted"))

        # Drain the bucket: token first (authoritative), then DB mirror.
        bodies.discard(edition)
        for slot in te.NON_BODY_SLOTS:
            key = (slot, session.chosen[slot])
            assets[key] = assets.get(key, 0) - 1
        try:
            await _sync_then_persist(deps, owner, assets, bodies)
        except Exception as e:
            # Mint succeeded but the bucket drain failed: burn the mint back so
            # the user's bucket is exactly as it was.
            revert_hash = await deps.char_burn_fn(nft_id, "")
            if revert_hash:
                session.new_nft_id = None
                session.fail(f"assemble failed draining the bucket ({e}); your bucket is untouched")
                _write_record(
                    deps.records_dir, "assemble", session.id, session._record("reverted_mint")
                )
            else:
                # The compensating burn ALSO failed: the minted token is stranded
                # in the issuer wallet. Keep its nft_id in the journal so an admin
                # can locate and burn it — do NOT wipe it.
                session.fail(
                    f"assemble failed draining the bucket ({e}) and the compensating burn of "
                    f"{nft_id} failed — admin must burn it manually (journal {session.id})"
                )
                _write_record(
                    deps.records_dir, "assemble", session.id, session._record("failed_revert_mint")
                )
            return

        # Deliver the new character to the user (offer + XUMM accept).
        offer_id = await deps.char_offer_fn(nft_id, owner)
        if not offer_id:
            session.fail(
                f"edition {edition} was minted ({nft_id}) and your bucket drained, but the offer "
                f"failed — contact an admin to re-offer it (journal {session.id})"
            )
            _write_record(
                deps.records_dir, "assemble", session.id, session._record("minted_no_offer")
            )
            return
        accept = await deps.char_accept_fn(offer_id)
        session.results.append(
            {"nft_id": nft_id, "image_url": image_url, "metadata_url": meta_url, "accept": accept}
        )
        session.state = DONE
        _write_record(deps.records_dir, "assemble", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Assemble {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))


def _raw_uri(uri_hex: str) -> str:
    """Decode an on-chain hex URI to its exact plain string (NOT ipfs-resolved,
    so re-hexing reproduces the same on-chain URI for a revert)."""
    try:
        return bytes.fromhex(uri_hex).decode("ascii")
    except ValueError:
        return ""


# --- Equip: move a loose asset onto a live character; displaced -> Bucket ---


@dataclass
class EquipSession:
    owner: str
    character: OnchainNft
    slot: str
    incoming_value: str
    state: str = RUNNING
    error: str | None = None
    displaced_value: str = ""
    modify_hash: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    platform: str = "discord"

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "equip",
            "id": self.id,
            "owner": self.owner,
            "nft_id": self.character.nft_id,
            "slot": self.slot,
            "incoming": self.incoming_value,
            "displaced": self.displaced_value,
            "modify_hash": self.modify_hash,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


async def run_equip(session: EquipSession, deps: EconomyDeps) -> None:
    """Drive an equip to a terminal state. Order: precheck -> compose+upload ->
    MODIFY the character in place (reversible: modify back to the old URI) ->
    swap the Bucket (-incoming, +displaced; token then DB). If the bucket swap
    fails after the modify, the character is reverted and the Bucket untouched."""
    conn, owner, rec = deps.conn, session.owner, session.character
    slot, incoming = session.slot, session.incoming_value
    try:
        assets, _bodies = _owner_contents(conn, owner)
        chk = te.can_equip(rec, slot, incoming, assets, mutable=bool(rec.mutable))
        if not chk.ok:
            session.fail(f"cannot equip: {chk.reason}")
            return
        session.displaced_value = te.slot_value(rec, slot)

        new_attrs = [
            {
                "trait_type": a["trait_type"],
                "value": incoming if a["trait_type"] == slot else a["value"],
            }
            for a in rec.attributes
        ]
        _image_url, _video_url, meta_url = await deps.char_compose_fn(
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

        # Swap the bucket: -incoming, +displaced. Token first, then DB.
        assets[(slot, incoming)] = assets.get((slot, incoming), 0) - 1
        assets[(slot, session.displaced_value)] = assets.get((slot, session.displaced_value), 0) + 1
        try:
            await _sync_then_persist(deps, owner, assets, _bodies)
        except Exception as e:
            # Roll the character back to its old traits; the bucket is untouched.
            old_uri = _raw_uri(rec.uri_hex)
            if old_uri:
                await deps.char_modify_fn(rec.nft_id, owner, old_uri)
                session.fail(f"equip failed updating the bucket ({e}); your character was reverted")
                _write_record(
                    deps.records_dir, "equip", session.id, session._record("reverted_modify")
                )
            else:
                # No decodable old URI to revert to: the character keeps the new
                # traits while the bucket was not updated. Report honestly and
                # flag for recovery rather than claiming a revert that didn't happen.
                session.fail(
                    f"equip failed updating the bucket ({e}); the character's URI could not be "
                    f"decoded to revert — it may retain the new traits (journal {session.id})"
                )
                _write_record(
                    deps.records_dir, "equip", session.id, session._record("failed_revert")
                )
            return

        session.state = DONE
        _write_record(deps.records_dir, "equip", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Equip {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
