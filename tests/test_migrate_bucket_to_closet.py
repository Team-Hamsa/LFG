# tests/test_migrate_bucket_to_closet.py
# Migration: re-mint legacy Bucket NFTs (LEGACY_BUCKET_TAXON) as Closets under
# CLOSET_TAXON.  All network calls are replaced with fakes so no XRPL connection
# is needed.

import asyncio
import os
import sqlite3
import sys

# Make scripts/ importable (mirrors test_economy_scripts_import.py)
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))

# Stub out env vars that config.py requires at import time
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config  # noqa: E402
from lfg_core import economy_flow as ef  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Fake deps (mirrors the _Fakes class from test_economy_flow_harvest.py)
# ---------------------------------------------------------------------------


class _Fakes:
    def __init__(self, *, taxon_for: dict[str, int] | None = None) -> None:
        self.burns: list[tuple[str, str]] = []
        self.bucket_modifies: list[tuple[str, str, str]] = []
        self.closet_mints: list[str] = []
        self.uploads = 0
        self.live_closet_ids: set[str] = set()
        self.events: list[str] = []
        self.closet_owner_addr: str | None = "rUser"
        # Map nft_id -> taxon (int) for fake on-ledger taxon lookups
        self._taxon_for: dict[str, int] = taxon_for or {}

    async def nft_info(self, nft_id: str) -> dict | None:
        """Fake xrpl_ops.nft_info: returns minimal info dict or None."""
        if nft_id in self._taxon_for:
            return {"nft_id": nft_id, "taxon": self._taxon_for[nft_id], "owner": "rUser"}
        return None

    async def closet_owner(self, nft_id: str) -> str | None:
        return self.closet_owner_addr

    async def closet_upload(self, meta: dict) -> str:
        self.uploads += 1
        return f"https://cdn/closet/{self.uploads}.json"

    async def closet_mint(self, url: str) -> str:
        nft_id = f"CLOSET{len(self.closet_mints)}"
        self.closet_mints.append(nft_id)
        self.events.append("closet_mint")
        self.live_closet_ids.add(nft_id)
        return nft_id

    async def closet_exists(self, nft_id: str) -> bool:
        return nft_id in self.live_closet_ids

    async def closet_offer(self, nft_id: str, owner: str) -> str:
        return "OFFER"

    async def closet_accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}

    async def closet_modify(self, nft_id: str, owner: str, url: str):
        self.bucket_modifies.append((nft_id, owner, url))
        self.events.append("closet_modify")
        return "MODHASH"

    async def char_burn(self, nft_id: str, owner: str):
        self.burns.append((nft_id, owner))
        return "BURNHASH"

    async def char_compose(self, attrs, body, edition, rev):
        return ("img", None, "meta")

    async def char_mint(self, url: str):
        return "CHAR"

    async def char_modify(self, nft_id, owner, url):
        return "H"

    async def char_offer(self, nft_id, owner):
        return "O"

    async def char_accept(self, offer_id):
        return {"xumm_url": "x"}


def _economy_deps(conn, f, tmp_path):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=f.closet_mint,
        closet_offer_fn=f.closet_offer,
        closet_accept_fn=f.closet_accept,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=f.char_compose,
        char_mint_fn=f.char_mint,
        char_modify_fn=f.char_modify,
        char_burn_fn=f.char_burn,
        char_offer_fn=f.char_offer,
        char_accept_fn=f.char_accept,
        closet_exists_fn=f.closet_exists,
        closet_owner_fn=f.closet_owner,
        records_dir=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Import the migration module (must fail before it exists)
# ---------------------------------------------------------------------------

