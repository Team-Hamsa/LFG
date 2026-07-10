# Harvest flow: burn a live character, drop its 8 assets + body into the Closet.
# Driven entirely through injected fakes — no network.

import asyncio
import json
import sqlite3

import pytest

from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft
from tests.economy_helpers import flaky_mirror_conn

NON_BODY = te.NON_BODY_SLOTS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _char(edition: int = 7, body: str = "Straight Blue") -> OnchainNft:
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rUser",
        is_burned=False,
        mutable=True,
        uri_hex="",
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def _conn_with_genesis(edition: int = 7, body: str = "Straight Blue") -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    genesis = te.Genesis(
        trait_counts={(s, "None"): 1 for s in NON_BODY},
        edition_bodies={edition: (body, "male")},
    )
    es.freeze_genesis(c, genesis, {})
    return c


class _Fakes:
    def __init__(
        self, *, fail_closet_modify: bool = False, raise_closet_modify: bool = False
    ) -> None:
        self.burns: list[tuple[str, str]] = []
        self.bucket_modifies: list[tuple[str, str, str]] = []
        self.closet_mints: list[str] = []
        self.fail_closet_modify = fail_closet_modify
        self.raise_closet_modify = raise_closet_modify
        self.uploads = 0
        # nft_ids that exist_fn should report as on-ledger; everything else is stale.
        self.live_closet_ids: set[str] = set()
        self.events: list[str] = []
        # address returned by closet_owner; None means not yet owned by user.
        self.closet_owner_addr: str | None = "rUser"

    async def closet_owner(self, nft_id: str) -> str | None:
        return self.closet_owner_addr

    async def closet_upload(self, meta: dict) -> str:
        self.uploads += 1
        return f"https://cdn/b/{self.uploads}.json"

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
        if self.raise_closet_modify:
            raise RuntimeError("timeout after submit")
        if self.fail_closet_modify:
            return None
        self.bucket_modifies.append((nft_id, owner, url))
        self.events.append("closet_modify")
        return "MODHASH"

    async def char_burn(self, nft_id: str, owner: str):
        self.burns.append((nft_id, owner))
        self.events.append("char_burn")
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


def _deps(conn, f, records_dir):
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
        records_dir=str(records_dir),
    )


