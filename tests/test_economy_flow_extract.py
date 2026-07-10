import asyncio
import json
import sqlite3

from lfg_core import closet_token as ct
from lfg_core import config
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from tests.economy_helpers import flaky_mirror_conn


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _F:
    def __init__(self, *, fail_sync=False, raise_sync=False, fail_offer=False, raise_offer=False):
        self.minted, self.burns, self.uploads = [], [], 0
        self.modifies = 0
        self.fail_sync = fail_sync
        self.raise_sync = raise_sync
        self.fail_offer = fail_offer
        self.raise_offer = raise_offer

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
        if self.raise_sync:
            raise RuntimeError("timeout after submit")
        if self.fail_sync:
            return None
        self.modifies += 1
        return "MOD"

    async def closet_offer(self, nft_id, owner):
        if self.raise_offer:
            raise RuntimeError("offer submit blew up")
        return None if self.fail_offer else "OFFER"

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
    # The optimistic mirror records the issuer as the current holder (the token is
    # issuer-held until the owner accepts the offer); the listener flips owner→wallet
    # on the AcceptOffer, at which point it becomes a deposit candidate.
    assert ("TRAIT0", config.SWAP_ISSUER_ADDRESS, "Hat", "Cap") in es.read_trait_tokens(conn)


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


# --- #107: phase-aware extract branches ---


def test_extract_mirror_failure_does_not_burn_and_offers(tmp_path):
    """Trait mint OK, Closet decrement committed on-chain, only the CLOSET DB
    mirror fails (distinct from the trait_tokens-mirror case below): the trait
    token must NOT be burned back — the Closet already gave up the trait.
    Delivery continues; DONE with a complete_pending_mirror journal."""
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F()
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.DONE
    assert f.burns == []  # NO destructive compensation
    assert s.nft_id == "TRAIT0"
    assert s.accept == {"xumm_url": "x"}  # offer/accept still ran
    record = json.loads((tmp_path / f"extract-{s.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["sync_tx_hash"] == "MOD"
    assert record["mirror_pending"] is True


def test_extract_indeterminate_keeps_token_no_burn(tmp_path):
    """closet_modify raises (decrement outcome unknown): fail-closed — FAILED,
    the trait token is kept (id journaled), no burn-back."""
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F(raise_sync=True)
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.burns == []
    assert s.nft_id == "TRAIT0"
    record = json.loads((tmp_path / f"extract-{s.id}.json").read_text())
    assert record["status"] == "extract_sync_indeterminate"
    assert record["nft_id"] == "TRAIT0"
    # closet mirror untouched
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 2


def test_extract_mirror_fail_then_offer_fail_precedence(tmp_path):
    """Closet mirror fails AND the later offer step fails: extract's existing
    offer-fail semantics win (DONE with accept=None), and the record still
    carries mirror_pending + sync_tx_hash. No burn."""
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F(fail_offer=True)
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.DONE  # existing offer-fail semantics: DONE, accept=None
    assert s.accept is None
    assert f.burns == []
    record = json.loads((tmp_path / f"extract-{s.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["mirror_pending"] is True
    assert record["sync_tx_hash"] == "MOD"


def test_extract_succeeds_when_mirror_write_fails(tmp_path, monkeypatch):
    """If _sync_then_persist succeeds but the trait_tokens mirror write raises,
    the on-chain extract is NOT reverted — the session ends DONE, no burn fires,
    and the Closet is decremented."""
    import lfg_core.economy_store as _es_module

    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F()
    monkeypatch.setattr(
        _es_module,
        "upsert_trait_token",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.DONE  # on-chain extract succeeded
    assert f.burns == []  # NO compensating burn
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 1  # Closet decremented
    assert s.nft_id == "TRAIT0"  # token kept


def test_extract_mirror_fail_then_offer_raise_keeps_mirror_journal(tmp_path):
    """Greptile #151: the Closet decrement COMMITTED (mirror-fail sets the
    sticky fields in memory) and then the delivery offer RAISES. The outer
    handler must persist a terminal journal carrying mirror_pending +
    sync_tx_hash — otherwise the on-disk record stays at the pre-decrement
    'minted' checkpoint and recovery treats the decrement as never-happened."""
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait(conn)
    f = _F(raise_offer=True)
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.burns == []  # no revert-burn against a drained Closet
    record = json.loads((tmp_path / f"extract-{s.id}.json").read_text())
    assert record["status"] == "failed"
    assert record["mirror_pending"] is True
    assert record["sync_tx_hash"] == "MOD"
