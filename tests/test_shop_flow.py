# tests/test_shop_flow.py — Task 6: ShopBuySession flow (#217). All-fake
# deps, no network. Env-guard preamble copied verbatim from
# tests/test_market_flow.py / tests/test_server_identity_wiring.py so
# importing lfg_core.config doesn't strand frozen constants when this file
# runs inside the full suite.
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

from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import (
    config,  # noqa: E402
    rarity,  # noqa: E402
    shop_flow,  # noqa: E402
    shop_store,  # noqa: E402
)
from lfg_core import economy_flow as ef  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402

BUYER = "rBUYER111111111111111111111"
OTHER = "rSOMEONEELSE1111111111111111"


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


class _F:
    """Fake ShopDeps callables; records calls for assertion."""

    def __init__(
        self,
        *,
        mint_fails: bool = False,
        offer_fails: bool = False,
        deposit_fails: bool = False,
    ) -> None:
        self.mint_fails = mint_fails
        self.offer_fails = offer_fails
        self.deposit_fails = deposit_fails
        self.mints: list[tuple[str, int, int, str, str]] = []
        self.offers: list[tuple[str, str, dict, int, str, str]] = []
        self.burns: list[tuple[str, str]] = []
        self.accepts: list[str] = []
        self.deposits: list[tuple[str, str]] = []
        self.minted_nft_id = "TRAIT_SHOP_0001"
        self.owner_of: dict[str, str] = {}

    async def trait_compose(self, slot: str, value: str) -> str:
        return f"https://cdn/{slot}/{value}.png"

    async def trait_upload(self, meta: dict) -> str:
        return "https://cdn/meta.json"

    async def mint(
        self, url: str, taxon: int, *, flags: int, action: str, platform: str
    ) -> str | None:
        self.mints.append((url, taxon, flags, action, platform))
        if self.mint_fails:
            return None
        self.owner_of[self.minted_nft_id] = config.SWAP_ISSUER_ADDRESS
        return self.minted_nft_id

    async def offer(
        self,
        nft_id: str,
        destination: str,
        *,
        amount: dict,
        expiration: int,
        platform: str,
        action: str,
    ) -> str | None:
        self.offers.append((nft_id, destination, amount, expiration, platform, action))
        if self.offer_fails:
            return None
        return "OFFER_INDEX_ABC"

    async def burn(self, nft_id: str, owner: str) -> str | None:
        self.burns.append((nft_id, owner))
        self.owner_of.pop(nft_id, None)
        return "BURN_HASH"

    async def payload_status(self, uuid: str):
        raise NotImplementedError  # overridden per-test

    async def accept_payload(self, offer_index: str, *, user_token: str | None = None) -> dict:
        self.accepts.append(offer_index)
        return {
            "qr_url": "https://xumm/q.png",
            "deep_link": "xumm://x",
            "uuid": "PAYLOAD-UUID",
            "pushed": False,
        }

    # run_deposit's EconomyDeps trait_* callables, resolving against the
    # buyer once the shop offer has (fictionally) been accepted.
    async def trait_info(self, nft_id: str) -> dict | None:
        return {
            "taxon": config.TRAIT_TAXON,
            "issuer": config.SWAP_ISSUER_ADDRESS,
            "owner": self.owner_of.get(nft_id),
        }

    async def trait_meta(self, nft_id: str) -> dict:
        return {"lfg_trait": {"slot": "Hat", "value": "Wizard Hat"}}

    async def trait_burn(self, nft_id: str, owner: str) -> str | None:
        self.deposits.append((nft_id, owner))
        if self.deposit_fails:
            raise RuntimeError("deposit boom")
        self.owner_of.pop(nft_id, None)
        return "DEPOSIT_BURN_HASH"

    async def closet_modify(self, nft_id: str, owner: str, url: str) -> str | None:
        return "MOD"

    async def closet_upload(self, meta: dict) -> str:
        return "https://cdn/c.json"

    async def closet_owner(self, nft_id: str) -> str:
        return BUYER


