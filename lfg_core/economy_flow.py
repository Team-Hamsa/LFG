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