from migrate_bucket_to_closet import migrate_owner  # noqa: E402

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migrate_path_remints_and_syncs_contents(tmp_path):
    """An owner with a legacy-taxon recorded closet gets a NEW closet token,
    their existing assets/bodies are synced into it, the old nft_id is recorded
    as abandoned, and the status is left as pending_accept."""
    conn = _mem_conn()
    owner = "rUser"
    old_nft_id = "LEGACY_BUCKET_0001"

    # Seed a recorded closet under the legacy taxon
    es.set_closet_token(conn, owner, old_nft_id, "AABB", status=ct.ACTIVE, offer_id=None)
    # Seed some assets and a body into the DB
    es.set_closet_contents(conn, owner, [("Eyes", "Blue", 2), ("Hat", "Cap", 1)], [42])

    # Fake: the on-ledger token reports the LEGACY taxon
    f = _Fakes(taxon_for={old_nft_id: config.LEGACY_BUCKET_TAXON})
    d = _economy_deps(conn, f, tmp_path)

    result = _run(migrate_owner(conn, owner, d, nft_info_fn=f.nft_info))

    # A new closet was minted
    assert result["owner"] == owner
    assert result["skipped"] is False
    assert result["old_nft_id"] == old_nft_id
    new_nft_id = result["new_nft_id"]
    assert new_nft_id is not None
    assert new_nft_id != old_nft_id
    assert "closet_mint" in f.events

    # Contents were synced into the new token
    assert result["asset_count"] == 2
    assert result["body_count"] == 1
    assert "closet_modify" in f.events

    # The DB record points to the new nft_id and is pending_accept
    rec = es.get_closet_record(conn, owner)
    assert rec is not None
    assert rec[0] == new_nft_id
    assert rec[2] == ct.PENDING_ACCEPT

    # Status reflects the new closet's lifecycle state (pending_accept until the
    # owner accepts the on-chain offer) — not to be confused with "migrated" as
    # a verb; the return dict's skipped=False already conveys that.
    assert result["status"] == ct.PENDING_ACCEPT


def test_migrate_restores_legacy_record_on_mint_failure(tmp_path):
    """If ensure_closet fails (mint/offer error) after the legacy record was
    deleted, migrate_owner restores the legacy record (and re-raises) so a
    re-run retries instead of dead-ending on 'no record — nothing to migrate'."""
    import pytest

    conn = _mem_conn()
    owner = "rUser"
    old_nft_id = "LEGACY_BUCKET_0001"
    es.set_closet_token(conn, owner, old_nft_id, "AABB", status=ct.ACTIVE, offer_id="OF9")
    es.set_closet_contents(conn, owner, [("Eyes", "Blue", 2)], [42])

    f = _Fakes(taxon_for={old_nft_id: config.LEGACY_BUCKET_TAXON})

    async def _failing_mint(_url: str):
        return None  # ensure_closet raises ClosetError on a falsy nft_id

    f.closet_mint = _failing_mint  # type: ignore[method-assign]
    d = _economy_deps(conn, f, tmp_path)

    with pytest.raises(ct.ClosetError):
        _run(migrate_owner(conn, owner, d, nft_info_fn=f.nft_info))

    # The legacy record is restored intact (nft_id, status, offer_id preserved).
    rec = es.get_closet_record(conn, owner)
    assert rec is not None
    assert rec[0] == old_nft_id
    assert rec[2] == ct.ACTIVE
    assert rec[3] == "OF9"


def test_idempotent_already_on_closet_taxon(tmp_path):
    """An owner whose recorded token is already on CLOSET_TAXON is skipped."""
    conn = _mem_conn()
    owner = "rUser"
    current_nft_id = "CLOSET_TAXON_0001"

    es.set_closet_token(conn, owner, current_nft_id, "CCDD", status=ct.ACTIVE, offer_id=None)

    f = _Fakes(taxon_for={current_nft_id: config.CLOSET_TAXON})
    d = _economy_deps(conn, f, tmp_path)

    result = _run(migrate_owner(conn, owner, d, nft_info_fn=f.nft_info))

    assert result["skipped"] is True
    assert result["reason"] == "already on CLOSET_TAXON"
    # No mints happened
    assert f.closet_mints == []


def test_no_record_is_skipped(tmp_path):
    """An owner with no recorded closet is simply skipped."""
    conn = _mem_conn()
    owner = "rNoCloset"

    f = _Fakes()
    d = _economy_deps(conn, f, tmp_path)

    result = _run(migrate_owner(conn, owner, d, nft_info_fn=f.nft_info))

    assert result["skipped"] is True
    assert "no record" in result["reason"].lower()
    assert f.closet_mints == []
