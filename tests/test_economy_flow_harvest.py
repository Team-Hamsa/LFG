# Harvest flow: burn a live character, drop its 8 assets + body into the Closet.
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
    def __init__(self, *, fail_closet_modify: bool = False) -> None:
        self.burns: list[tuple[str, str]] = []
        self.bucket_modifies: list[tuple[str, str, str]] = []
        self.closet_mints: list[str] = []
        self.fail_closet_modify = fail_closet_modify
        self.uploads = 0
        # nft_ids that exist_fn should report as on-ledger; everything else is stale.
        self.live_closet_ids: set[str] = set()
        self.events: list[str] = []

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
        records_dir=str(records_dir),
    )


def test_harvest_happy_path(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
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
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == [("NFT7", "rUser")]  # burn happened (irreversible)
    # assets are NOT in the DB (deposit failed) but ARE preserved in the journal
    assert es.read_closet_bodies(conn) == []
    record = json.loads((tmp_path / f"harvest-{session.id}.json").read_text())
    assert record["status"] == "harvested_pending_bucket"
    assert record["burn_hash"] == "BURNHASH"
    assert len(record["moved_assets"]) == len(NON_BODY)


def test_harvest_remints_stale_bucket_before_burn(tmp_path):
    """#101: a stale bucket record (token gone from the ledger) must be detected
    and re-minted BEFORE the irreversible character burn — otherwise the later
    NFTokenModify hits tecNO_ENTRY and the harvested assets are lost. With the
    on-ledger check wired, the bucket is re-minted, the burn proceeds, and the
    assets land in the (fresh) bucket."""
    conn, f = _conn_with_genesis(), _Fakes()
    # A stale row: an nft_id that exists in the DB but NOT on-ledger.
    es.set_closet_token(conn, "rUser", "DEADBUCKET", "AABB")
    assert "DEADBUCKET" not in f.live_closet_ids  # exists_fn -> False

    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    # The dead bucket was re-minted...
    assert len(f.closet_mints) == 1
    fresh_id = f.closet_mints[0]
    assert es.get_closet_token(conn, "rUser")[0] == fresh_id
    # ...the character was burned...
    assert f.burns == [("NFT7", "rUser")]
    # ...the bucket modify succeeded against the fresh token...
    assert [m[0] for m in f.bucket_modifies] == [fresh_id]
    # ...and the assets/body landed.
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert all(assets[(s, "None")] == 1 for s in NON_BODY)
    assert es.read_closet_bodies(conn) == [("rUser", 7)]
    # Ordering: bucket ensured (re-mint) BEFORE the burn, which is before the deposit.
    assert f.events == ["closet_mint", "char_burn", "closet_modify"]
