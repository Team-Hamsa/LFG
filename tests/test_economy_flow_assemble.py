# Assemble flow: body + full set from the Bucket -> mint the edition + offer.
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
    es.set_bucket_token(c, "rUser", "BUCKET", "00")
    es.set_bucket_contents(c, "rUser", [(s, "None", 1) for s in NON_BODY], [edition])
    return c


class _Fakes:
    def __init__(self, *, fail_bucket_modify=False, fail_offer=False) -> None:
        self.fail_bucket_modify = fail_bucket_modify
        self.fail_offer = fail_offer
        self.mints: list[str] = []
        self.char_burns: list[tuple[str, str]] = []
        self.bucket_modifies = 0

    async def bucket_upload(self, meta: dict) -> str:
        return "https://cdn/b.json"

    async def bucket_mint(self, url: str):
        return "BUCKET"

    async def bucket_offer(self, nft_id, owner):
        return "O"

    async def bucket_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def bucket_modify(self, nft_id, owner, url):
        if self.fail_bucket_modify:
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
        return "BURN"

    async def char_offer(self, nft_id, owner):
        return None if self.fail_offer else "OFFER"

    async def char_accept(self, offer_id):
        return {"xumm_url": "accept"}


def _deps(conn, f, records_dir):
    return ef.EconomyDeps(
        conn=conn,
        bucket_upload_fn=f.bucket_upload,
        bucket_mint_fn=f.bucket_mint,
        bucket_offer_fn=f.bucket_offer,
        bucket_accept_fn=f.bucket_accept,
        bucket_modify_fn=f.bucket_modify,
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
    assert es.read_bucket_bodies(conn) == []
    assert es.read_bucket_assets(conn) == []
    assert s.results[0]["accept"] == {"xumm_url": "accept"}


def test_assemble_rejects_incomplete_set(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = _session()
    del s.chosen[NON_BODY[0]]  # missing a slot
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.mints == []  # never minted
    assert es.read_bucket_bodies(conn) == [("rUser", 7)]  # bucket untouched


def test_assemble_mint_then_drain_fails_reverts(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_bucket_modify=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.mints == ["meta"]  # minted...
    assert f.char_burns == [("CHAR7", "")]  # ...then burned back (issuer-held)
    assert s.new_nft_id is None
    assert es.read_bucket_bodies(conn) == [("rUser", 7)]  # bucket untouched


def test_assemble_offer_fail_parks_token(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_offer=True)
    s = _session()
    _run(ef.run_assemble(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert s.new_nft_id == "CHAR7"  # token exists, parked for re-offer
    assert f.char_burns == []  # NOT burned — bucket already drained, no asset loss
    assert es.read_bucket_bodies(conn) == []  # drained
    record = json.loads((tmp_path / f"assemble-{s.id}.json").read_text())
    assert record["status"] == "minted_no_offer"
