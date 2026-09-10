# ruff: noqa: F811 - _service_env is a fixture re-exported from test_sponsored_mint_flow
"""Login-time funder warm + read-only sponsored-eligibility preview.

The funder gate resolves a wallet's activation funder at reservation time.
Warming that cache at sign-in (and exposing a preview that runs the SAME
predicate as the reservation, without writing a claim) lets the client show
free-mint eligibility before the user starts a mint.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from lfg_core import funding
from lfg_core import sponsored_mint as sm
from lfg_service import app as server
from tests.sponsored_helpers import ready_history, stub_clean_preflight
from tests.test_sponsored_mint_flow import (  # noqa: F401 - fixture re-export
    _PostRequest,
    _run,
    _service_env,
)


def _paths(tmp_path):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    ready_history(history, network="mainnet")
    return db, history


def _funders(db):
    with sqlite3.connect(db) as conn:
        funding.ensure_schema(conn)
        return conn.execute("SELECT wallet, funder, ledger_index FROM wallet_funders").fetchall()


# --- funding.warm_funder_cache ---------------------------------------------


def test_warm_records_uncached_funder(tmp_path):
    db = str(tmp_path / "app.db")
    calls = []

    def lookup(wallet):
        calls.append(wallet)
        return ("rFunder", 7)

    assert funding.warm_funder_cache(db, "rNew", lookup=lookup) is True
    assert _funders(db) == [("rNew", "rFunder", 7)]
    assert calls == ["rNew"]


def test_warm_skips_lookup_when_cached(tmp_path):
    db = str(tmp_path / "app.db")
    with sqlite3.connect(db) as conn:
        funding.ensure_schema(conn)
        funding.record_funder(conn, "rKnown", "rFunder", 1)

    def lookup(_wallet):
        raise AssertionError("cached wallet must not hit the ledger")

    assert funding.warm_funder_cache(db, "rKnown", lookup=lookup) is False
    assert _funders(db) == [("rKnown", "rFunder", 1)]


def test_warm_does_not_cache_unfunded_wallet(tmp_path):
    db = str(tmp_path / "app.db")
    assert funding.warm_funder_cache(db, "rGhost", lookup=lambda _w: (None, None)) is False
    assert _funders(db) == []


def test_warm_swallows_lookup_failure(tmp_path):
    db = str(tmp_path / "app.db")

    def lookup(_wallet):
        raise funding.FunderLookupError("boom")

    assert funding.warm_funder_cache(db, "rFlaky", lookup=lookup) is False
    assert _funders(db) == []


# --- sponsored_mint.preview_eligibility --------------------------------------


def test_preview_reports_eligible_without_writing_a_claim(tmp_path):
    db, history = _paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)

    result = sm.preview_eligibility(
        db,
        history,
        network="mainnet",
        wallet="rNew",
        now=101,
        funder_lookup=lambda _w: ("rFunder", 1),
    )

    assert result.sponsored is True
    assert result.reason == "eligible"
    assert result.claim is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM free_mint_claims").fetchone()[0] == 0
    # The preview still warms the funder cache — that is the point of it.
    assert _funders(db) == [("rNew", "rFunder", 1)]


def test_preview_refuses_a_funder_sibling(tmp_path):
    db, history = _paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    first = sm.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="rFirst",
        session_id="s1",
        now=101,
        funder_lookup=lambda _w: ("rFarm", 1),
    )
    assert first.sponsored

    result = sm.preview_eligibility(
        db,
        history,
        network="mainnet",
        wallet="rSecond",
        now=102,
        funder_lookup=lambda _w: ("rFarm", 2),
    )

    assert result.sponsored is False
    assert result.reason == "ineligible"


def test_preview_reports_campaign_off(tmp_path):
    db, history = _paths(tmp_path)
    sm.ensure_schema(db)
    result = sm.preview_eligibility(db, history, network="mainnet", wallet="rNew", now=101)
    assert (result.sponsored, result.reason) == (False, "campaign_off")


def test_preview_surfaces_the_wallets_own_live_reservation(tmp_path):
    db, history = _paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    reserved = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rMine", session_id="s1", now=101
    )
    assert reserved.sponsored

    result = sm.preview_eligibility(db, history, network="mainnet", wallet="rMine", now=102)

    assert result.sponsored is True
    assert result.reason == "reserved"
    assert result.claim is not None and result.claim.id == reserved.claim.id


def test_preview_matches_reservation_verdict_for_a_consumed_wallet(tmp_path):
    db, history = _paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    sm.reserve_if_eligible(db, history, network="mainnet", wallet="rDone", session_id="s1", now=101)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE free_mint_claims SET nft_id='NFT' WHERE wallet='rDone'")

    preview = sm.preview_eligibility(db, history, network="mainnet", wallet="rDone", now=102)
    real = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rDone", session_id="s2", now=102
    )
    assert (preview.sponsored, preview.reason) == (real.sponsored, real.reason)
    assert preview.reason == "already_consumed"


# --- service: warm at sign-in, preview endpoint -------------------------------


def _signin_env(_service_env, monkeypatch, lookup):
    # Other sign-in tests leave warm tasks from their (closed) loops behind;
    # gathering one of those raises "belongs to a different loop".
    server._funder_warm_tasks.clear()
    monkeypatch.setattr(server.funding, "lookup_funder", lookup)
    monkeypatch.setattr(server.identity_store, "DATABASE", str(_service_env.app_db), raising=False)
    server.identity_store.ensure_identities_table()


def test_web_signin_warms_funder_cache(_service_env, monkeypatch):
    _signin_env(_service_env, monkeypatch, lambda _w: ("rFunder", 3))

    async def scenario():
        resp = await server._finish_web_signin("rLoginWallet", "walletconnect")
        await asyncio.gather(*server._funder_warm_tasks)
        return resp

    resp = _run(scenario())
    assert resp.status == 200
    assert _funders(_service_env.app_db) == [("rLoginWallet", "rFunder", 3)]


def test_web_signin_survives_funder_lookup_failure(_service_env, monkeypatch):
    def boom(_w):
        raise funding.FunderLookupError("down")

    _signin_env(_service_env, monkeypatch, boom)

    async def scenario():
        resp = await server._finish_web_signin("rLoginWallet", "walletconnect")
        await asyncio.gather(*server._funder_warm_tasks)
        return resp

    assert _run(scenario()).status == 200
    assert _funders(_service_env.app_db) == []


def test_eligibility_endpoint_returns_preview(_service_env, monkeypatch):
    # The endpoint runs on wall-clock time, so the campaign must be live NOW.
    _live_campaign(_service_env, monkeypatch)
    stub_clean_preflight(monkeypatch, server)

    resp = _run(server.handle_sponsored_eligibility(_PostRequest()))

    body = json.loads(resp.text)
    assert resp.status == 200
    assert body == {"eligible": True, "reason": "eligible"}
    with sqlite3.connect(_service_env.app_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM free_mint_claims").fetchone()[0] == 0


def test_eligibility_endpoint_fails_closed_before_recovery(_service_env, monkeypatch):
    sm.start_campaign(_service_env.app_db, network="mainnet", actor="42", now=100)
    monkeypatch.setattr(server, "_sponsored_recovery_ready", False)

    resp = _run(server.handle_sponsored_eligibility(_PostRequest()))

    assert json.loads(resp.text) == {"eligible": False, "reason": "eligibility_unavailable"}


def test_eligibility_route_is_registered():
    routes = {r.resource.canonical for r in server.create_app().router.routes() if r.resource}
    assert "/api/mint/sponsored/eligibility" in routes


# --- Greptile review (PR #472) ------------------------------------------------


def test_warm_never_raises_on_unexpected_resolver_or_db_errors(tmp_path):
    def explode(_wallet):
        raise RuntimeError("not a FunderLookupError")

    assert funding.warm_funder_cache(str(tmp_path / "app.db"), "rBoom", lookup=explode) is False
    # An unwritable db path (a directory) must be contained too.
    assert funding.warm_funder_cache(str(tmp_path), "rDir", lookup=lambda _w: ("rF", 1)) is False


def test_proof_link_warms_the_newly_linked_wallet(_service_env, monkeypatch):
    _signin_env(_service_env, monkeypatch, lambda _w: ("rFunder", 9))
    monkeypatch.setattr(server.identity_store, "link_proof", lambda *a, **k: True)

    async def scenario():
        await server._write_proof_link("rSession", "rLinked", "wc-signed-tx")
        await asyncio.gather(*server._funder_warm_tasks)

    _run(scenario())
    assert _funders(_service_env.app_db) == [("rLinked", "rFunder", 9)]


def _live_campaign(_service_env, monkeypatch):
    sm.start_campaign(_service_env.app_db, network="mainnet", actor="42")
    ready_history(_service_env.history_db, network="mainnet", now=int(time.time()))
    monkeypatch.setattr(server.funding, "lookup_funder", lambda _w: ("rFunder", 3))


def test_eligibility_endpoint_refuses_a_destination_that_cannot_take_delivery(
    _service_env, monkeypatch
):
    _live_campaign(_service_env, monkeypatch)

    async def unfunded(_wallet):
        return server.xrpl_ops.DestinationPreflight(False, None, None, None)

    monkeypatch.setattr(server.xrpl_ops, "destination_preflight", unfunded)
    resp = _run(server.handle_sponsored_eligibility(_PostRequest()))
    assert json.loads(resp.text) == {"eligible": False, "reason": "wallet_unfunded"}


def test_preview_still_reports_own_reservation_after_campaign_stops_or_expires(tmp_path):
    """CodeRabbit #472: the wallet's live promise outranks the campaign-state
    fast path — a stopped/expired campaign must not hide it."""
    db, history = _paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    reserved = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rMine", session_id="s1", now=101
    )
    assert reserved.sponsored

    sm.stop_campaign(db, network="mainnet", actor="42", now=102)
    stopped = sm.preview_eligibility(db, history, network="mainnet", wallet="rMine", now=103)
    assert (stopped.sponsored, stopped.reason) == (True, "reserved")

    expired = sm.preview_eligibility(db, history, network="mainnet", wallet="rMine", now=5000)
    assert (expired.sponsored, expired.reason) == (True, "reserved")
    # …while a wallet WITHOUT a promise still sees the campaign state.
    other = sm.preview_eligibility(db, history, network="mainnet", wallet="rOther", now=103)
    assert other.reason == "campaign_off"


def test_eligibility_endpoint_fails_closed_on_unresolved_preflight(_service_env, monkeypatch):
    _live_campaign(_service_env, monkeypatch)

    async def unresolved(_wallet):
        raise TimeoutError("account_info blip")

    monkeypatch.setattr(server.xrpl_ops, "destination_preflight", unresolved)
    resp = _run(server.handle_sponsored_eligibility(_PostRequest()))
    assert json.loads(resp.text) == {"eligible": False, "reason": "eligibility_unavailable"}
