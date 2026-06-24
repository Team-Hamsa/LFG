# webapp/economy_api.py
# HTTP-facing economy read model + session plumbing for the Dressing Room.
# Wraps the Phase 2 economy_flow ops (driven via scripts._economy_deps) and the
# per-network on-chain index DB. Kept separate from server.py so the economy
# HTTP concern stays focused.
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from lfg_core import economy_flow, economy_store, nft_index, swap_meta, trait_economy

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
