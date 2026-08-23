# tests/test_closet_pending_sweep.py
# #382: Closet accepts missed during listener downtime — the service-side
# `sweep_pending_closet_accepts` backstop promotes `pending_accept` rows to
# `active` once clio confirms the owner, and never promotes otherwise.

import asyncio
import sqlite3

import pytest

from lfg_core import economy_store as es
from lfg_core.nft_index import init_db as init_onchain_db
from lfg_service import app as server

ISSUER = "rIssuer0000000000000000000000000000"
OWNER_A = "rOwnerA000000000000000000000000000"
OWNER_B = "rOwnerB000000000000000000000000000"
OWNER_C = "rOwnerC000000000000000000000000000"
OWNER_D = "rOwnerD000000000000000000000000000"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def onchain_env(tmp_path, monkeypatch):
    onchain_path = str(tmp_path / "onchain_testnet.db")
    conn = init_onchain_db(onchain_path)
    es.init_economy_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(server, "_CLOSET_SWEEP_DELAY_SECONDS", 0)
    monkeypatch.setattr(server, "_closet_sweep_offset", 0)
    yield onchain_path


def _seed(path, owner, nft_id, status):
    conn = sqlite3.connect(path)
    es.set_closet_token(conn, owner, nft_id, "AB", status=status, offer_id="OFF")
    conn.close()


def _status(path, owner):
    conn = sqlite3.connect(path)
    try:
        rec = es.get_closet_record(conn, owner)
    finally:
        conn.close()
    return None if rec is None else rec[2]


def test_list_pending_closets_oldest_first_with_limit():
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    es.set_closet_token(conn, OWNER_A, "NA", "AB", status="pending_accept")
    conn.execute(
        "UPDATE closet_tokens SET updated_at='2026-01-01 00:00:00' WHERE owner=?", (OWNER_A,)
    )
    es.set_closet_token(conn, OWNER_B, "NB", "AB", status="pending_accept")
    conn.execute(
        "UPDATE closet_tokens SET updated_at='2026-02-01 00:00:00' WHERE owner=?", (OWNER_B,)
    )
    es.set_closet_token(conn, OWNER_C, "NC", "AB", status="active")
    conn.commit()
    assert es.list_pending_closets(conn) == [(OWNER_A, "NA"), (OWNER_B, "NB")]
    assert es.list_pending_closets(conn, limit=1) == [(OWNER_A, "NA")]
    assert es.list_pending_closets(conn, limit=1, offset=1) == [(OWNER_B, "NB")]
    assert es.list_pending_closets(conn, limit=1, offset=2) == []


def test_sweep_promotes_only_ledger_confirmed_rows(onchain_env, monkeypatch):
    _seed(onchain_env, OWNER_A, "NA", "pending_accept")
    _seed(onchain_env, OWNER_B, "NB", "pending_accept")
    _seed(onchain_env, OWNER_C, "NC", "pending_accept")
    _seed(onchain_env, OWNER_D, "ND", "active")
    looked_up: list[str] = []

    async def fake_nft_info(nft_id, clio=None):
        looked_up.append(nft_id)
        if nft_id == "NA":
            return {"owner": OWNER_A}
        if nft_id == "NB":
            return {"owner": ISSUER}  # offer not accepted yet
        raise RuntimeError("clio down")  # NC: lookup blows up

    monkeypatch.setattr(server.xrpl_ops, "nft_info", fake_nft_info)
    _run(server.sweep_pending_closet_accepts())

    assert _status(onchain_env, OWNER_A) == "active"
    assert _status(onchain_env, OWNER_B) == "pending_accept"
    assert _status(onchain_env, OWNER_C) == "pending_accept"
    assert _status(onchain_env, OWNER_D) == "active"
    # Only pending rows are looked up; the raising row did not stop the pass.
    assert sorted(looked_up) == ["NA", "NB", "NC"]


def test_sweep_lookup_failure_never_promotes(onchain_env, monkeypatch):
    _seed(onchain_env, OWNER_A, "NA", "pending_accept")

    async def fake_nft_info(nft_id, clio=None):
        return None

    monkeypatch.setattr(server.xrpl_ops, "nft_info", fake_nft_info)
    _run(server.sweep_pending_closet_accepts())
    assert _status(onchain_env, OWNER_A) == "pending_accept"


def test_sweep_honors_batch_limit(onchain_env, monkeypatch):
    for i, owner in enumerate([OWNER_A, OWNER_B, OWNER_C]):
        _seed(onchain_env, owner, f"N{i}", "pending_accept")
    monkeypatch.setattr(server, "_CLOSET_SWEEP_BATCH", 2)
    calls: list[str] = []

    async def fake_nft_info(nft_id, clio=None):
        calls.append(nft_id)
        return {"owner": ISSUER}

    monkeypatch.setattr(server.xrpl_ops, "nft_info", fake_nft_info)
    _run(server.sweep_pending_closet_accepts())
    assert len(calls) == 2


def test_sweep_rotates_so_stuck_oldest_rows_cannot_starve_newer_ones(onchain_env, monkeypatch):
    # Greptile P1 on #436: if the oldest batch stays issuer-owned forever, later
    # passes must still reach the newer rows — the window rotates across passes.
    conn = sqlite3.connect(onchain_env)
    for i, owner in enumerate([OWNER_A, OWNER_B, OWNER_C]):
        es.set_closet_token(conn, owner, f"N{i}", "AB", status="pending_accept")
        conn.execute(
            "UPDATE closet_tokens SET updated_at=? WHERE owner=?",
            (f"2026-0{i + 1}-01 00:00:00", owner),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "_CLOSET_SWEEP_BATCH", 2)

    async def fake_nft_info(nft_id, clio=None):
        return {"owner": OWNER_C if nft_id == "N2" else ISSUER}

    monkeypatch.setattr(server.xrpl_ops, "nft_info", fake_nft_info)
    _run(server.sweep_pending_closet_accepts())  # pass 1: N0, N1 (both still issuer-owned)
    assert _status(onchain_env, OWNER_C) == "pending_accept"
    _run(server.sweep_pending_closet_accepts())  # pass 2: N2 → promoted
    assert _status(onchain_env, OWNER_C) == "active"
    _run(server.sweep_pending_closet_accepts())  # pass 3 wraps back to the start, no crash
    assert _status(onchain_env, OWNER_A) == "pending_accept"


def test_sweep_noop_when_economy_disabled(onchain_env, monkeypatch):
    _seed(onchain_env, OWNER_A, "NA", "pending_accept")
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)

    async def fake_nft_info(nft_id, clio=None):
        raise AssertionError("must not hit clio when the economy is off")

    monkeypatch.setattr(server.xrpl_ops, "nft_info", fake_nft_info)
    _run(server.sweep_pending_closet_accepts())
    assert _status(onchain_env, OWNER_A) == "pending_accept"
