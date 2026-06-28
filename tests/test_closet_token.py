# Closet NFToken metadata builder/parser round-trips (pure).

import asyncio
import sqlite3

from lfg_core import closet_token as bt
from lfg_core import economy_store as es


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c


class _ClosetFakes:
    def __init__(self) -> None:
        self.mints: list[str] = []
        self.uploads = 0

    async def upload(self, meta: dict) -> str:
        self.uploads += 1
        return f"https://cdn/closet/{self.uploads}.json"

    async def mint(self, url: str) -> str:
        self.mints.append(url)
        return f"FRESHNFT{len(self.mints)}"

    async def offer(self, nft_id: str, owner: str) -> str:
        return "OFFER1"

    async def accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}


def test_metadata_roundtrips():
    assets = [("Head", "None", 3), ("Background", "Blue", 1)]
    bodies = [3536, 12]
    meta = bt.build_closet_metadata("rUser", assets, bodies)
    assert meta["lfg_closet"]["bodies"] == [12, 3536]  # sorted
    assert meta["name"] == "LFG Closet — rUser"
    got_assets, got_bodies = bt.parse_closet_metadata(meta)
    assert sorted(got_assets) == sorted(assets)
    assert got_bodies == [12, 3536]


def test_none_assets_preserved():
    meta = bt.build_closet_metadata("rUser", [("Head", "None", 2)], [])
    got_assets, got_bodies = bt.parse_closet_metadata(meta)
    assert got_assets == [("Head", "None", 2)]
    assert got_bodies == []


def test_empty_closet():
    meta = bt.build_closet_metadata("rUser", [], [])
    assert bt.parse_closet_metadata(meta) == ([], [])


def test_parse_tolerates_garbage():
    assert bt.parse_closet_metadata({}) == ([], [])
    assert bt.parse_closet_metadata({"lfg_closet": "x"}) == ([], [])
    assert bt.parse_closet_metadata({"lfg_closet": {"assets": "x"}}) == ([], [])
    # malformed entries are skipped, valid ones kept
    mixed = {
        "lfg_closet": {
            "assets": [{"slot": "Head"}, {"slot": "Eyes", "value": "Blue", "count": 1}],
            "bodies": ["x", 7],
        }
    }
    assert bt.parse_closet_metadata(mixed) == ([("Eyes", "Blue", 1)], [7])


# --- Task 3 tests (new lifecycle: pending_accept → active) ---


class _F:
    def __init__(self, exists=True, owner=None):
        self.minted = 0
        self.offers = 0
        self.exists = exists
        self.owner = owner

    async def up(self, meta):
        return "https://cdn/c.json"

    async def mint(self, url):
        self.minted += 1
        return f"NFT{self.minted}"

    async def offer(self, nft_id, owner):
        self.offers += 1
        return f"OF{self.offers}"

    async def accept(self, offer_id):
        return {"xumm_url": f"x/{offer_id}"}

    async def exists_fn(self, nft_id):
        return self.exists

    async def owner_fn(self, nft_id):
        return self.owner


def test_ensure_closet_first_use_records_pending():
    c, f = _conn(), _F()
    ref = _run(
        bt.ensure_closet(
            c, "rA", upload_fn=f.up, mint_fn=f.mint, offer_fn=f.offer, accept_payload_fn=f.accept
        )
    )
    assert ref.status == bt.PENDING_ACCEPT and ref.minted and ref.accept_payload
    assert es.get_closet_record(c, "rA")[2] == bt.PENDING_ACCEPT
    assert f.minted == 1


def test_ensure_closet_pending_is_idempotent_and_reshows_accept():
    c, f = _conn(), _F()
    _run(
        bt.ensure_closet(
            c, "rA", upload_fn=f.up, mint_fn=f.mint, offer_fn=f.offer, accept_payload_fn=f.accept
        )
    )
    ref = _run(
        bt.ensure_closet(
            c,
            "rA",
            upload_fn=f.up,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
            exists_fn=f.exists_fn,
        )
    )
    assert f.minted == 1  # did NOT re-mint
    assert ref.status == bt.PENDING_ACCEPT and ref.accept_payload  # re-showed accept


