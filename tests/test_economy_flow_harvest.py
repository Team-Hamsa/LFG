# Harvest flow: burn a live character, drop its 8 assets + body into the Bucket.
# Driven entirely through injected fakes — no network.

import asyncio
import json
import sqlite3

from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft

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
    def __init__(self, *, fail_bucket_modify: bool = False) -> None:
        self.burns: list[tuple[str, str]] = []
        self.bucket_modifies: list[tuple[str, str, str]] = []
        self.fail_bucket_modify = fail_bucket_modify
        self.uploads = 0

    async def bucket_upload(self, meta: dict) -> str:
        self.uploads += 1
        return f"https://cdn/b/{self.uploads}.json"

    async def bucket_mint(self, url: str) -> str:
        return "BUCKET"

    async def bucket_offer(self, nft_id: str, owner: str) -> str:
        return "OFFER"

    async def bucket_accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}

    async def bucket_modify(self, nft_id: str, owner: str, url: str):
        if self.fail_bucket_modify:
            return None
        self.bucket_modifies.append((nft_id, owner, url))
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


def test_harvest_happy_path(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert f.burns == [("NFT7", "rUser")]
    assert len(f.bucket_modifies) == 1  # bucket synced on-chain
    assets = {(s, v): n for o, s, v, n in es.read_bucket_assets(conn)}
    assert all(assets[(s, "None")] == 1 for s in NON_BODY)
    assert es.read_bucket_bodies(conn) == [("rUser", 7)]


def test_harvest_rejects_non_burnable(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=False)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []  # never touched the chain
    assert es.read_bucket_bodies(conn) == []


def test_harvest_burn_then_bucket_sync_fails(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes(fail_bucket_modify=True)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == [("NFT7", "rUser")]  # burn happened (irreversible)
    # assets are NOT in the DB (deposit failed) but ARE preserved in the journal
    assert es.read_bucket_bodies(conn) == []
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "harvested_pending_bucket"
    assert record["burn_hash"] == "BURNHASH"
    assert len(record["moved_assets"]) == len(NON_BODY)
