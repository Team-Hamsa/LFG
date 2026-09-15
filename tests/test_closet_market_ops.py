import asyncio
import importlib.util
import pathlib
import sqlite3
from decimal import Decimal

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import config, memos
from lfg_core import economy_store as es

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", ROOT / "scripts" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load("audit_closet_market")
setup = _load("closet_market_setup")
SELLER, BUYER = "rSeller", "rBuyer"


def _conn():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 1)], [])
    return c


def _settled_fill(c, *, forward=True):
    bid = cms.create_pending_bid(
        c,
        owner=BUYER,
        slot="Head",
        value="Crown",
        price_brix="10",
        platform=None,
        condition="C",
        fulfillment_enc="S",
        cancel_after=9,
        payload_uuid=None,
        xumm_url=None,
        qr_url=None,
        push=None,
    )
    cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E", escrow_owner_seq=3)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    cms.update_fill(c, fill["id"], state=cms.FUNDED, escrow_finish_hash="FIN")
    cms.move_asset(c, fill["id"])
    cms.update_fill(c, fill["id"], state=cms.PAID, forward_tx_hash="FWD" if forward else None)
    return fill


def test_clean_book_passes():
    c = _conn()
    _settled_fill(c)
    assert audit.audit_rows(c) == []


def test_paid_without_forward_is_drift():
    c = _conn()
    fill = _settled_fill(c, forward=False)
    assert any(fill["id"] in p and "forward" in p for p in audit.audit_rows(c))


def test_over_encumbered_owner_is_drift():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    es.set_closet_contents(c, SELLER, [], [])
    assert any("encumbered" in p for p in audit.audit_rows(c))


def test_stuck_fill_is_reported():
    c = _conn()
    fill = _settled_fill(c)
    cms.update_fill(c, fill["id"], state=cms.ASSET_MOVED, forward_tx_hash=None)
    assert any("stuck" in p for p in audit.audit_rows(c, now=fill["created_ts"] + 7200))


def test_onchain_missing_escrow_and_short_balance(monkeypatch):
    c = _conn()
    bid = cms.create_pending_bid(
        c,
        owner=BUYER,
        slot="Head",
        value="Tiara",
        price_brix="10",
        platform=None,
        condition="C",
        fulfillment_enc="S",
        cancel_after=9,
        payload_uuid=None,
        xumm_url=None,
        qr_url=None,
        push=None,
    )
    cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E2", escrow_owner_seq=4)
    _settled_fill(c)
    fill_id = c.execute("SELECT id FROM closet_fills").fetchone()[0]
    cms.update_fill(c, fill_id, state=cms.ASSET_MOVED, forward_tx_hash=None)

    async def no_escrow(owner, seq):
        return None

    async def balance(address, currency, issuer):
        return Decimal("1")

    monkeypatch.setattr(audit.xrpl_ops, "get_escrow", no_escrow)
    monkeypatch.setattr(audit.xrpl_ops, "get_trustline_balance", balance)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rApp")
    monkeypatch.setattr(config, "BRIX_ISSUER", "rIssuer")
    problems = asyncio.new_event_loop().run_until_complete(audit.audit_onchain(c))
    assert any(bid["id"] in p and "escrow is gone" in p for p in problems)
    assert any("owes 10" in p for p in problems)


def test_setup_helpers(monkeypatch):
    assert setup.flag_set(0x40000000 | 0x00100000) is True
    assert setup.flag_set(0x00100000) is False
    data = {"RegularKey": "rRegKey"}
    assert setup.signer_allowed("rRegKey", "rIssuer", data)
    assert setup.signer_allowed("rIssuer", "rIssuer", {})
    assert not setup.signer_allowed("rOther", "rIssuer", data)
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_SEED", "sDistributor")
    monkeypatch.setattr(config, "SEED", "sSeed")
    assert setup.issuer_signer_seed("mainnet") == "sDistributor"
    assert setup.issuer_signer_seed("testnet") == "sSeed"


def test_account_set_memo_action_exists():
    assert memos.build_memo_models(
        memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_ACCOUNT_SET
    )


def test_unforwarded_brix_forward_landed_overshoot_pending():
    """Forward paid, overshoot still pending → owed == overshoot."""
    c = _conn()
    fill = _settled_fill(c)
    # Simulate: forward landed, overshoot pending
    cms.update_fill(
        c,
        fill["id"],
        state=cms.ASSET_MOVED,
        forward_tx_hash="FWD",
        overshoot_tx_hash=None,
    )
    # Direct DB update to set overshoot (not via update_fill, which doesn't allow it)
    c.execute(
        "UPDATE closet_fills SET overshoot_brix = ? WHERE id = ?",
        ("3", fill["id"]),
    )
    c.commit()
    owed = audit.unforwarded_brix(c)
    assert owed == Decimal("3")


def test_unforwarded_brix_nothing_paid_asset_moved():
    """Nothing paid on asset_moved cross fill → owed == (price - fee) + overshoot."""
    c = _conn()
    fill = _settled_fill(c)
    # Simulate: funds moved, but forward and overshoot not yet paid
    cms.update_fill(
        c,
        fill["id"],
        state=cms.ASSET_MOVED,
        forward_tx_hash=None,
        overshoot_tx_hash=None,
    )
    # Direct DB update to set overshoot
    c.execute(
        "UPDATE closet_fills SET overshoot_brix = ? WHERE id = ?",
        ("3", fill["id"]),
    )
    c.commit()
    # fill was created with price_brix="10", fee_bps=0 (so fee_brix="0")
    owed = audit.unforwarded_brix(c)
    assert owed == Decimal("13")  # (10 - 0) + 3


def test_unforwarded_brix_refund_pending_with_refund_brix():
    """Refund_pending with refund_brix set → owed == refund_brix."""
    c = _conn()
    fill = _settled_fill(c)
    # Simulate: refund_pending with a specific refund_brix value
    cms.update_fill(c, fill["id"], state=cms.REFUND_PENDING, refund_brix="2.5")
    owed = audit.unforwarded_brix(c)
    assert owed == Decimal("2.5")
