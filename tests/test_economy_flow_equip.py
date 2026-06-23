# Equip flow: move a loose asset onto a live character; displaced -> Bucket.
# Driven through injected fakes — no network.

import asyncio
import sqlite3

from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft

NON_BODY = te.NON_BODY_SLOTS
OLD_URL = "https://cdn/old.json"
OLD_URI_HEX = OLD_URL.encode().hex().upper()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _char() -> OnchainNft:
    attrs = [{"trait_type": "Body", "value": "Straight Blue"}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id="NFT7",
        nft_number=7,
        owner="rUser",
        is_burned=False,
        mutable=True,
        uri_hex=OLD_URI_HEX,
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def _conn_with_bucket() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    es.freeze_genesis(
        c, te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")}), {}
    )
    es.set_bucket_token(c, "rUser", "BUCKET", "00")
    es.set_bucket_contents(c, "rUser", [("Head", "Crown", 1)], [])
    return c


class _Fakes:
    def __init__(self, *, fail_bucket_modify=False) -> None:
        self.fail_bucket_modify = fail_bucket_modify
        self.char_modifies: list[tuple[str, str, str]] = []

    async def bucket_upload(self, meta: dict) -> str:
        return "https://cdn/b.json"

    async def bucket_mint(self, url):
        return "BUCKET"

    async def bucket_offer(self, nft_id, owner):
        return "O"

    async def bucket_accept(self, offer_id):
        return {}

    async def bucket_modify(self, nft_id, owner, url):
        return None if self.fail_bucket_modify else "MODHASH"

    async def char_compose(self, attrs, body, edition, rev):
        return ("img", None, "https://cdn/new.json")

    async def char_mint(self, url):
        return "CHAR"

    async def char_modify(self, nft_id, owner, url):
        self.char_modifies.append((nft_id, owner, url))
        return "MODH"

    async def char_burn(self, nft_id, owner):
        return "BURN"

    async def char_offer(self, nft_id, owner):
        return "O"

    async def char_accept(self, offer_id):
        return {}


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


def test_equip_happy_path(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), slot="Head", incoming_value="Crown")
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.displaced_value == "None"
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]
    assets = {(slot, v): n for o, slot, v, n in es.read_bucket_assets(conn)}
    assert ("Head", "Crown") not in assets  # incoming consumed
    assert assets[("Head", "None")] == 1  # displaced returned


def test_equip_rejects_missing_asset(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), slot="Head", incoming_value="Tiara")
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []  # never touched the character


def test_equip_modify_then_bucket_fails_reverts(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes(fail_bucket_modify=True)
    s = ef.EquipSession(owner="rUser", character=_char(), slot="Head", incoming_value="Crown")
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    # modified to the new metadata, then reverted to the old on-chain URI
    assert f.char_modifies == [
        ("NFT7", "rUser", "https://cdn/new.json"),
        ("NFT7", "rUser", OLD_URL),
    ]
    # bucket untouched
    assets = {(slot, v): n for o, slot, v, n in es.read_bucket_assets(conn)}
    assert assets == {("Head", "Crown"): 1}


def test_equip_bucket_fails_and_uri_undecodable_reports_honestly(tmp_path):
    import json

    conn, f = _conn_with_bucket(), _Fakes(fail_bucket_modify=True)
    rec = _char()
    rec.uri_hex = ""  # no decodable old URI to revert to
    s = ef.EquipSession(owner="rUser", character=rec, slot="Head", incoming_value="Crown")
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    # only the forward modify happened; NO false revert was attempted
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]
    record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
    assert record["status"] == "failed_revert"
