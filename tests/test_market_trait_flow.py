# tests/test_market_trait_flow.py
# Task 9: the trait sell wizard (Extract -> List, one action, two Xaman
# signatures) and trait-sale settlement (burn sold trait -> buyer's Closet),
# per spec §Q7.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them. (Copy the block verbatim from
# tests/test_server_identity_wiring.py — same keys/values.)
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
import json  # noqa: E402
import sqlite3  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import (
    config,  # noqa: E402
    market_flow,  # noqa: E402
)
from lfg_core import economy_flow as ef  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_core.market_store import (  # noqa: E402
    MarketListing,
    close_listing,  # noqa: E402
    unsettled_trait_sales,  # noqa: E402
    upsert_listing,
)
from lfg_core.market_store import get_listing as market_get_listing  # noqa: E402
from lfg_core.market_store import init_db as init_market_db  # noqa: E402
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
from lfg_service import app as server  # noqa: E402
from webapp import mock_economy  # noqa: E402

SELLER = "rSellerAddress0000000000000000000"
BUYER = "rBuyerAddress000000000000000000000"
TRAIT1 = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C7000000a1"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mocked_request(method, path):
    return make_mocked_request(method, path, app=web.Application())


async def _read_json(resp):
    return json.loads(resp.body.decode())


def _init_onchain(path):
    conn = init_onchain_db(path)
    es.init_economy_schema(conn)
    init_market_db(conn)
    conn.commit()
    return conn


@pytest.fixture
def onchain_env(tmp_path, monkeypatch):
    onchain_path = str(tmp_path / "onchain_testnet.db")
    conn = _init_onchain(onchain_path)
    conn.commit()
    conn.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()
    server._sweep_attempts.clear()
    yield onchain_path
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()
    server._sweep_attempts.clear()


def _reopen(onchain_path):
    conn = sqlite3.connect(onchain_path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def market_wallet(monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", SELLER)
    # WEBAPP_DEV_MODE=True above is only for require_wallet's dev-mode wallet
    # injection (request["wallet"] = mock_economy.DEV_OWNER); these tests
    # exercise the REAL market handler logic (Task 10 added a mock-market
    # substitution gated on the same flag — see app._use_market_mock's
    # docstring), so pin that substitution off independently.
    monkeypatch.setattr(server, "_use_market_mock", lambda: False)
    server.market_sessions.clear()
    yield
    server.market_sessions.clear()


def _post_request(path, body):
    req = _mocked_request("POST", path)

    async def json_body():
        return body

    req.json = json_body
    return req


def _fake_payload(qr="https://list-qr", url="https://xumm.app/sign/LIST", pl_uuid="LIST-UUID"):
    async def fake(*args, **kwargs):
        return {"qr_url": qr, "xumm_url": url, "uuid": pl_uuid}

    return fake


def _fake_status(*, signed, expired=False, txid=None):
    async def fake(_uuid):
        return {"opened": True, "signed": signed, "expired": expired, "txid": txid}

    return fake


def _sell_offer_meta(nft_id, offer_index, amount_drops):
    return {
        "TransactionResult": "tesSUCCESS",
        "AffectedNodes": [
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenOffer",
                    "LedgerIndex": offer_index,
                    "NewFields": {
                        "NFTokenID": nft_id,
                        "Amount": str(amount_drops),
                        "Flags": 1,
                    },
                }
            }
        ],
    }


# ---------------------------------------------------------------------------
# market_flow.advance_trait_sell_session (pure state machine)
# ---------------------------------------------------------------------------


@dataclass
class _FakeExtract:
    """Duck-typed stand-in for economy_flow.ExtractSession — advance_trait_sell_session
    reads only .state/.error/.nft_id/.accept."""

    state: str = "running"
    error: str | None = None
    nft_id: str | None = None
    accept: dict[str, Any] | None = None


def _wizard_session(extract, **overrides):
    base = {
        "discord_id": "dev",
        "wallet_address": SELLER,
        "slot": "Hat",
        "value": "Wizard Hat",
        "amount_drops": 500_000,
        "extract_session": extract,
    }
    base.update(overrides)
    return market_flow.TraitSellSession(**base)


