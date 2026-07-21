# Harvest flow: strip a live character to a BLANK. Mutable characters are
# stripped via NFTokenModify in place; legacy non-mutable-but-burnable
# characters get a one-time burn + remint-as-blank + offer-back upgrade.
# Either way its 9 slot values (8 non-body incl. "None", plus Body) drop into
# the Closet as loose assets. Driven entirely through injected fakes — no
# network.

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


def _char(
    edition: int = 7, body: str = "Straight Blue", mutable: bool = True, uri_hex: str = "AABB"
) -> OnchainNft:
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rUser",
        is_burned=False,
        mutable=mutable,
        uri_hex=uri_hex,
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
        self,
        *,
        fail_closet_modify: bool = False,
        raise_closet_modify: bool = False,
        fail_char_modify: bool = False,
        fail_char_burn: bool = False,
        fail_char_mint: bool = False,
        fail_char_offer: bool = False,
        fail_blank_meta: bool = False,
    ) -> None:
        self.burns: list[tuple[str, str]] = []
        self.char_modifies: list[tuple[str, str, str]] = []
        self.bucket_modifies: list[tuple[str, str, str]] = []
        self.closet_mints: list[str] = []
        self.mints: list[str] = []
        self.offers: list[tuple[str, str]] = []
        self.fail_closet_modify = fail_closet_modify
        self.raise_closet_modify = raise_closet_modify
        self.fail_char_modify = fail_char_modify
        self.fail_char_burn = fail_char_burn
        self.fail_char_mint = fail_char_mint
        self.fail_char_offer = fail_char_offer
        self.fail_blank_meta = fail_blank_meta
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
        return None if self.fail_char_burn else "BURNHASH"

    async def char_compose(self, attrs, body, edition, rev):
        return ("img", None, "meta")

    async def char_mint(self, url: str):
        self.mints.append(url)
        return None if self.fail_char_mint else "NEWCHAR"

    async def char_modify(self, nft_id, owner, url):
        self.char_modifies.append((nft_id, owner, url))
        return None if self.fail_char_modify else "MODIFYHASH"

    async def char_offer(self, nft_id, owner):
        self.offers.append((nft_id, owner))
        return None if self.fail_char_offer else "O"

    async def char_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def blank_meta(self, edition: int) -> str | None:
        return None if self.fail_blank_meta else f"https://cdn/blank/{edition}.json"


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
        blank_meta_fn=f.blank_meta,
        records_dir=str(records_dir),
    )


def _all_slot_assets(conn) -> dict[tuple[str, str], int]:
    return {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}


# --- Mutable path: NFTokenModify to blank in place ---


def test_harvest_mutable_modifies_to_blank_no_burn(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=True), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/blank/7.json")]
    assert f.burns == []  # never burned
    assert session.legacy_upgrade is False
    assets = _all_slot_assets(conn)
    assert all(assets[(s, "None")] == 1 for s in NON_BODY)
    assert assets[("Body", "Straight Blue")] == 1
    assert es.read_supply_changes(conn) == []  # supply-neutral, no ledger rows


