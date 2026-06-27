import asyncio
import sqlite3

from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _F:
    def __init__(self, *, fail_sync=False):
        self.minted, self.burns, self.uploads = [], [], 0
        self.modifies = 0
        self.fail_sync = fail_sync

    async def trait_compose(self, slot, value):
        return f"https://cdn/trait/{slot}-{value}.png"

    async def trait_upload(self, meta):
        self.uploads += 1
        return f"https://cdn/t/{self.uploads}.json"

    async def trait_mint(self, url):
        nid = f"TRAIT{len(self.minted)}"
        self.minted.append(nid)
        return nid

    async def trait_burn(self, nft_id, owner):
        self.burns.append((nft_id, owner))
        return "BURN"

    async def closet_upload(self, meta):
        return "https://cdn/c.json"

    async def closet_modify(self, nft_id, owner, url):
        if self.fail_sync:
            return None
        self.modifies += 1
        return "MOD"

    async def closet_offer(self, nft_id, owner):
        return "OFFER"

    async def closet_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def closet_owner(self, nft_id):
        return "rUser"


def _deps(conn, f, tmp):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=f.trait_mint,
        closet_offer_fn=f.closet_offer,
        closet_accept_fn=f.closet_accept,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=None,
        char_mint_fn=None,
        char_modify_fn=None,
        char_burn_fn=None,
        char_offer_fn=f.closet_offer,
        char_accept_fn=f.closet_accept,
        closet_owner_fn=f.closet_owner,
        trait_compose_fn=f.trait_compose,
        trait_upload_fn=f.trait_upload,
        trait_mint_fn=f.trait_mint,
        trait_burn_fn=f.trait_burn,
        records_dir=str(tmp),
    )


def _active_closet_with_trait(conn, owner="rUser"):
    es.init_economy_schema(conn)
    es.set_closet_token(conn, owner, "CLOSET", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, owner, [("Hat", "Cap", 2)], [])


def test_extract_happy_path(tmp_path):
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F()
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.DONE and s.nft_id == "TRAIT0"
    # Closet decremented to 1, trait_tokens has the new token
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 1
    assert ("TRAIT0", "rUser", "Hat", "Cap") in es.read_trait_tokens(conn)


def test_extract_rejected_without_active_closet(tmp_path):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    f = _F()
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED and f.minted == []


def test_extract_rejected_when_trait_absent(tmp_path):
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F()
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Top Hat")  # not in closet
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED and f.minted == []


def test_extract_burns_back_on_closet_sync_failure(tmp_path):
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F(fail_sync=True)
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED
    assert f.burns == [("TRAIT0", "")]  # compensating issuer burn
    assert es.read_trait_tokens(conn) == []  # no token row left
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 2  # closet untouched
