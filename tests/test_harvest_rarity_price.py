# Harvest/assemble rarity bookkeeping (#305): a harvest burn must leave the
# rarity live-count (raising the Trait Shop price), and an assemble rebirth
# must put the edition back. Env-guard preamble per tests convention.

import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio
import sqlite3

from lfg_core import closet_token as ct
from lfg_core import config, db_helpers, rarity, shop
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft

NET = config.ECONOMY_NETWORK
NON_BODY = te.NON_BODY_SLOTS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- app-DB fixture: production-shaped LFG + legacy burned_nfts -------------


def _app_db(path):
    c = sqlite3.connect(path)
    c.execute(
        """CREATE TABLE LFG (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        owner_address TEXT, metadata_url TEXT, image_url TEXT,
        Background TEXT, Back TEXT, Body TEXT, Clothing TEXT, Eyes TEXT,
        Eyebrows TEXT, Mouth TEXT, Hat TEXT, Accessory TEXT,
        body_type TEXT, network TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    )
    # Legacy shape: no revived_at column — exercises the self-migration.
    c.execute(
        """CREATE TABLE burned_nfts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nft_number INTEGER, nft_id TEXT, discord_id TEXT,
        burned_by TEXT, reason TEXT,
        burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        original_mint_time TIMESTAMP)"""
    )
    rarity.ensure_schema(c)
    c.commit()
    return c


def _mint_row(c, edition, clothing="Ring Dress"):
    c.execute(
        """INSERT INTO LFG (nft_number, nft_id, discord_id, Background, Body,
           Clothing, Eyes, Eyebrows, Mouth, Hat, Accessory, body_type, network)
           VALUES (?, ?, 'd1', 'Blue', 'Straight Blue', ?, 'Open', 'Flat',
                   'Smile', 'None', 'None', 'female', ?)""",
        (edition, f"NFT{edition}", clothing, NET),
    )
    c.commit()


def _clothing_count(c, value="Ring Dress"):
    (n,) = c.execute(
        "SELECT COALESCE(SUM(live_count),0) FROM trait_rarity"
        " WHERE network=? AND category='Clothing' AND trait=?",
        (NET, value),
    ).fetchone()
    return n


# --- helper-level tests ------------------------------------------------------


def test_recalc_excludes_harvest_burn_and_migrates_legacy_schema(tmp_path):
    c = _app_db(tmp_path / "app.db")
    _mint_row(c, 7)
    _mint_row(c, 8)
    rarity.recalculate_rarity(c, network=NET)
    assert _clothing_count(c) == 2

    db_helpers.record_harvest_burn(c, 7, "NFT7", "rUser")
    rarity.recalculate_rarity(c, network=NET)
    assert _clothing_count(c) == 1
    # migration added revived_at to the legacy table
    cols = {r[1] for r in c.execute("PRAGMA table_info(burned_nfts)")}
    assert "revived_at" in cols
    # audit fields captured from the LFG row
    row = c.execute(
        "SELECT discord_id, burned_by, reason FROM burned_nfts WHERE nft_number=7"
    ).fetchone()
    assert row == ("d1", "rUser", "harvest")


def test_revive_restores_live_count_and_keeps_audit_row(tmp_path):
    c = _app_db(tmp_path / "app.db")
    _mint_row(c, 7)
    db_helpers.record_harvest_burn(c, 7, "NFT7", "rUser")
    rarity.recalculate_rarity(c, network=NET)
    assert _clothing_count(c) == 0

    assert db_helpers.revive_harvested_edition(c, 7) is True
    rarity.recalculate_rarity(c, network=NET)
    assert _clothing_count(c) == 1
    # the burn row still exists (audit), just stamped
    (revived,) = c.execute("SELECT revived_at FROM burned_nfts WHERE nft_number=7").fetchone()
    assert revived is not None
    # nothing left to revive
    assert db_helpers.revive_harvested_edition(c, 7) is False


def test_revive_ignores_admin_burns(tmp_path):
    c = _app_db(tmp_path / "app.db")
    _mint_row(c, 9)
    c.execute(
        "INSERT INTO burned_nfts (nft_number, nft_id, burned_by, reason)"
        " VALUES (9, 'NFT9', 'admin', 'violation')"
    )
    c.commit()
    assert db_helpers.revive_harvested_edition(c, 9) is False
    rarity.recalculate_rarity(c, network=NET)
    assert _clothing_count(c) == 0  # admin burn stays excluded


def test_shop_price_rises_after_harvest(tmp_path):
    """End goal: the shop quote for a trait goes UP once a character wearing
    it is harvested out of the live population."""
    c = _app_db(tmp_path / "app.db")
    for ed in range(1, 4):
        _mint_row(c, ed, clothing="Ring Dress")
    for ed in range(4, 20):
        _mint_row(c, ed, clothing="Hoodie")
    rarity.recalculate_rarity(c, network=NET)
    before = shop.quote(c, NET, "Clothing", "Ring Dress")

    db_helpers.record_harvest_burn(c, 1, "NFT1", "rUser")
    rarity.recalculate_rarity(c, network=NET)
    after = shop.quote(c, NET, "Clothing", "Ring Dress")
    assert before is not None and after is not None
    assert after > before


# --- flow-level tests: run_harvest / run_assemble wire the bookkeeping ------


def _char(edition=7, body="Straight Blue"):
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


def _econ_conn(edition=7, body="Straight Blue"):
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    genesis = te.Genesis(
        trait_counts={(s, "None"): 1 for s in NON_BODY},
        edition_bodies={edition: (body, "male")},
    )
    es.freeze_genesis(c, genesis, {})
    return c


class _Fakes:
    async def closet_upload(self, meta):
        return "https://cdn/b/1.json"

    async def closet_mint(self, url):
        return "CLOSET0"

    async def closet_offer(self, nft_id, owner):
        return "OFFER0"

    async def closet_accept(self, offer_id):
        return {"url": "x", "qr": "y", "uuid": "z"}

    async def closet_modify(self, nft_id, owner, url):
        return "TXHASH"

    async def closet_exists(self, nft_id):
        return True

    async def closet_owner(self, nft_id):
        return "rUser"

    async def char_compose(self, attrs, body_class, edition, *_extra):
        return ("https://cdn/i.png", "https://cdn/m.json", None)

    async def char_mint(self, url):
        return "NEWNFT"

    async def char_modify(self, nft_id, owner, url):
        return "TXHASH"

    async def char_burn(self, nft_id, owner):
        return "BURNHASH"

    async def char_offer(self, nft_id, owner):
        return "OFFER1"

    async def char_accept(self, offer_id):
        return {"url": "x", "qr": "y", "uuid": "z"}


def _deps(conn, records_dir, app_db_file):
    f = _Fakes()
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
        app_conn_factory=lambda: sqlite3.connect(app_db_file),
        records_dir=str(records_dir),
    )


def test_run_harvest_records_burn_and_recounts(tmp_path):
    app_db_file = str(tmp_path / "app.db")
    app = _app_db(app_db_file)
    _mint_row(app, 7)
    rarity.recalculate_rarity(app, network=NET)
    assert _clothing_count(app) == 1

    conn = _econ_conn()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, tmp_path, app_db_file)))

    assert session.state == ef.DONE
    assert _clothing_count(app) == 0
    row = app.execute("SELECT reason, revived_at FROM burned_nfts WHERE nft_number=7").fetchone()
    assert row == ("harvest", None)


def test_run_assemble_revives_edition(tmp_path):
    app_db_file = str(tmp_path / "app.db")
    app = _app_db(app_db_file)
    _mint_row(app, 7)
    db_helpers.record_harvest_burn(app, 7, "NFT7", "rUser")
    rarity.recalculate_rarity(app, network=NET)
    assert _clothing_count(app) == 0

    conn = _econ_conn()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    # Closet holds the body + a full asset set, mirroring a completed harvest.
    es.set_closet_contents(conn, "rUser", [(s, "None", 1) for s in NON_BODY], [7])
    session = ef.AssembleSession(
        owner="rUser",
        edition=7,
        chosen=dict.fromkeys(NON_BODY, "None"),
        body_value="Straight Blue",
        body_class="male",
        live_editions=set(),
    )
    _run(ef.run_assemble(session, _deps(conn, tmp_path, app_db_file)))

    assert session.state == ef.DONE, session.error
    (revived,) = app.execute("SELECT revived_at FROM burned_nfts WHERE nft_number=7").fetchone()
    assert revived is not None
    rarity.recalculate_rarity(app, network=NET)
    assert _clothing_count(app) == 1


def test_harvest_survives_rarity_bookkeeping_failure(tmp_path):
    """The app-DB hook is best-effort: a broken factory must not fail the op."""

    def _boom():
        raise RuntimeError("app db unavailable")

    conn = _econ_conn()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    deps = _deps(conn, tmp_path, ":memory:")
    deps.app_conn_factory = _boom
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, deps))
    assert session.state == ef.DONE
