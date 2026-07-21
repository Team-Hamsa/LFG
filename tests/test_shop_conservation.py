# tests/test_shop_conservation.py — Task 12: conservation audit over the
# Trait Shop (#217). Drives a full fake happy-path purchase (mirrors
# tests/test_shop_flow.py's _deps helper) and a full fake expiry sweep
# (mirrors tests/test_shop_sweep.py's harness), then asserts the SAME
# conservation checker `scripts/audit_trait_economy.py` uses in production
# (lfg_core.trait_economy.verify_conservation) reports zero drift for both.
#
# There are no live characters in either fixture (the shop only ever mints
# standalone trait tokens), so genesis is the empty baseline -- Genesis({}, {})
# -- and the moving conservation target is entirely the supply_changes ledger
# that shop_flow/app.sweep_shop_orders record on every mint/burn. That keeps
# this test wired to the REAL checker function rather than reimplementing its
# arithmetic.
#
# Env-guard preamble copied verbatim from tests/test_shop_flow.py /
# tests/test_shop_sweep.py so importing lfg_core.config doesn't strand frozen
# constants when this file runs inside the full suite.
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

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402

from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config, shop_flow, shop_store, trait_economy  # noqa: E402
from lfg_core import economy_flow as ef  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
from lfg_service import app as server  # noqa: E402

BUYER = "rBuyerAddress000000000000000000000"
TRAIT1 = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C7000000a1"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _active_closet(conn, owner=BUYER):
    es.init_economy_schema(conn)
    es.set_closet_token(conn, owner, "CLOSET", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, owner, [], [])


def _empty_genesis() -> trait_economy.Genesis:
    # The shop never touches live characters -- only standalone trait tokens
    # and Closet contents -- so the frozen baseline is empty and the entire
    # conservation target comes from the supply_changes ledger.
    return trait_economy.Genesis(trait_counts={}, edition_bodies={})


def _assert_zero_drift(conn) -> None:
    genesis = _empty_genesis()
    supply_changes = es.read_supply_changes(conn)
    census = trait_economy.asset_census(
        {},  # no live characters
        es.read_closet_assets(conn),
        es.read_trait_tokens(conn),
    )
    report = trait_economy.verify_conservation(genesis, census, supply_changes)
    assert report.ok, f"conservation drift: trait={report.trait_drift}"


# ---------------------------------------------------------------------------
# Happy path: T6's full ShopBuySession start + advance to settlement.
# ---------------------------------------------------------------------------


class _F:
    """Fake ShopDeps callables (mirrors tests/test_shop_flow.py's _F)."""

    def __init__(self) -> None:
        self.minted_nft_id = "TRAIT_SHOP_0001"
        self.owner_of: dict[str, str] = {}
        self.conn: sqlite3.Connection | None = None
        self.deposits: list[tuple[str, str]] = []

    async def trait_compose(self, slot: str, value: str) -> str:
        return f"https://cdn/{slot}/{value}.png"

    async def trait_upload(self, meta: dict) -> str:
        return "https://cdn/meta.json"

    async def mint(self, url, taxon, *, flags, action, platform):
        self.owner_of[self.minted_nft_id] = config.SWAP_ISSUER_ADDRESS
        return self.minted_nft_id

    async def offer(self, nft_id, destination, *, amount, expiration, platform, action):
        return "OFFER_INDEX_ABC"

    async def burn(self, nft_id, owner):
        self.owner_of.pop(nft_id, None)
        return "BURN_HASH"

    async def payload_status(self, uuid):
        return {"opened": True, "signed": True, "account": BUYER}

    async def accept_payload(self, offer_index, *, user_token=None):
        return {"qr_url": "x", "deep_link": "x", "uuid": "PAYLOAD-UUID", "pushed": False}

    async def trait_info(self, nft_id):
        return {
            "taxon": config.TRAIT_TAXON,
            "issuer": config.SWAP_ISSUER_ADDRESS,
            "owner": self.owner_of.get(nft_id),
        }

    async def trait_meta(self, nft_id):
        return {"lfg_trait": {"slot": "Hat", "value": "Wizard Hat"}}

    async def trait_burn(self, nft_id, owner):
        self.deposits.append((nft_id, owner))
        self.owner_of.pop(nft_id, None)
        return "DEPOSIT_BURN_HASH"

    async def closet_modify(self, nft_id, owner, url):
        return "MOD"

    async def closet_upload(self, meta):
        return "https://cdn/c.json"

    async def closet_owner(self, nft_id):
        return BUYER


