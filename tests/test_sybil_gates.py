"""Sybil-resistance gates for sponsored free-mint admission: all-time
same-funder (non-exchange) and same-device dedup across campaigns."""

import os
import sqlite3

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import funding
from lfg_core import sponsored_mint as sm
from tests.sponsored_helpers import ready_history

FARM_FUNDER = "rFarmFunder111111111111111111111"
EXCHANGE = next(iter(funding.EXCHANGES))


def paths(tmp_path, *, network="mainnet"):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    ready_history(history, network=network)
    return db, history


def lookup_from(mapping):
    def lookup(wallet):
        v = mapping[wallet]
        if isinstance(v, Exception):
            raise v
        return v

    return lookup


def reserve(db, history, wallet, session_id, **kw):
    return sm.reserve_if_eligible(
        db, history, network="mainnet", wallet=wallet, session_id=session_id, now=101, **kw
    )


# --- funding module ---------------------------------------------------------


def test_funder_cache_roundtrip(tmp_path):
    db = str(tmp_path / "app.db")
    with sqlite3.connect(db) as conn:
        funding.ensure_schema(conn)
        assert funding.cached_funder(conn, "rW") is None
        funding.record_funder(conn, "rW", FARM_FUNDER, 123)
        assert funding.cached_funder(conn, "rW") == FARM_FUNDER
        # unfunded wallets cache their None result too
        funding.record_funder(conn, "rEmpty", None, None)
        assert funding.cached_funder(conn, "rEmpty") is None
        assert funding.has_cached_funder(conn, "rEmpty")


# --- funder gate ------------------------------------------------------------


def test_same_nonexchange_funder_blocked_across_campaigns(tmp_path):
    db, history = paths(tmp_path)
    lookup = lookup_from({"rA": (FARM_FUNDER, 1), "rB": (FARM_FUNDER, 2)})
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1", funder_lookup=lookup).sponsored

    # same campaign
    second = reserve(db, history, "rB", "s2", funder_lookup=lookup)
    assert not second.sponsored
    assert second.reason == "ineligible"

    # later campaign: all-time dedup
    sm.stop_campaign(db, network="mainnet", actor="42", now=102)
    sm.start_campaign(db, network="mainnet", actor="42", now=103)
    third = sm.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="rB",
        session_id="s3",
        now=104,
        funder_lookup=lookup,
    )
    assert not third.sponsored
    assert third.reason == "ineligible"


def test_exchange_funder_not_deduped(tmp_path):
    db, history = paths(tmp_path)
    lookup = lookup_from({"rA": (EXCHANGE, 1), "rB": (EXCHANGE, 2)})
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1", funder_lookup=lookup).sponsored
    assert reserve(db, history, "rB", "s2", funder_lookup=lookup).sponsored


def test_funder_lookup_failure_fails_closed(tmp_path):
    db, history = paths(tmp_path)
    lookup = lookup_from({"rA": funding.FunderLookupError("boom")})
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    result = reserve(db, history, "rA", "s1", funder_lookup=lookup)
    assert not result.sponsored
    assert result.reason == "eligibility_unavailable"


def test_unfunded_wallet_is_ineligible(tmp_path):
    db, history = paths(tmp_path)
    lookup = lookup_from({"rA": (None, None)})
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    result = reserve(db, history, "rA", "s1", funder_lookup=lookup)
    assert not result.sponsored
    assert result.reason == "ineligible"


def test_funder_lookup_cached_after_first_admission(tmp_path):
    db, history = paths(tmp_path)
    calls = []

    def lookup(wallet):
        calls.append(wallet)
        return (FARM_FUNDER, 1)

    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1", funder_lookup=lookup).sponsored
    # re-poll of the same wallet/session must not re-hit the RPC
    assert reserve(db, history, "rA", "s1", funder_lookup=lookup).sponsored
    assert calls == ["rA"]


def test_no_lookup_provided_skips_funder_gate(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1").sponsored


# --- device gate ------------------------------------------------------------


def test_same_device_different_wallet_blocked(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1", device_key="dev1").sponsored
    result = reserve(db, history, "rB", "s2", device_key="dev1")
    assert not result.sponsored
    assert result.reason == "ineligible"


def test_same_device_blocked_across_campaigns(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1", device_key="dev1").sponsored
    sm.stop_campaign(db, network="mainnet", actor="42", now=102)
    sm.start_campaign(db, network="mainnet", actor="42", now=103)
    result = sm.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="rB",
        session_id="s2",
        now=104,
        device_key="dev1",
    )
    assert not result.sponsored
    assert result.reason == "ineligible"


def test_same_device_same_wallet_repoll_ok(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1", device_key="dev1").sponsored
    assert reserve(db, history, "rA", "s1", device_key="dev1").sponsored


def test_missing_device_key_skips_device_gate(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert reserve(db, history, "rA", "s1", device_key="dev1").sponsored
    assert reserve(db, history, "rB", "s2").sponsored


def test_device_key_backfill_on_existing_claim(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    # first reserve had no token yet; token learned when the claim payload signs
    assert reserve(db, history, "rA", "s1").sponsored
    sm.set_claim_device_key(db, network="mainnet", wallet="rA", device_key="dev1")
    result = reserve(db, history, "rB", "s2", device_key="dev1")
    assert not result.sponsored
    assert result.reason == "ineligible"


# --- backfill script --------------------------------------------------------


def test_backfill_wallet_funders_covers_legacy_claimants(tmp_path, monkeypatch):
    import importlib
    import sys

    from tests.sponsored_helpers import ready_history

    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    ready_history(history)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rLegacy", session_id="s1", now=101
    ).sponsored

    script = importlib.import_module("scripts.backfill_wallet_funders")
    monkeypatch.setattr(script.config, "XRPL_NETWORK", "mainnet")
    monkeypatch.setattr(script.funding, "lookup_funder", lambda w: (FARM_FUNDER, 7))
    monkeypatch.setattr(sys, "argv", ["backfill", "--network", "mainnet", "--app-db", db])
    assert script.main() == 0

    with sqlite3.connect(db) as conn:
        assert funding.cached_funder(conn, "rLegacy") == FARM_FUNDER
    # idempotent: nothing left to do
    assert script.main() == 0