def test_harvest_happy_path(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert f.burns == [("NFT7", "rUser")]
    assert len(f.bucket_modifies) == 1  # bucket synced on-chain
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert all(assets[(s, "None")] == 1 for s in NON_BODY)
    assert es.read_closet_bodies(conn) == [("rUser", 7)]


def test_harvest_rejects_non_burnable(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []  # never touched the chain
    assert es.read_closet_bodies(conn) == []


def test_harvest_burn_then_bucket_sync_fails(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes(fail_closet_modify=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == [("NFT7", "rUser")]  # burn happened (irreversible)
    # assets are NOT in the DB (deposit failed) but ARE preserved in the journal
    assert es.read_closet_bodies(conn) == []
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "harvested_pending_closet"
    assert record["burn_hash"] == "BURNHASH"
    assert len(record["moved_assets"]) == len(NON_BODY)


def test_harvest_stale_active_closet_fails_after_burn(tmp_path):
    """A Closet that is ACTIVE in the DB but missing from the ledger (stale) passes
    the precondition check (DB status is ACTIVE), so the burn proceeds. The
    subsequent NFTokenModify (sync_closet) then hits a missing token and fails,
    leaving assets in the journal for recovery.

    Previously (#101) the old ensure_closet block would re-mint before the burn.
    That block is now removed: users must hold an ACTIVE Closet via the issuance
    UI before harvesting; staleness caught here is a post-burn failure recorded in
    the journal."""
    conn, f = _conn_with_genesis(), _Fakes(fail_closet_modify=True)
    # A stale row: ACTIVE in the DB but the on-ledger token is gone (modify fails).
    es.set_closet_token(conn, "rUser", "DEADBUCKET", "AABB", status=ct.ACTIVE)

    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    # Precondition passes (DB says ACTIVE), burn happens, then deposit fails.
    assert session.state == ef.FAILED
    assert f.burns == [("NFT7", "rUser")]  # burn happened (irreversible)
    assert f.closet_mints == []  # no re-mint; ensure_closet block removed
    assert es.read_closet_bodies(conn) == []  # deposit not committed to DB
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "harvested_pending_closet"
    assert record["burn_hash"] == "BURNHASH"
    assert len(record["moved_assets"]) == len(NON_BODY)


def test_harvest_rejected_without_active_closet(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    # no closet row at all → status none
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))
    assert session.state == ef.FAILED
    assert f.burns == []  # never burned
    assert "closet" in (session.error or "").lower()


def test_harvest_succeeds_with_active_closet(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "NFTC", "AB", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))
    assert session.state == ef.DONE
    assert f.burns == [("NFT7", "rUser")]


# --- #107: phase-aware harvest branches ---


def test_harvest_mirror_failure_completes_pending_mirror(tmp_path):
    """Burn OK, Closet modify OK, only the DB mirror write fails: the chain is
    fully consistent — the session ends DONE (listener converges the mirror)
    and the journal records complete_pending_mirror with the modify tx hash."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert session.state == ef.DONE
    assert f.burns == [("NFT7", "rUser")]  # exactly one burn, no compensation
    assert len(f.bucket_modifies) == 1
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["sync_tx_hash"] == "MODHASH"
    assert record["mirror_pending"] is True


def test_harvest_indeterminate_sync_journals_and_fails(tmp_path):
    """closet_modify raises (commit status unknown): fail-closed — FAILED, no
    compensation, journal harvest_sync_indeterminate with the moved assets."""
    conn, f = _conn_with_genesis(), _Fakes(raise_closet_modify=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == [("NFT7", "rUser")]  # the irreversible burn had happened
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "harvest_sync_indeterminate"
    assert len(record["moved_assets"]) == len(NON_BODY)
    assert record["sync_tx_hash"] is None
    # DB mirror untouched (nothing was credited)
    assert es.read_closet_bodies(conn) == []


# --- #107: phase-aware _sync_then_persist ---


def test_sync_then_persist_mirror_failure_is_typed(tmp_path):
    """A DB failure AFTER the on-chain Closet modify committed must surface as
    ClosetMirrorError carrying the modify tx hash — not a bare Exception."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    deps = _deps(flaky_mirror_conn(conn), f, tmp_path)
    with pytest.raises(ct.ClosetMirrorError) as ei:
        _run(ef._sync_then_persist(deps, "rUser", {("Head", "Crown"): 1}, {7}))
    assert ei.value.tx_hash == "MODHASH"
    assert len(f.bucket_modifies) == 1  # on-chain modify DID happen


def test_sync_then_persist_mirror_failure_rolls_back(tmp_path):
    """Open-transaction hazard (spec 2.3): the injected failure lands after the
    closet_assets DELETE executed. The mirror-failed path must roll the shared
    connection back so the original rows survive — even a later unrelated
    commit() must not persist the half-applied delete."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, "rUser", [("Head", "Crown", 2)], [3])
    deps = _deps(flaky_mirror_conn(conn), f, tmp_path)
    with pytest.raises(ct.ClosetMirrorError):
        _run(ef._sync_then_persist(deps, "rUser", {("Head", "Crown"): 1}, {7}))
    original = [("rUser", "Head", "Crown", 2)]
    assert es.read_closet_assets(conn) == original  # delete rolled back
    assert es.read_closet_bodies(conn) == [("rUser", 3)]
    conn.commit()  # an unrelated later commit must not resurrect the delete
    assert es.read_closet_assets(conn) == original


def test_sync_then_persist_returns_tx_hash(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    deps = _deps(conn, f, tmp_path)
    got = _run(ef._sync_then_persist(deps, "rUser", {("Head", "Crown"): 1}, {7}))
    assert got == "MODHASH"


def test_harvest_rejected_when_active_closet_gone_onledger(tmp_path):
    """ACTIVE in the DB but owner-check returns None (token gone on-ledger) → the
    gate fires before the irreversible burn, so no character is lost (#101)."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSETX", "AB", status=ct.ACTIVE)
    # Simulate the token no longer owned by the user (gone/transferred).
    f.closet_owner_addr = None

    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []  # character was NOT burned
    assert "closet" in (session.error or "").lower() or "verified" in (session.error or "").lower()