def test_ensure_closet_pending_without_offer_id_creates_fresh_offer():
    """A listener-rebuilt pending Closet loses its offer_id (offer ids are not
    on-chain). ensure_closet must self-heal by creating a fresh offer for the
    existing token and persisting it, so the accept QR can still be shown."""
    c, f = _conn(), _F()
    # Seed a pending closet with NO offer_id (as the listener writes it).
    es.set_closet_token(c, "rA", "NFTC", "AABB", status=bt.PENDING_ACCEPT, offer_id=None)
    ref = _run(
        bt.ensure_closet(
            c, "rA", upload_fn=f.up, mint_fn=f.mint, offer_fn=f.offer, accept_payload_fn=f.accept
        )
    )
    assert f.minted == 0  # did NOT re-mint the closet
    assert f.offers == 1  # created exactly one fresh offer
    assert ref.status == bt.PENDING_ACCEPT and ref.accept_payload  # QR recovered
    # The fresh offer id is persisted so a later re-show reuses it.
    assert es.get_closet_record(c, "rA")[3] == "OF1"


def test_ensure_closet_pending_raises_when_fresh_offer_fails():
    """If the fresh-offer recovery itself fails (offer_fn returns falsey), surface
    it as an error instead of silently returning accept=None."""

    class _FNoOffer(_F):
        async def offer(self, nft_id, owner):
            return None  # offer creation fails

    c, f = _conn(), _FNoOffer()
    es.set_closet_token(c, "rA", "NFTC", "AABB", status=bt.PENDING_ACCEPT, offer_id=None)
    try:
        _run(
            bt.ensure_closet(
                c,
                "rA",
                upload_fn=f.up,
                mint_fn=f.mint,
                offer_fn=f.offer,
                accept_payload_fn=f.accept,
            )
        )
        raise AssertionError("expected ClosetError")
    except bt.ClosetError:
        pass


def test_ensure_closet_stale_record_remints():
    c, f = _conn(), _F(exists=False)
    _run(
        bt.ensure_closet(
            c, "rA", upload_fn=f.up, mint_fn=f.mint, offer_fn=f.offer, accept_payload_fn=f.accept
        )
    )
    ref = _run(
        bt.ensure_closet(
            c,
            "rA",
            upload_fn=f.up,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
            exists_fn=f.exists_fn,
        )
    )
    assert f.minted == 2 and ref.nft_id == "NFT2"


def test_confirm_accept_promotes_when_owner_matches():
    c, f = _conn(), _F(owner="rA")
    _run(
        bt.ensure_closet(
            c, "rA", upload_fn=f.up, mint_fn=f.mint, offer_fn=f.offer, accept_payload_fn=f.accept
        )
    )
    assert _run(bt.confirm_accept(c, "rA", owner_fn=f.owner_fn)) == bt.ACTIVE
    assert es.get_closet_record(c, "rA")[2] == bt.ACTIVE


def test_confirm_accept_stays_pending_when_owner_mismatch():
    c, f = _conn(), _F(owner="rISSUER")
    _run(
        bt.ensure_closet(
            c, "rA", upload_fn=f.up, mint_fn=f.mint, offer_fn=f.offer, accept_payload_fn=f.accept
        )
    )
    assert _run(bt.confirm_accept(c, "rA", owner_fn=f.owner_fn)) == bt.PENDING_ACCEPT


# --- Legacy tests updated for new lifecycle ---


