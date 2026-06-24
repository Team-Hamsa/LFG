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

from lfg_core import config, economy_flow, economy_store, nft_index, swap_meta, trait_economy
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
    }


def read_economy_state(conn: sqlite3.Connection, owner: str) -> dict[str, Any]:
    """The Dressing Room's full view for one owner: live characters + Bucket."""
    chars = [_char_dict(r) for r in nft_index.owner_live_nfts(conn, owner)]
    assets = [
        {"slot": s, "value": v, "count": c}
        for (o, s, v, c) in economy_store.read_bucket_assets(conn)
        if o == owner
    ]
    bodies = [ed for (o, ed) in economy_store.read_bucket_bodies(conn) if o == owner]
    return {
        "characters": chars,
        "bucket": {"assets": assets, "bodies": bodies},
        "trait_order": swap_meta.TRAIT_ORDER,
        "slots": trait_economy.NON_BODY_SLOTS,
    }


def economy_session_dict(kind: str, s: Any) -> dict[str, Any]:
    """JSON-safe per-op session status for the client poller."""
    base: dict[str, Any] = {"id": s.id, "state": s.state, "error": s.error}
    if kind == "equip":
        base["displaced"] = s.displaced_value
    elif kind == "harvest":
        base["accept"] = (s.bucket_accept or {}).get("xumm_url")
        base["moved_assets"] = s.moved_assets
    elif kind == "assemble":
        r = s.results[0] if s.results else None
        base["accept"] = ((r["accept"] or {}).get("xumm_url")) if r else None
        base["image_url"] = r["image_url"] if r else None
        base["nft_id"] = r["nft_id"] if r else None
    return base


@dataclass
class EconomyWebSession:
    """Adapts a Phase 2 economy session to what server.py's session helpers
    expect (discord_id, state, created_at, to_dict)."""

    discord_id: str
    kind: str  # "equip" | "harvest" | "assemble"
    inner: Any
    created_at: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return self.inner.id  # type: ignore[no-any-return]

    @property
    def state(self) -> str:
        return self.inner.state  # type: ignore[no-any-return]

    def to_dict(self) -> dict[str, Any]:
        return economy_session_dict(self.kind, self.inner)


class EconomyError(Exception):
    """A user-safe economy precondition/validation failure."""


def open_conn() -> sqlite3.Connection:
    """Open the configured per-network economy index (event-loop thread only)."""
    return _economy_deps.open_index(config.ECONOMY_NETWORK)


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
    kind: str, discord_id: str, session: Any, conn: sqlite3.Connection, runner: Any
) -> EconomyWebSession:
    deps = _economy_deps.build_economy_deps(conn)
    asyncio.get_running_loop().create_task(_run_and_close(runner, session, deps, conn))
    return EconomyWebSession(discord_id=discord_id, kind=kind, inner=session)


async def start_equip(
    discord_id: str, owner: str, nft_id: str, slot: str, value: str
) -> EconomyWebSession:
    conn = open_conn()
    rec = _load_owned_character(conn, owner, nft_id)
    assets = {(s, v): c for (o, s, v, c) in economy_store.read_bucket_assets(conn) if o == owner}
    chk = trait_economy.can_equip(rec, slot, value, assets, mutable=bool(rec.mutable))
    if not chk.ok:
        raise EconomyError(f"cannot equip: {chk.reason}")
    session = economy_flow.EquipSession(owner=owner, character=rec, slot=slot, incoming_value=value)
    return _schedule("equip", discord_id, session, conn, economy_flow.run_equip)


async def start_harvest(discord_id: str, owner: str, nft_id: str) -> EconomyWebSession:
    conn = open_conn()
    rec = _load_owned_character(conn, owner, nft_id)
    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
    )
    burnable = await _economy_deps.fetch_burnable(owner, nft_id)
    chk = trait_economy.can_harvest(rec, genesis, burnable)
    if not chk.ok:
        raise EconomyError(f"cannot harvest: {chk.reason}")
    session = economy_flow.HarvestSession(owner=owner, character=rec, burnable=burnable)
    return _schedule("harvest", discord_id, session, conn, economy_flow.run_harvest)


async def start_assemble(
    discord_id: str, owner: str, edition: int, chosen: dict[str, str]
) -> EconomyWebSession:
    conn = open_conn()
    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
    )
    body = genesis.edition_bodies.get(edition)
    if body is None:
        raise EconomyError(f"edition {edition} has no known body")
    assets = {(s, v): c for (o, s, v, c) in economy_store.read_bucket_assets(conn) if o == owner}
    bodies = {ed for (o, ed) in economy_store.read_bucket_bodies(conn) if o == owner}
    live_editions = {r.nft_number for r in nft_index.live_nfts(conn) if r.nft_number is not None}
    chk = trait_economy.can_assemble(edition, chosen, bodies, assets, live_editions, genesis)
    if not chk.ok:
        raise EconomyError(f"cannot assemble: {chk.reason}")
    session = economy_flow.AssembleSession(
        owner=owner,
        edition=edition,
        chosen=chosen,
        body_value=body[0],
        body_class=body[1],
        live_editions=live_editions,
    )
    return _schedule("assemble", discord_id, session, conn, economy_flow.run_assemble)