def test_harvest_mutable_closet_fail_reverts_character(tmp_path):
    """Modify-to-blank succeeds, then the Closet deposit definitively does not
    commit: the character is modified back to its original URI, journaled
    reverted_modify, and the Closet is left untouched."""
    conn, f = _conn_with_genesis(), _Fakes(fail_closet_modify=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    old_uri_hex = b"ipfs://old".hex()
    char = _char(mutable=True, uri_hex=old_uri_hex)
    session = ef.HarvestSession(owner="rUser", character=char, burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    # First call blanks the character, second call reverts to the old URI.
    assert f.char_modifies[0] == ("NFT7", "rUser", "https://cdn/blank/7.json")
    assert f.char_modifies[1] == ("NFT7", "rUser", "ipfs://old")
    assert _all_slot_assets(conn) == {}  # closet untouched
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "reverted_modify"


def test_harvest_mutable_mirror_failure_completes_pending_mirror(tmp_path):
    """The Closet modify committed on-chain; only the DB mirror write fails:
    DONE, mirror_pending set, no revert of the character."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=True), burnable=False)
    _run(ef.run_harvest(session, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert session.state == ef.DONE
    assert len(f.char_modifies) == 1  # only the blank modify; NO revert modify
    assert len(f.bucket_modifies) == 1
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["sync_tx_hash"] == "MODHASH"
    assert record["mirror_pending"] is True


# --- Legacy path: burn + remint-as-blank + offer-back ---


def test_harvest_legacy_burns_reminits_and_credits_closet(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=False), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert session.legacy_upgrade is True
    assert f.burns == [("NFT7", "rUser")]
    assert f.mints == ["https://cdn/blank/7.json"]
    assert f.offers == [("NEWCHAR", "rUser")]
    assert session.new_nft_id == "NEWCHAR"
    assert session.accept == {"xumm_url": "x"}
    assets = _all_slot_assets(conn)
    assert all(assets[(s, "None")] == 1 for s in NON_BODY)
    assert assets[("Body", "Straight Blue")] == 1

    changes = es.read_supply_changes(conn)
    assert [c["kind"] for c in changes] == ["burn", "mint"]
    assert changes[0]["edition"] == changes[1]["edition"] == 7
    # net zero across the pair, per key
    for key, delta in changes[0]["trait_deltas"].items():
        assert changes[1]["trait_deltas"][key] == -delta


def test_harvest_legacy_offer_fails_credits_closet_pending_offer(tmp_path):
    # Remint succeeds but the delivery offer creation fails: the Closet is still
    # credited (parts belong to the owner), the session stays DONE, but the
    # journal is complete_pending_offer and carries new_nft_id so an admin can
    # locate and re-offer the stranded blank.
    conn, f = _conn_with_genesis(), _Fakes(fail_char_offer=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=False), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert session.new_nft_id == "NEWCHAR"
    assert session.accept is None
    # Closet was still credited with all the harvested parts.
    assets = _all_slot_assets(conn)
    assert all(assets[(s, "None")] == 1 for s in NON_BODY)
    assert assets[("Body", "Straight Blue")] == 1

    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "complete_pending_offer"
    assert record["new_nft_id"] == "NEWCHAR"


def test_harvest_legacy_remint_fails_after_burn(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes(fail_char_mint=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=False), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == [("NFT7", "rUser")]  # burn happened (irreversible)
    assert f.offers == []  # never offered
    assert "admin" in (session.error or "").lower()
    assert _all_slot_assets(conn) == {}  # closet untouched
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "burned_no_remint"
    # only the -1 burn row was written; no +1 mint row (remint never happened)
    changes = es.read_supply_changes(conn)
    assert [c["kind"] for c in changes] == ["burn"]


def test_harvest_legacy_burn_fails_nothing_lost(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes(fail_char_burn=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=False), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.mints == []  # never reminted
    assert es.read_supply_changes(conn) == []
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "failed_burn"


# --- Blank-metadata prep failure (shared by both paths) ---


def test_harvest_blank_meta_failure_changes_nothing(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes(fail_blank_meta=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=True), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.char_modifies == []
    assert f.burns == []
    assert _all_slot_assets(conn) == {}


# --- Preconditions (mutable/burnable gating; Closet requirement) ---


def test_harvest_rejects_neither_mutable_nor_burnable(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=False), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []
    assert f.char_modifies == []
    assert _all_slot_assets(conn) == {}


def test_harvest_rejected_without_active_closet(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    # no closet row at all → status none
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))
    assert session.state == ef.FAILED
    assert f.char_modifies == []  # never touched the chain
    assert "closet" in (session.error or "").lower()


def test_harvest_rejected_when_active_closet_gone_onledger(tmp_path):
    """ACTIVE in the DB but owner-check returns None (token gone on-ledger) → the
    gate fires before the irreversible step, so no character is lost (#101)."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSETX", "AB", status=ct.ACTIVE)
    # Simulate the token no longer owned by the user (gone/transferred).
    f.closet_owner_addr = None

    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.char_modifies == []
    assert "closet" in (session.error or "").lower() or "verified" in (session.error or "").lower()


def test_harvest_succeeds_with_active_closet(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "NFTC", "AB", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))
    assert session.state == ef.DONE
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/blank/7.json")]


def test_harvest_indeterminate_sync_journals_and_fails(tmp_path):
    """closet_modify raises (commit status unknown): fail-closed — FAILED, no
    compensation, journal harvest_sync_indeterminate."""
    conn, f = _conn_with_genesis(), _Fakes(raise_closet_modify=True)
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(mutable=True), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert len(f.char_modifies) == 1  # the blank modify had happened; no revert
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "harvest_sync_indeterminate"
    assert len(record["moved_assets"]) == len(NON_BODY) + 1
    assert record["sync_tx_hash"] is None
    # DB mirror untouched (nothing was credited)
    assert _all_slot_assets(conn) == {}


# --- #107: phase-aware _sync_then_persist (assets-only, Task 5) ---


def test_sync_then_persist_mirror_failure_is_typed(tmp_path):
    """A DB failure AFTER the on-chain Closet modify committed must surface as
    ClosetMirrorError carrying the modify tx hash — not a bare Exception."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    deps = _deps(flaky_mirror_conn(conn), f, tmp_path)
    with pytest.raises(ct.ClosetMirrorError) as ei:
        _run(ef._sync_then_persist(deps, "rUser", {("Head", "Crown"): 1}))
    assert ei.value.tx_hash == "MODHASH"
    assert len(f.bucket_modifies) == 1  # on-chain modify DID happen


def test_sync_then_persist_mirror_failure_rolls_back(tmp_path):
    """Open-transaction hazard (spec 2.3): the injected failure lands after the
    closet_assets DELETE executed. The mirror-failed path must roll the shared
    connection back so the original rows survive — even a later unrelated
    commit() must not persist the half-applied delete."""
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, "rUser", [("Head", "Crown", 2)], [])
    deps = _deps(flaky_mirror_conn(conn), f, tmp_path)
    with pytest.raises(ct.ClosetMirrorError):
        _run(ef._sync_then_persist(deps, "rUser", {("Head", "Crown"): 1}))
    original = [("rUser", "Head", "Crown", 2)]
    assert es.read_closet_assets(conn) == original  # delete rolled back
    conn.commit()  # an unrelated later commit must not resurrect the delete
    assert es.read_closet_assets(conn) == original


def test_sync_then_persist_returns_tx_hash(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    deps = _deps(conn, f, tmp_path)
    got = _run(ef._sync_then_persist(deps, "rUser", {("Head", "Crown"): 1}))
    assert got == "MODHASH"