class TestAdvanceTraitSellSession:
    def test_extract_running_stays_pending(self):
        s = _wizard_session(_FakeExtract(state="running"))
        row = _run(market_flow.advance_trait_sell_session(s))
        assert row is None
        assert s.state == market_flow.EXTRACT_PENDING

    def test_extract_failed_fails_wizard_no_listing(self):
        s = _wizard_session(_FakeExtract(state="failed", error="no loose Hat in your Closet"))
        row = _run(market_flow.advance_trait_sell_session(s))
        assert row is None
        assert s.state == market_flow.FAILED
        assert s.error == "no loose Hat in your Closet"
        assert s.nft_id is None

    def test_extract_done_exposes_extract_qr_and_waits_for_signature(self):
        extract = _FakeExtract(
            state="done",
            nft_id=TRAIT1,
            accept={
                "qr_url": "https://extract-qr",
                "xumm_url": "https://xumm.app/sign/E1",
                "uuid": "E1",
            },
        )
        s = _wizard_session(extract)
        row = _run(market_flow.advance_trait_sell_session(s))
        assert row is None
        assert s.state == market_flow.EXTRACT_DONE
        assert s.nft_id == TRAIT1
        assert s.extract_qr_url == "https://extract-qr"
        assert s.extract_xumm_url == "https://xumm.app/sign/E1"

        # Not yet signed -> stays extract_done, no listing payload created.
        async def boom(*a, **k):
            raise AssertionError("must not create a list payload before signature 1")

        row2 = _run(
            market_flow.advance_trait_sell_session(
                s,
                get_payload_status=_fake_status(signed=False),
                create_sell_offer_payload=boom,
            )
        )
        assert row2 is None
        assert s.state == market_flow.EXTRACT_DONE

    def test_extract_signature_expired_fails_wizard(self):
        extract = _FakeExtract(
            state="done", nft_id=TRAIT1, accept={"qr_url": "q", "xumm_url": "x", "uuid": "E1"}
        )
        s = _wizard_session(extract)
        _run(market_flow.advance_trait_sell_session(s))  # -> EXTRACT_DONE
        row = _run(
            market_flow.advance_trait_sell_session(
                s, get_payload_status=_fake_status(signed=False, expired=True)
            )
        )
        assert row is None
        assert s.state == market_flow.FAILED

    def test_extract_signed_creates_list_payload_and_moves_to_list_pending(self):
        extract = _FakeExtract(
            state="done", nft_id=TRAIT1, accept={"qr_url": "q", "xumm_url": "x", "uuid": "E1"}
        )
        s = _wizard_session(extract)
        _run(market_flow.advance_trait_sell_session(s))  # -> EXTRACT_DONE

        calls = []

        async def fake_create_sell(account, nft_id, drops, **kwargs):
            calls.append((account, nft_id, drops))
            return {
                "qr_url": "https://list-qr",
                "xumm_url": "https://xumm.app/sign/L1",
                "uuid": "L1",
            }

        row = _run(
            market_flow.advance_trait_sell_session(
                s,
                get_payload_status=_fake_status(signed=True),
                create_sell_offer_payload=fake_create_sell,
            )
        )
        assert row is None
        assert s.state == market_flow.LIST_PENDING
        assert s.list_qr_url == "https://list-qr"
        assert s.list_xumm_url == "https://xumm.app/sign/L1"
        assert calls == [(SELLER, TRAIT1, "500000")]

    def test_list_step_delegates_to_advance_list_session_and_writes_row_on_success(self):
        extract = _FakeExtract(
            state="done", nft_id=TRAIT1, accept={"qr_url": "q", "xumm_url": "x", "uuid": "E1"}
        )
        s = _wizard_session(extract)
        _run(market_flow.advance_trait_sell_session(s))
        _run(
            market_flow.advance_trait_sell_session(
                s,
                get_payload_status=_fake_status(signed=True),
                create_sell_offer_payload=_fake_payload(),
            )
        )
        assert s.state == market_flow.LIST_PENDING

        meta = _sell_offer_meta(TRAIT1, "B" * 64, 500_000)

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": meta}

        row = _run(
            market_flow.advance_trait_sell_session(
                s,
                get_payload_status=_fake_status(signed=True, txid="TXHASH"),
                get_tx=fake_get_tx,
            )
        )
        assert row is not None
        assert row["offer_index"] == "B" * 64
        assert row["nft_id"] == TRAIT1
        assert row["kind"] == "trait"
        assert row["slot"] == "Hat"
        assert row["value"] == "Wizard Hat"
        assert s.state == market_flow.LISTED
        assert s.offer_index == "B" * 64

    def test_list_step_failure_fails_wizard(self):
        extract = _FakeExtract(
            state="done", nft_id=TRAIT1, accept={"qr_url": "q", "xumm_url": "x", "uuid": "E1"}
        )
        s = _wizard_session(extract)
        _run(market_flow.advance_trait_sell_session(s))
        _run(
            market_flow.advance_trait_sell_session(
                s,
                get_payload_status=_fake_status(signed=True),
                create_sell_offer_payload=_fake_payload(),
            )
        )

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": {"TransactionResult": "tecNO_PERMISSION"}}

        row = _run(
            market_flow.advance_trait_sell_session(
                s,
                get_payload_status=_fake_status(signed=True, txid="TXHASH"),
                get_tx=fake_get_tx,
            )
        )
        assert row is None
        assert s.state == market_flow.FAILED

    def test_terminal_state_never_re_polls(self):
        s = _wizard_session(_FakeExtract(state="running"))
        s.state = market_flow.LISTED

        async def boom(_uuid):
            raise AssertionError("must not poll a terminal session")

        row = _run(market_flow.advance_trait_sell_session(s, get_payload_status=boom))
        assert row is None