def _economy_deps(f: _F, tmp) -> ef.EconomyDeps:
    return ef.EconomyDeps(
        conn=f.conn,  # type: ignore[attr-defined]
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


def _deps(conn, f: _F, tmp, *, now_ts: int = 1_752_000_000, network: str = "testnet"):
    f.conn = conn
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    return shop_flow.ShopDeps(
        conn=conn,
        # Fresh connection per call, mirroring production's
        # sqlite3.connect(db_path.app_db_path(network)) — shop_flow now
        # closes this connection after use, so it must not be the shared
        # `conn` the rest of the test still reads from.
        app_conn_factory=lambda: sqlite3.connect(db_path),
        economy_deps=_economy_deps(f, tmp),
        mint_fn=f.mint,
        offer_fn=f.offer,
        burn_fn=f.burn,
        payload_status_fn=lambda u: f.payload_status(u),
        accept_payload_fn=f.accept_payload,
        now_ts_fn=lambda: now_ts,
        network=network,
    )


def _session(**overrides) -> shop_flow.ShopBuySession:
    base = {"buyer": BUYER, "slot": "Hat", "value": "Wizard Hat", "price_brix": 25}
    base.update(overrides)
    return shop_flow.ShopBuySession(**base)


def _signed_status(*, account: str):
    async def fake(_uuid):
        return {"opened": True, "signed": True, "account": account}

    return fake


def test_happy_path_start_then_settle(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))

    assert session.state == "awaiting_accept"
    assert session.nft_id == f.minted_nft_id
    assert session.offer_index == "OFFER_INDEX_ABC"
    assert session.accept is not None and session.accept["uuid"] == "PAYLOAD-UUID"
    assert len(f.offers) == 1
    assert f.offers[0][4] == "discord-activity"  # platform threaded into the offer memo

    order = shop_store.get_order(conn, session.id)
    assert order is not None
    assert order["status"] == "pending_accept"
    assert order["nft_id"] == f.minted_nft_id
    assert order["offer_index"] == "OFFER_INDEX_ABC"

    # Buyer accepted the offer on-ledger -> owns the trait token now.
    f.owner_of[f.minted_nft_id] = BUYER
    f.payload_status = _signed_status(account=BUYER)

    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == "done"
    assert f.deposits == [(f.minted_nft_id, BUYER)]
    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "settled"

    row = conn.execute(
        "SELECT shop_count FROM trait_rarity WHERE network=? AND category=? AND trait=?",
        (deps.network, "Hat", "Wizard Hat"),
    ).fetchone()
    assert row is not None and row[0] == 1


def test_settle_order_marked_settled_even_if_shop_count_increment_raises(tmp_path, monkeypatch):
    """Bot review finding (#217): the order status write must land before the
    best-effort shop_count increment, so an increment failure never leaves a
    completed purchase as a ghost 'accepted'/'failed' order."""
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))
    f.owner_of[f.minted_nft_id] = BUYER
    f.payload_status = _signed_status(account=BUYER)

    def raising_increment(*a, **kw):
        raise RuntimeError("increment boom")

    monkeypatch.setattr(shop_flow.rarity, "increment_shop_count", raising_increment)

    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == "done"
    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "settled"


def test_mint_failure_no_supply_row(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F(mint_fails=True)
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))

    assert session.state == "failed"
    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "failed"

    from lfg_core import economy_store as es_mod

    assert es_mod.read_supply_changes(conn) == []


def test_offer_failure_after_mint_reverts_with_burn_and_two_supply_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F(offer_fails=True)
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))

    assert session.state == "failed"
    assert f.burns == [(f.minted_nft_id, "")] or f.burns[0][0] == f.minted_nft_id

    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "failed"

    from lfg_core import economy_store as es_mod

    changes = es_mod.read_supply_changes(conn)
    assert len(changes) == 2
    assert changes[0]["kind"] == "mint"
    assert changes[0]["trait_deltas"] == {"Hat|Wizard Hat": 1}
    assert changes[1]["kind"] == "burn"
    assert changes[1]["trait_deltas"] == {"Hat|Wizard Hat": -1}