def test_ensure_closet_remints_when_record_stale():
    """A DB record that no longer exists on-ledger (e.g. after a testnet reset)
    is treated as stale: ensure_closet mints a fresh closet rather than trusting
    the dead nft_id (which would later make NFTokenModify fail tecNO_ENTRY)."""
    c, f = _conn(), _ClosetFakes()
    es.set_closet_token(c, "rUser", "STALENFT", "AABB", status=bt.ACTIVE)

    async def absent(nft_id: str) -> bool:
        return False

    ref = _run(
        bt.ensure_closet(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
            exists_fn=absent,
        )
    )
    assert ref.minted is True
    assert ref.nft_id != "STALENFT"
    assert len(f.mints) == 1
    # The stale row was overwritten with the fresh token.
    assert es.get_closet_token(c, "rUser")[0] == ref.nft_id


def test_ensure_closet_keeps_record_when_on_ledger():
    """A DB record that still exists on-ledger is returned as-is (no re-mint)."""
    c, f = _conn(), _ClosetFakes()
    es.set_closet_token(c, "rUser", "LIVENFT", "AABB", status=bt.ACTIVE)

    async def present(nft_id: str) -> bool:
        return True

    ref = _run(
        bt.ensure_closet(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
            exists_fn=present,
        )
    )
    assert ref.minted is False
    assert ref.nft_id == "LIVENFT"
    assert f.mints == []


def test_ensure_closet_no_exists_fn_trusts_record():
    """Back-compat: callers that don't pass exists_fn trust the DB record."""
    c, f = _conn(), _ClosetFakes()
    es.set_closet_token(c, "rUser", "LIVENFT", "AABB", status=bt.ACTIVE)

    ref = _run(
        bt.ensure_closet(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
        )
    )
    assert ref.minted is False
    assert ref.nft_id == "LIVENFT"
    assert f.mints == []


# --- Task 3 Fix 1 coverage: sync_closet must preserve status + offer_id ---


class _SyncFakes:
    """Minimal fakes for sync_closet (upload + modify)."""

    def __init__(self) -> None:
        self.uploads = 0
        self.modifies: list[tuple[str, str, str]] = []

    async def upload(self, meta: dict) -> str:
        self.uploads += 1
        return f"https://cdn/closet/sync{self.uploads}.json"

    async def modify(self, nft_id: str, owner: str, url: str) -> str:
        self.modifies.append((nft_id, owner, url))
        return "SYNCHASH"


def test_sync_closet_preserves_active_status():
    """sync_closet must not reset an ACTIVE closet back to pending_accept."""
    c = _conn()
    es.set_closet_token(c, "rB", "NFT_ACTIVE", "AABB", status=bt.ACTIVE, offer_id="OF1")
    f = _SyncFakes()
    _run(bt.sync_closet(c, "rB", [("Head", "Blue", 1)], [], upload_fn=f.upload, modify_fn=f.modify))
    rec = es.get_closet_record(c, "rB")
    assert rec is not None
    assert rec[2] == bt.ACTIVE, f"expected ACTIVE, got {rec[2]!r}"
    assert rec[3] == "OF1", f"expected offer_id OF1, got {rec[3]!r}"


def test_confirm_accept_returns_none_without_record():
    """confirm_accept returns 'none' when no closet is recorded for the owner."""
    c = _conn()

    async def owner_fn(nft_id: str) -> str | None:
        return "rISSUER"

    result = _run(bt.confirm_accept(c, "rNoone", owner_fn=owner_fn))
    assert result == "none"


def test_ensure_closet_stale_remint_records_pending():
    """After a stale-remint, the NEW record is recorded as pending_accept."""
    c, f = _conn(), _F(exists=False)
    # Seed a stale record
    _run(
        bt.ensure_closet(
            c, "rA", upload_fn=f.up, mint_fn=f.mint, offer_fn=f.offer, accept_payload_fn=f.accept
        )
    )
    # Now remint (exists=False triggers stale path)
    _run(
        bt.ensure_closet(
            c,
            "rA",
            upload_fn=f.up,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
            exists_fn=f.exists_fn,
        )
    )
    rec = es.get_closet_record(c, "rA")
    assert rec is not None
    assert rec[2] == bt.PENDING_ACCEPT, f"expected PENDING_ACCEPT, got {rec[2]!r}"