# ---------------------------------------------------------------------------
# POST /api/market/trait/list (handle_market_trait_list_start)
# ---------------------------------------------------------------------------


@dataclass
class _FakeEconomyWebSession:
    inner: Any
    id: str = field(default_factory=lambda: "extract-session-id")


@pytest.mark.parametrize("bad_price", ["abc", "0", "-1", "Infinity", "nan"])
def test_trait_list_start_bad_price_400_no_extract_started(
    onchain_env, market_wallet, monkeypatch, bad_price
):
    async def boom(*a, **k):
        raise AssertionError("must not start an extract for a bad price")

    monkeypatch.setattr(server.economy_api, "start_extract", boom)
    req = _post_request(
        "/api/market/trait/list", {"slot": "Hat", "value": "Wizard Hat", "price_xrp": bad_price}
    )
    resp = _run(server.handle_market_trait_list_start(req))
    assert resp.status == 400
    assert len(server.market_sessions) == 0


def test_trait_list_start_success_returns_extract_pending_session(
    onchain_env, market_wallet, monkeypatch
):
    async def fake_start_extract(discord_id, owner, body):
        assert body == {"slot": "Hat", "value": "Wizard Hat"}
        return _FakeEconomyWebSession(inner=_FakeExtract(state="running"))

    monkeypatch.setattr(server.economy_api, "start_extract", fake_start_extract)
    req = _post_request(
        "/api/market/trait/list", {"slot": "Hat", "value": "Wizard Hat", "price_xrp": "5"}
    )
    resp = _run(server.handle_market_trait_list_start(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == "extract_pending"
    assert len(server.market_sessions) == 1


def test_trait_list_start_economy_error_400_no_session(onchain_env, market_wallet, monkeypatch):
    from webapp import economy_api

    async def fake_start_extract(discord_id, owner, body):
        raise economy_api.EconomyError("Create and claim your Closet first.")

    monkeypatch.setattr(server.economy_api, "start_extract", fake_start_extract)
    req = _post_request(
        "/api/market/trait/list", {"slot": "Hat", "value": "Wizard Hat", "price_xrp": "5"}
    )
    resp = _run(server.handle_market_trait_list_start(req))
    assert resp.status == 400
    assert len(server.market_sessions) == 0


def test_trait_list_start_disabled_economy_403(onchain_env, market_wallet, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)
    req = _post_request(
        "/api/market/trait/list", {"slot": "Hat", "value": "Wizard Hat", "price_xrp": "5"}
    )
    resp = _run(server.handle_market_trait_list_start(req))
    assert resp.status == 403


# ---------------------------------------------------------------------------
# GET /api/market/trait/list/{session_id} (handle_market_trait_list_status)
# ---------------------------------------------------------------------------


class _StatusReq:
    headers: dict = {}

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self._store = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _make_wizard_session(extract, **overrides):
    base = {
        "discord_id": "dev",
        "wallet_address": SELLER,
        "slot": "Hat",
        "value": "Wizard Hat",
        "amount_drops": 500_000,
        "extract_session": extract,
        "platform": "discord",
    }
    base.update(overrides)
    s = market_flow.TraitSellSession(**base)
    server.market_sessions[s.id] = s
    return s


def test_trait_list_status_not_found_404(onchain_env, market_wallet):
    resp = _run(server.handle_market_trait_list_status(_StatusReq("nope")))
    assert resp.status == 404


def test_trait_list_status_full_wizard_writes_listing_row(onchain_env, market_wallet, monkeypatch):
    extract = _FakeExtract(state="running")
    s = _make_wizard_session(extract)

    # Poll 1: still extracting.
    resp = _run(server.handle_market_trait_list_status(_StatusReq(s.id)))
    assert _run(_read_json(resp))["state"] == "extract_pending"

    # Extract completes.
    extract.state = "done"
    extract.nft_id = TRAIT1
    extract.accept = {"qr_url": "q", "xumm_url": "x", "uuid": "E1"}
    resp = _run(server.handle_market_trait_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "extract_done"
    assert body["nft_id"] == TRAIT1

    # Signature 1 confirmed -> list payload created (signature 2).
    monkeypatch.setattr(server.xumm_ops, "get_payload_status", _fake_status(signed=True))
    monkeypatch.setattr(server.xumm_ops, "create_sell_offer_payload", _fake_payload())
    resp = _run(server.handle_market_trait_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "list_pending"
    assert body["list_qr_url"] == "https://list-qr"

    # Signature 2 validated tesSUCCESS -> listed, row written to market_listings.
    meta = _sell_offer_meta(TRAIT1, "C" * 64, 500_000)
    monkeypatch.setattr(
        server.xumm_ops, "get_payload_status", _fake_status(signed=True, txid="TXHASH")
    )

    async def fake_get_tx(_hash):
        return {"validated": True, "meta": meta}

    monkeypatch.setattr(server.xrpl_ops, "get_tx", fake_get_tx)
    resp = _run(server.handle_market_trait_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "listed"
    assert body["offer_index"] == "C" * 64

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "C" * 64)
    assert row is not None
    assert row["kind"] == "trait"
    assert row["nft_id"] == TRAIT1
    assert row["slot"] == "Hat"
    assert row["value"] == "Wizard Hat"
    assert row["is_live"] == 1


def test_trait_list_status_extract_failure_leaves_no_listing(onchain_env, market_wallet):
    extract = _FakeExtract(state="failed", error="no loose Hat in your Closet")
    s = _make_wizard_session(extract)
    resp = _run(server.handle_market_trait_list_status(_StatusReq(s.id)))
    body = _run(_read_json(resp))
    assert body["state"] == "failed"
    assert body["error"] == "no loose Hat in your Closet"

    conn = _reopen(onchain_env)
    count = conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Recoverability: an extracted-but-never-listed token shows under /api/market/mine
# ---------------------------------------------------------------------------


def test_extracted_unlisted_trait_token_appears_under_mine(onchain_env, market_wallet):
    """spec §Q7: each wizard step is independently recoverable — a token that
    finished Extract (signature 1 accepted, so the listener already flipped
    trait_tokens.owner to the seller) but never reached List is a perfectly
    ordinary wallet trait token, and must surface under /api/market/mine's
    unlisted trait tokens (not stranded, not requiring the wizard to resume)."""
    conn = _reopen(onchain_env)
    es.upsert_trait_token(conn, TRAIT1, SELLER, "Hat", "Wizard Hat")
    conn.commit()
    conn.close()

    req = _mocked_request("GET", "/api/market/mine")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = SELLER
    resp = _run(server.handle_market_mine(req))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert {"nft_id": TRAIT1, "slot": "Hat", "value": "Wizard Hat"} in body["unlisted_trait_tokens"]
    assert body["listings"] == []


# ---------------------------------------------------------------------------
# Settlement: _settle_trait_sale (mocked EconomyDeps, real run_deposit)
# ---------------------------------------------------------------------------


@dataclass
class _SettleFakeDeps:
    fail_closet_sync: bool = False
    burn_ok: bool = True
    burns: list = field(default_factory=list)

    async def trait_info(self, nft_id):
        return {"taxon": config.TRAIT_TAXON, "issuer": config.SWAP_ISSUER_ADDRESS, "owner": BUYER}

    async def trait_meta(self, nft_id):
        return {"lfg_trait": {"slot": "Hat", "value": "Wizard Hat"}}

    async def trait_burn(self, nft_id, owner):
        self.burns.append((nft_id, owner))
        return "BURNHASH" if self.burn_ok else None

    async def closet_upload(self, meta):
        return "https://cdn/closet.json"

    async def closet_modify(self, nft_id, owner, url):
        return None if self.fail_closet_sync else "MODHASH"

    async def closet_offer(self, nft_id, owner):
        return "OFFER"

    async def closet_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def closet_owner(self, nft_id):
        return BUYER


def _settle_deps(conn, f, tmp_path):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=None,
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
        trait_burn_fn=f.trait_burn,
        trait_info_fn=f.trait_info,
        trait_meta_fn=f.trait_meta,
        records_dir=str(tmp_path),
    )


def _active_buyer_closet(conn, owner=BUYER):
    es.set_closet_token(conn, owner, "CLOSET", "AB", status="active", offer_id=None)
    es.set_closet_contents(conn, owner, [], [])


def test_settle_trait_sale_success_marks_settled(onchain_env, monkeypatch, tmp_path):
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    upsert_listing(
        conn,
        MarketListing(
            offer_index="D" * 64,
            nft_id=TRAIT1,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
            is_live=0,
            closed_reason="sold",
            settled=0,
        ),
    )
    conn.commit()
    conn.close()

    f = _SettleFakeDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    ok = _run(server._settle_trait_sale(BUYER, TRAIT1, "D" * 64, "testnet"))
    assert ok is True
    assert f.burns == [(TRAIT1, BUYER)]

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "D" * 64)
    assert row["settled"] == 1


def test_settle_trait_sale_failure_leaves_unsettled_and_journals(
    onchain_env, monkeypatch, tmp_path
):
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    upsert_listing(
        conn,
        MarketListing(
            offer_index="E" * 64,
            nft_id=TRAIT1,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
            is_live=0,
            closed_reason="sold",
            settled=0,
        ),
    )
    conn.commit()
    conn.close()

    f = _SettleFakeDeps(fail_closet_sync=True)  # burn succeeds, closet credit fails
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    ok = _run(server._settle_trait_sale(BUYER, TRAIT1, "E" * 64, "testnet"))
    assert ok is False
    assert f.burns == [(TRAIT1, BUYER)]  # burn DID happen

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "E" * 64)
    assert row["settled"] == 0  # stays pending

    # run_deposit's own journal exists (deposited_pending_closet), even though
    # _settle_trait_sale itself writes no journal of its own.
    records = list(tmp_path.glob("deposit-*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["status"] == "deposited_pending_closet"


def test_settle_trait_sale_no_active_closet_fails_cleanly(onchain_env, monkeypatch, tmp_path):
    conn = _reopen(onchain_env)
    # No Closet record at all for the buyer.
    upsert_listing(
        conn,
        MarketListing(
            offer_index="F" * 64,
            nft_id=TRAIT1,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
            is_live=0,
            closed_reason="sold",
            settled=0,
        ),
    )
    conn.commit()
    conn.close()

    f = _SettleFakeDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    ok = _run(server._settle_trait_sale(BUYER, TRAIT1, "F" * 64, "testnet"))
    assert ok is False
    assert f.burns == []  # no active Closet -> precondition fails before any burn

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "F" * 64)
    assert row["settled"] == 0


# ---------------------------------------------------------------------------
# settle_pending_trait_sales sweep: bounded retry
# ---------------------------------------------------------------------------


def _seed_unsettled_trait_sale(conn, offer_index, nft_id=TRAIT1, owner=BUYER):
    es.upsert_trait_token(conn, nft_id, owner, "Hat", "Wizard Hat")
    upsert_listing(
        conn,
        MarketListing(
            offer_index=offer_index,
            nft_id=nft_id,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
            is_live=0,
            closed_reason="sold",
            settled=0,
        ),
    )


def test_sweep_resolves_buyer_from_trait_tokens_and_settles(onchain_env, monkeypatch, tmp_path):
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    _seed_unsettled_trait_sale(conn, "G" * 64)
    conn.commit()
    conn.close()

    f = _SettleFakeDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    _run(server.settle_pending_trait_sales())
    assert f.burns == [(TRAIT1, BUYER)]

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "G" * 64)
    assert row["settled"] == 1
    assert unsettled_trait_sales(conn) == []


def test_sweep_resolves_buyer_from_persisted_row_when_trait_tokens_deleted(
    onchain_env, monkeypatch, tmp_path
):
    # run_deposit deletes the trait_tokens ownership row between the burn and
    # the Closet credit; if credit then fails, a later sweep must still resolve
    # the buyer from the durable `buyer` column persisted on the sold listing
    # (CodeRabbit #129 Major) — never from trait_tokens.owner, which is gone.
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    # Live listing, then close sold WITH a persisted buyer, but with NO
    # trait_tokens row for the token (deleted mid-settlement).
    upsert_listing(
        conn,
        MarketListing(
            offer_index="K" * 64,
            nft_id=TRAIT1,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
        ),
    )
    close_listing(conn, "K" * 64, "sold", buyer=BUYER)
    conn.commit()
    conn.close()

    f = _SettleFakeDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )

    _run(server.settle_pending_trait_sales())
    # The sweep resolved the buyer from the row (not trait_tokens) and settled.
    assert f.burns == [(TRAIT1, BUYER)]
    conn = _reopen(onchain_env)
    assert market_get_listing(conn, "K" * 64)["settled"] == 1