def test_signer_mismatch_leaves_order_pending_accept(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))
    assert session.state == "awaiting_accept"

    f.payload_status = _signed_status(account=OTHER)
    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == "failed"
    assert session.error == "signer_mismatch"
    assert f.deposits == []
    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "pending_accept"


def test_settle_failure_leaves_order_accepted_and_session_settling(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F(deposit_fails=True)
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))
    f.owner_of[f.minted_nft_id] = BUYER
    f.payload_status = _signed_status(account=BUYER)

    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == "settling"
    assert session.state != "failed"
    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "accepted"

    rarity.ensure_schema(conn)
    row = conn.execute(
        "SELECT shop_count FROM trait_rarity WHERE network=? AND category=? AND trait=?",
        (deps.network, "Hat", "Wizard Hat"),
    ).fetchone()
    assert row is None or row[0] == 0


def test_advance_noop_on_none_status(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))
    assert session.state == "awaiting_accept"

    async def fake_none(_uuid):
        return None

    f.payload_status = fake_none
    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == "awaiting_accept"
    assert f.deposits == []
    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "pending_accept"


def test_advance_idempotent_after_done(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))
    f.owner_of[f.minted_nft_id] = BUYER
    f.payload_status = _signed_status(account=BUYER)
    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == "done"
    assert f.deposits == [(f.minted_nft_id, BUYER)]

    # Re-advancing a terminal session must be a no-op.
    _run(shop_flow.advance_shop_buy(session, deps))

    assert session.state == "done"
    assert f.deposits == [(f.minted_nft_id, BUYER)]
    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "settled"


def test_offer_fn_raises_reverts_with_burn_and_two_supply_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()

    async def raising_offer(*a, **kw):
        f.offers.append((a, kw))
        raise RuntimeError("offer boom")

    f.offer = raising_offer
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))

    assert session.state == "failed"
    assert f.burns and f.burns[0][0] == f.minted_nft_id

    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "failed"

    from lfg_core import economy_store as es_mod

    changes = es_mod.read_supply_changes(conn)
    assert len(changes) == 2
    assert changes[0]["kind"] == "mint"
    assert changes[1]["kind"] == "burn"
    assert changes[1]["trait_deltas"] == {"Hat|Wizard Hat": -1}


def test_offer_fn_and_burn_fn_both_raise_leaves_admin_intervention(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "economy.db"))
    _active_closet(conn)
    f = _F()

    async def raising_offer(*a, **kw):
        raise RuntimeError("offer boom")

    async def raising_burn(*a, **kw):
        f.burns.append(a)
        raise RuntimeError("burn boom")

    f.offer = raising_offer
    f.burn = raising_burn
    deps = _deps(conn, f, tmp_path)
    session = _session()

    _run(shop_flow.start_shop_buy(session, deps))

    assert session.state == "failed"
    assert session.error is not None and "admin" in session.error.lower()

    order = shop_store.get_order(conn, session.id)
    assert order is not None and order["status"] == "failed"

    from lfg_core import economy_store as es_mod

    changes = es_mod.read_supply_changes(conn)
    assert len(changes) == 1
    assert changes[0]["kind"] == "mint"


def test_ripple_expiration():
    assert shop_flow.ripple_expiration(1_752_000_000, 900) == 1_752_000_900 - 946_684_800


def test_brix_amount_shape():
    amt = shop_flow.brix_amount(25)
    assert amt == {
        "currency": config.TOKEN_CURRENCY_HEX,
        "issuer": config.TOKEN_ISSUER_ADDRESS,
        "value": "25",
    }
