# webapp/economy_api.py
# HTTP-facing economy read model + session plumbing for the Dressing Room.
# Wraps the Phase 2 economy_flow ops (driven via scripts._economy_deps) and the
# per-network on-chain index DB. Kept separate from server.py so the economy
# HTTP concern stays focused.
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from lfg_core import (
    closet_token as ct,
)
from lfg_core import (
    config,
    economy_flow,
    economy_store,
    layer_store,
    nft_index,
    swap_compose,
    swap_meta,
    trait_config,
    trait_economy,
)
from scripts import _economy_deps

TERMINAL_STATES: set[str] = {economy_flow.DONE, economy_flow.FAILED}


def _char_dict(r: nft_index.OnchainNft) -> dict[str, Any]:
    return {
        "nft_id": r.nft_id,
        "edition": r.nft_number,
        "body": r.body,
        "mutable": bool(r.mutable),
        "image_url": r.image,
        "attributes": r.attributes,
        "blank": trait_economy.is_blank(r),
    }


def read_economy_state(conn: sqlite3.Connection, owner: str) -> dict[str, Any]:
    """The Dressing Room's full view for one owner: live characters + Closet."""
    chars = [_char_dict(r) for r in nft_index.owner_live_nfts(conn, owner)]
    assets = [
        {"slot": s, "value": v, "count": c}
        for (o, s, v, c) in economy_store.read_closet_assets(conn)
        if o == owner
    ]
    rec = economy_store.get_closet_record(conn, owner)
    closet_token = {"status": "none", "nft_id": None}
    if rec is not None:
        closet_token = {"status": rec[2], "nft_id": rec[0]}
    trait_tokens = [
        {"nft_id": nid, "slot": s, "value": v}
        for nid, o, s, v in economy_store.read_trait_tokens(conn)
        if o == owner
    ]
    return {
        "characters": chars,
        "closet": {"assets": assets, "token": closet_token},
        "trait_order": swap_meta.TRAIT_ORDER,
        "slots": trait_economy.NON_BODY_SLOTS,
        "trait_tokens": trait_tokens,
    }


async def start_closet(
    discord_id: str, owner: str, user_token: str | None = None
) -> dict[str, Any]:
    """Ensure the owner has a Closet NFToken, minting on first use. Returns a
    status dict with {status, nft_id, accept, accept_push} (accept is the
    Xaman URL or None). ``user_token`` (#212) push-delivers the claim offer."""
    conn = open_conn()
    try:
        deps = _economy_deps.build_economy_deps(conn, user_token=user_token, owner=owner)
        ref = await ct.ensure_closet(
            conn,
            owner,
            upload_fn=deps.closet_upload_fn,
            mint_fn=deps.closet_mint_fn,
            offer_fn=deps.closet_offer_fn,
            accept_payload_fn=deps.closet_accept_fn,
            exists_fn=deps.closet_exists_fn,
        )
        accept = ref.accept_payload or {}
        return {
            "status": ref.status,
            "nft_id": ref.nft_id,
            "accept": accept.get("xumm_url"),
            "accept_push": accept.get("push"),
        }
    finally:
        conn.close()


def economy_session_dict(kind: str, s: Any) -> dict[str, Any]:
    """JSON-safe per-op session status for the client poller."""
    base: dict[str, Any] = {"id": s.id, "state": s.state, "error": s.error}
    if kind == "equip":
        base["displaced"] = [{"slot": k, "value": v} for k, v in s.displaced.items()]
    elif kind == "harvest":
        base["moved_assets"] = s.moved_assets
        # Legacy (non-mutable) harvest remints the edition as a mutable blank and
        # offers it back — surface the one-time upgrade accept QR/push so the
        # client can prompt the sign. The mutable path modifies in place and
        # carries no accept (all None).
        base["accept"] = (s.accept or {}).get("xumm_url")
        base["accept_push"] = (s.accept or {}).get("push")
        base["new_nft_id"] = s.new_nft_id
    elif kind == "assemble":
        r = s.results[0] if s.results else None
        base["accept"] = ((r["accept"] or {}).get("xumm_url")) if r else None
        base["accept_push"] = ((r["accept"] or {}).get("push")) if r else None
        base["image_url"] = r["image_url"] if r else None
        # .get(): older journals / fakes predate the animated-NFT field (#250).
        base["video_url"] = r.get("video_url") if r else None
        base["nft_id"] = r["nft_id"] if r else None
        # Edition surfaces so the firehose announce can say "dressed a blank into #N".
        base["edition"] = s.edition
    elif kind == "extract":
        base["accept"] = (s.accept or {}).get("xumm_url")
        base["accept_push"] = (s.accept or {}).get("push")
        base["nft_id"] = s.nft_id
    elif kind == "deposit":
        base["slot"] = s.slot
        base["value"] = s.value
    return base