def test_sweep_skips_row_with_no_known_buyer_without_counting_attempt(onchain_env, monkeypatch):
    conn = _reopen(onchain_env)
    upsert_listing(
        conn,
        MarketListing(
            offer_index="H" * 64,
            nft_id=TRAIT1,
            kind="trait",
            seller=SELLER,
            amount_drops=500_000,
            slot="Hat",
            value="Wizard Hat",
            is_live=0,
            closed_reason="sold",
            settled=0,
        ),
    )
    conn.commit()
    conn.close()

    async def boom(*a, **k):
        raise AssertionError("must not attempt settlement without a resolvable buyer")

    monkeypatch.setattr(server, "_settle_trait_sale", boom)
    _run(server.settle_pending_trait_sales())
    assert server._sweep_attempts.get("H" * 64, 0) == 0


def test_sweep_gives_up_after_max_attempts_and_journals(onchain_env, monkeypatch, tmp_path):
    conn = _reopen(onchain_env)
    _seed_unsettled_trait_sale(conn, "I" * 64)  # buyer has NO active Closet -> always fails
    conn.commit()
    conn.close()

    f = _SettleFakeDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )
    monkeypatch.setattr(server.config, "ECONOMY_RECORDS_DIR", str(tmp_path))

    for _ in range(server._SWEEP_MAX_ATTEMPTS):
        _run(server.settle_pending_trait_sales())
    assert server._sweep_attempts["I" * 64] == server._SWEEP_MAX_ATTEMPTS

    giveup = tmp_path / f"trait-settlement-giveup-{'I' * 64}.json"
    assert giveup.exists()
    record = json.loads(giveup.read_text())
    assert record["status"] == "abandoned"
    assert record["buyer"] == BUYER

    conn = _reopen(onchain_env)
    row = market_get_listing(conn, "I" * 64)
    assert row["settled"] == 0  # never silently marked settled

    # One more sweep pass: no further attempts counted (row no longer retried).
    _run(server.settle_pending_trait_sales())
    assert server._sweep_attempts["I" * 64] == server._SWEEP_MAX_ATTEMPTS