def _economy_deps(f: _F, tmp) -> ef.EconomyDeps:
    return ef.EconomyDeps(
        conn=f.conn,  # type: ignore[arg-type]
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=None,
        closet_offer_fn=None,
        closet_accept_fn=None,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=None,
        char_mint_fn=None,
        char_modify_fn=None,
        char_burn_fn=None,
        char_offer_fn=None,
        char_accept_fn=None,
        closet_owner_fn=f.closet_owner,
        trait_compose_fn=f.trait_compose,
        trait_upload_fn=f.trait_upload,
        trait_mint_fn=None,
        trait_burn_fn=f.trait_burn,
        trait_info_fn=f.trait_info,
        trait_meta_fn=f.trait_meta,
        records_dir=str(tmp),
    )


def _shop_deps(conn, f: _F, tmp) -> shop_flow.ShopDeps:
    f.conn = conn
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    return shop_flow.ShopDeps(
        conn=conn,
        # Fresh connection per call (shop_flow closes it after use).
        app_conn_factory=lambda: sqlite3.connect(db_path),
        economy_deps=_economy_deps(f, tmp),
        mint_fn=f.mint,
        offer_fn=f.offer,
        burn_fn=f.burn,
        payload_status_fn=f.payload_status,
        accept_payload_fn=f.accept_payload,
        now_ts_fn=lambda: 1_752_000_000,
        network="testnet",
    )


def test_happy_path_purchase_has_zero_conservation_drift(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()
    deps = _shop_deps(conn, f, tmp_path)
    session = shop_flow.ShopBuySession(buyer=BUYER, slot="Hat", value="Wizard Hat", price_brix=25)

    _run(shop_flow.start_shop_buy(session, deps))
    assert session.state == shop_flow.AWAITING_ACCEPT

    f.owner_of[f.minted_nft_id] = BUYER
    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == shop_flow.DONE
    assert f.deposits == [(f.minted_nft_id, BUYER)]

    # The trait was minted (+1 supply), then deposited back into the Closet
    # (burn off-ledger for our purposes, but the deposit path does NOT touch
    # supply_changes -- see economy_flow.run_deposit's supply-neutral docstring)
    # so the asset now lives in closet_assets instead of trait_tokens. Either
    # way it must still equal genesis + supply_changes.
    _assert_zero_drift(conn)


# ---------------------------------------------------------------------------
# Expiry: T7's sweep_shop_orders() over a stale pending_accept order.
# ---------------------------------------------------------------------------


def _init_onchain(path):
    conn = init_onchain_db(path)
    es.init_economy_schema(conn)
    shop_store.ensure_schema(conn)
    conn.commit()
    return conn


def _reopen(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_order(conn, session_id, *, status, created_ts):
    shop_store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO shop_orders (session_id, buyer, slot, value, price_brix,"
        " nft_id, offer_index, status, created_ts, updated_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            BUYER,
            "Hat",
            "Wizard Hat",
            100,
            TRAIT1,
            "OFFER1",
            status,
            created_ts,
            created_ts,
        ),
    )
    conn.commit()


def test_expiry_sweep_has_zero_conservation_drift(tmp_path, monkeypatch):
    onchain_path = str(tmp_path / "onchain_testnet.db")
    conn = _init_onchain(onchain_path)
    conn.commit()
    conn.close()
    app_db = str(tmp_path / "lfg_nfts_testnet.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    monkeypatch.setattr(server.db_path, "app_db_path", lambda net=None: app_db)
    server._shop_settle_attempts.clear()

    conn = _reopen(onchain_path)
    old_ts = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS - 60
    _seed_order(conn, "S1", status="pending_accept", created_ts=old_ts)
    conn.close()

    async def fake_cancel(offer_index, **kw):
        return "CANCELHASH"

    async def fake_burn(nft_id, *a, **kw):
        return "BURNHASH"

    monkeypatch.setattr(server.xrpl_ops, "cancel_nft_offer", fake_cancel)
    monkeypatch.setattr(server.xrpl_ops, "burn_nft", fake_burn)

    _run(server.sweep_shop_orders())

    conn = _reopen(onchain_path)
    order = shop_store.get_order(conn, "S1")
    assert order["status"] == "expired"

    # The mint that seeded this order was never itself journaled by this test
    # (it stands in for a real T6 mint that happened before expiry), so seed
    # the matching +1 mint row now to reconstruct what production state would
    # look like at the moment of expiry, then confirm the -1 burn the sweep
    # just wrote brings the ledger back to zero drift.
    es.record_supply_change(
        conn,
        kind="mint",
        edition=None,
        body_value="",
        body_class="",
        trait_deltas={"Hat|Wizard Hat": 1},
        actor="shop",
        reason="shop purchase S1",
    )
    conn.commit()

    _assert_zero_drift(conn)

    server._shop_settle_attempts.clear()