@dataclass
class EconomyWebSession:
    """Adapts a Phase 2 economy session to what server.py's session helpers
    expect (discord_id, state, created_at, to_dict)."""

    discord_id: str
    kind: str  # "equip" | "harvest" | "assemble" | "extract" | "deposit"
    inner: Any
    created_at: float = field(default_factory=time.time)
    platform: str = "discord"

    @property
    def id(self) -> str:
        return self.inner.id  # type: ignore[no-any-return]

    @property
    def state(self) -> str:
        return self.inner.state  # type: ignore[no-any-return]

    def to_dict(self) -> dict[str, Any]:
        return {**economy_session_dict(self.kind, self.inner), "platform": self.platform}


class EconomyError(Exception):
    """A user-safe economy precondition/validation failure."""


def open_conn() -> sqlite3.Connection:
    """Open the configured per-network economy index (event-loop thread only)."""
    return _economy_deps.open_index(config.ECONOMY_NETWORK)


def build_settlement_deps(conn: sqlite3.Connection) -> economy_flow.EconomyDeps:
    """The real EconomyDeps for a service-triggered settlement deposit (Task 9,
    spec §Q7: burn a sold trait token back into the buyer's Closet). Same
    wiring as `start_deposit`'s, minus the session-scheduling plumbing —
    settlement runs `run_deposit` to completion in the caller (the buy status
    handler or the settlement sweep) rather than as a client-polled session, so
    there is no EconomyWebSession to hand back. Exists as its own function (a
    thin alias for `_economy_deps.build_economy_deps`) purely as a monkeypatch
    seam for tests."""
    return _economy_deps.build_economy_deps(conn)


def _load_owned_character(
    conn: sqlite3.Connection, owner: str, nft_id: str
) -> nft_index.OnchainNft:
    rec = _economy_deps.load_index_character(conn, nft_id)
    if rec is None:
        raise EconomyError("character not found in the index")
    if rec.owner != owner:
        raise EconomyError("that character is not in your wallet")
    return rec


async def _run_and_close(runner: Any, session: Any, deps: Any, conn: sqlite3.Connection) -> None:
    try:
        await runner(session, deps)
    except Exception as e:  # unexpected crash: ensure the session reaches a terminal state
        session.fail(f"internal error: {e}")
    finally:
        conn.close()


def _schedule(
    kind: str,
    discord_id: str,
    session: Any,
    conn: sqlite3.Connection,
    runner: Any,
    user_token: str | None = None,
) -> EconomyWebSession:
    # Every session that builds an accept payload carries the wallet it is
    # for (`.owner`); pinning it means only that wallet can sign the delivery.
    deps = _economy_deps.build_economy_deps(
        conn, user_token=user_token, owner=getattr(session, "owner", None)
    )
    asyncio.get_running_loop().create_task(_run_and_close(runner, session, deps, conn))
    return EconomyWebSession(discord_id=discord_id, kind=kind, inner=session)


async def _require_body_affinity(char_body: str, slot: str, value: str) -> None:
    """Raise EconomyError unless (slot, value) can legally render on
    char_body. Spec §5: economy ops gate on the SAME check as the swap path —
    allowed = own-dir ∪ (matrix-permitted foreign ∩ source-body affinity),
    which is exactly swap_compose.resolve_layer (source-body affinity is
    enforced inside its foreign branch; do NOT add a target-body
    value_allowed term here — that would reject placements the swap path
    legally produces). "None" is always legal — it's the real-but-file-less
    asset for an empty slot, same convention swap_compose._canonical uses
    when filtering attributes before compose."""
    if value == "None":
        return
    cfg = trait_config.get_config()
    store = layer_store.get_layer_store()
    if await swap_compose.resolve_layer(store, cfg, char_body, slot, value) is None:
        raise EconomyError(f"'{value}' does not fit a {char_body} body")