def test_sweep_success_clears_prior_attempt_count(onchain_env, monkeypatch, tmp_path):
    conn = _reopen(onchain_env)
    _seed_unsettled_trait_sale(conn, "J" * 64)
    conn.commit()
    conn.close()
    server._sweep_attempts["J" * 64] = server._SWEEP_MAX_ATTEMPTS - 1

    f = _SettleFakeDeps()
    monkeypatch.setattr(
        server.economy_api, "build_settlement_deps", lambda c: _settle_deps(c, f, tmp_path)
    )
    conn = _reopen(onchain_env)
    _active_buyer_closet(conn)
    conn.commit()
    conn.close()

    _run(server.settle_pending_trait_sales())
    assert "J" * 64 not in server._sweep_attempts


# ---------------------------------------------------------------------------
# Startup registration: the sweep runs as an asyncio background task
# ---------------------------------------------------------------------------


def test_start_settlement_sweep_schedules_task_when_economy_enabled(monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", True)

    async def go():
        app: dict = {}
        await server._start_settlement_sweep(app)
        task = app.get("settlement_sweep_task")
        assert task is not None
        assert isinstance(task, asyncio.Task)
        assert not task.done()
        await server._stop_settlement_sweep(app)
        assert task.cancelled() or task.done()

    _run(go())


def test_start_settlement_sweep_skipped_when_economy_disabled(monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)

    async def go():
        app: dict = {}
        await server._start_settlement_sweep(app)
        assert "settlement_sweep_task" not in app
        await server._stop_settlement_sweep(app)  # no-op, must not raise

    _run(go())
