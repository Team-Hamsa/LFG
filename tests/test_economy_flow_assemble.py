# Assemble flow: body + full set from the Closet -> mint the edition + offer.
# Driven through injected fakes — no network.

import asyncio
import json
import sqlite3

from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import trait_economy as te

NON_BODY = te.NON_BODY_SLOTS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _conn_with_bucket(edition: int = 7) -> sqlite3.Connection:
    """Genesis with one edition; the owner's bucket holds that body + a full
    'None' asset set + an existing bucket token."""
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    genesis = te.Genesis(
        trait_counts={(s, "None"): 1 for s in NON_BODY},
        edition_bodies={edition: ("Straight Blue", "male")},
    )
    es.freeze_genesis(c, genesis, {})
    es.set_closet_token(c, "rUser", "CLOSET", "00")
    es.set_closet_contents(c, "rUser", [(s, "None", 1) for s in NON_BODY], [edition])
    return c


class _Fakes:
    def __init__(self, *, fail_closet_modify=False, fail_offer=False, fail_char_burn=False) -> None:
        self.fail_closet_modify = fail_closet_modify
        self.fail_offer = fail_offer
        self.fail_char_burn = fail_char_burn
        self.mints: list[str] = []
        self.char_burns: list[tuple[str, str]] = []
        self.bucket_modifies = 0

    async def closet_upload(self, meta: dict) -> str:
        return "https://cdn/b.json"

    async def closet_mint(self, url: str):
        return "CLOSET"

    async def closet_offer(self, nft_id, owner):
        return "O"

    async def closet_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def closet_modify(self, nft_id, owner, url):
        if self.fail_closet_modify:
            return None
        self.bucket_modifies += 1
        return "MODHASH"

    async def char_compose(self, attrs, body, edition, rev):
        return ("img", None, "meta")

    async def char_mint(self, url: str):
        self.mints.append(url)
        return "CHAR7"

    async def char_modify(self, nft_id, owner, url):
        return "H"

    async def char_burn(self, nft_id, owner):
        self.char_burns.append((nft_id, owner))
        return None if self.fail_char_burn else "BURN"

    async def char_offer(self, nft_id, owner):
        return None if self.fail_offer else "OFFER"

    async def char_accept(self, offer_id):
        return {"xumm_url": "accept"}


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
        records_dir=str(records_dir),
    )


def _session() -> ef.AssembleSession:
    return ef.AssembleSession(
        owner="rUser",
        edition=7,
        chosen=dict.fromkeys(NON_BODY, "None"),
        body_value="Straight Blue",
        body_class="male",
        live_editions=set(),
    )


def test_assemble_happy_path(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.new_nft_id == "CHAR7"
    assert f.bucket_modifies == 1
    # bucket fully drained
    assert es.read_closet_bodies(conn) == []
    assert es.read_closet_assets(conn) == []
    assert s.results[0]["accept"] == {"xumm_url": "accept"}


def test_assemble_rejects_incomplete_set(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    del s.chosen[NON_BODY[0]]  # missing a slot
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.mints == []  # never minted
    assert es.read_closet_bodies(conn) == [("rUser", 7)]  # bucket untouched


def test_assemble_mint_then_drain_fails_reverts(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_closet_modify=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.mints == ["meta"]  # minted...
    assert f.char_burns == [("CHAR7", "")]  # ...then burned back (issuer-held)
    assert s.new_nft_id is None
    assert es.read_closet_bodies(conn) == [("rUser", 7)]  # bucket untouched


def test_assemble_drain_fail_then_burnback_fail_keeps_nft_id(tmp_path):
    # Mint succeeds, bucket drain fails, AND the compensating burn-back fails:
    # the minted token's id MUST be retained in the journal for admin recovery.
    conn, f = _conn_with_bucket(), _Fakes(fail_closet_modify=True, fail_char_burn=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert s.new_nft_id == "CHAR7"  # NOT wiped — token is stranded, id preserved
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "failed_revert_mint"
    assert record["new_nft_id"] == "CHAR7"


def test_assemble_offer_fail_parks_token(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_offer=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert s.new_nft_id == "CHAR7"  # token exists, parked for re-offer
    assert f.char_burns == []  # NOT burned — bucket already drained, no asset loss
    assert es.read_closet_bodies(conn) == []  # drained
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "minted_no_offer"