def normalize_equip_changes(body: dict[str, Any]) -> list[tuple[str, str]]:
    """Wire-level compatibility seam: accept either the batch shape
    {"changes": [{"slot", "value"}, ...]} or the legacy single
    {"slot", "value"}, and return the canonical list of (slot, value) pairs.
    Raises EconomyError (-> HTTP 400) on anything malformed."""
    raw = body.get("changes")
    if raw is None:
        slot, value = body.get("slot"), body.get("value")
        if not slot or not value:
            raise EconomyError("no changes to apply")
        raw = [{"slot": slot, "value": value}]
    if not isinstance(raw, list) or not raw:
        raise EconomyError("no changes to apply")
    if len(raw) > len(trait_economy.NON_BODY_SLOTS):
        raise EconomyError("too many changes in one batch")
    changes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise EconomyError("each change must be {slot, value}")
        slot, value = item.get("slot"), item.get("value")
        if not isinstance(slot, str) or not isinstance(value, str) or not slot or not value:
            raise EconomyError("each change must be {slot, value}")
        if slot in seen:
            raise EconomyError(f"duplicate slot in one batch ({slot})")
        seen.add(slot)
        changes.append((slot, value))
    return changes


async def assemble_options(conn: sqlite3.Connection, owner: str) -> dict[str, Any]:
    """The Builder's data source for assembling a blank: caller-owned blank
    characters + every legal (body, slot, value) combination the caller's
    Closet can currently support. Gates on an active Closet, same as
    start_harvest/start_extract/start_deposit. An empty Closet is NOT an
    error -- it returns empty bodies/options so the client can explain."""
    closet_rec = economy_store.get_closet_record(conn, owner)
    if closet_rec is None or closet_rec[2] != ct.ACTIVE:
        raise EconomyError("Create and claim your Closet first.")

    blanks = [
        {"nft_id": r.nft_id, "edition": r.nft_number}
        for r in nft_index.owner_live_nfts(conn, owner)
        if bool(r.mutable) and trait_economy.is_blank(r)
    ]

    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
    )
    body_class_map = trait_economy.body_class_map(genesis)

    owned_assets = [
        (s, v) for (o, s, v, c) in economy_store.read_closet_assets(conn) if o == owner and c > 0
    ]
    bodies = sorted({v for (s, v) in owned_assets if s == "Body" and v in body_class_map})
    non_body_assets = [(s, v) for (s, v) in owned_assets if s != "Body"]

    cfg = trait_config.get_config()
    store = layer_store.get_layer_store()
    options: dict[str, dict[str, list[str]]] = {}
    for body_value in bodies:
        body_class = body_class_map[body_value]
        per_slot: dict[str, list[str]] = {}
        for slot in trait_economy.NON_BODY_SLOTS:
            values = []
            for s, v in non_body_assets:
                if s != slot:
                    continue
                if v == "None" or await swap_compose.resolve_layer(store, cfg, body_class, s, v):
                    values.append(v)
            per_slot[slot] = values
        options[body_value] = per_slot

    return {
        "blanks": blanks,
        "bodies": bodies,
        "body_class": {b: body_class_map[b] for b in bodies},
        "slots": trait_economy.NON_BODY_SLOTS,
        "options": options,
    }


async def start_equip(
    discord_id: str,
    owner: str,
    nft_id: str,
    changes: list[tuple[str, str]],
    user_token: str | None = None,
) -> EconomyWebSession:
    conn = open_conn()
    try:
        rec = _load_owned_character(conn, owner, nft_id)
        assets = {
            (s, v): c for (o, s, v, c) in economy_store.read_closet_assets(conn) if o == owner
        }
        # Mirror run_equip's running working copy so an over-spending batch is
        # rejected up front, with the same message the flow would produce —
        # including its duplicate-slot guard. A repeated slot would otherwise
        # miscompute displaced/asset counts here (run_equip still rejects it, so
        # nothing reaches the ledger, but the three implementations of this batch
        # contract must agree).
        seen: set[str] = set()
        for slot, value in changes:
            if slot in seen:
                raise EconomyError(f"cannot equip: duplicate slot in one batch ({slot})")
            seen.add(slot)
            chk = trait_economy.can_equip(rec, slot, value, assets, mutable=bool(rec.mutable))
            if not chk.ok:
                raise EconomyError(f"cannot equip: {chk.reason}")
            await _require_body_affinity(rec.body, slot, value)
            displaced = trait_economy.slot_value(rec, slot)
            assets[(slot, value)] = assets.get((slot, value), 0) - 1
            assets[(slot, displaced)] = assets.get((slot, displaced), 0) + 1
    except Exception:
        conn.close()
        raise
    session = economy_flow.EquipSession(owner=owner, character=rec, changes=changes)
    return _schedule("equip", discord_id, session, conn, economy_flow.run_equip, user_token)


async def start_harvest(
    discord_id: str, owner: str, nft_id: str, user_token: str | None = None
) -> EconomyWebSession:
    conn = open_conn()
    closet_rec = economy_store.get_closet_record(conn, owner)
    if closet_rec is None or closet_rec[2] != ct.ACTIVE:
        conn.close()
        raise EconomyError("Create and claim your Closet first.")
    rec = _load_owned_character(conn, owner, nft_id)
    burnable = await _economy_deps.fetch_burnable(owner, nft_id)
    chk = trait_economy.can_harvest(rec, mutable=bool(rec.mutable), burnable=burnable)
    if not chk.ok:
        conn.close()
        raise EconomyError(f"cannot harvest: {chk.reason}")
    session = economy_flow.HarvestSession(owner=owner, character=rec, burnable=burnable)
    return _schedule("harvest", discord_id, session, conn, economy_flow.run_harvest, user_token)


async def start_assemble(
    discord_id: str,
    owner: str,
    nft_id: str,
    body: str,
    chosen: dict[str, str],
    user_token: str | None = None,
) -> EconomyWebSession:
    """Dress a caller-owned BLANK character in place. Loads the character by
    nft_id, resolves its target body class from the effective genesis, runs the
    per-slot body-affinity gate (the same swap_compose.resolve_layer check the
    swap path uses), then schedules run_assemble. The blank precondition is
    enforced inside the flow (te.can_assemble); an unknown body raises here."""
    conn = open_conn()
    try:
        rec = _load_owned_character(conn, owner, nft_id)
        genesis = trait_economy.effective_genesis(
            economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
        )
        body_class = trait_economy.body_class_map(genesis).get(body)
        if body_class is None:
            raise EconomyError(f"unknown body: {body}")
        for slot in trait_economy.NON_BODY_SLOTS:
            if slot in chosen:
                await _require_body_affinity(body_class, slot, chosen[slot])
    except Exception:
        conn.close()
        raise
    session = economy_flow.AssembleSession(
        owner=owner,
        character=rec,
        body_value=body,
        body_class=body_class,
        chosen=chosen,
    )
    return _schedule("assemble", discord_id, session, conn, economy_flow.run_assemble, user_token)


async def start_extract(
    discord_id: str, owner: str, body: dict[str, Any], user_token: str | None = None
) -> EconomyWebSession:
    """Extract a loose Closet trait into a standalone tradeable trait NFToken.
    Gates on an active Closet; raises EconomyError otherwise."""
    slot = body["slot"]  # KeyError here -> 400, no open connection
    value = body["value"]
    conn = open_conn()
    closet_rec = economy_store.get_closet_record(conn, owner)
    if closet_rec is None or closet_rec[2] != ct.ACTIVE:
        conn.close()
        raise EconomyError("Create and claim your Closet first.")
    session = economy_flow.ExtractSession(owner=owner, slot=slot, value=value)
    return _schedule("extract", discord_id, session, conn, economy_flow.run_extract, user_token)


async def start_deposit(
    discord_id: str, owner: str, body: dict[str, Any], user_token: str | None = None
) -> EconomyWebSession:
    """Deposit a standalone trait NFToken back into the owner's Closet.
    Gates on an active Closet; raises EconomyError otherwise."""
    nft_id = body["nft_id"]  # KeyError here -> 400, no open connection
    conn = open_conn()
    closet_rec = economy_store.get_closet_record(conn, owner)
    if closet_rec is None or closet_rec[2] != ct.ACTIVE:
        conn.close()
        raise EconomyError("Create and claim your Closet first.")
    session = economy_flow.DepositSession(owner=owner, nft_id=nft_id)
    return _schedule("deposit", discord_id, session, conn, economy_flow.run_deposit, user_token)
