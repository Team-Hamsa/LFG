"""Fee-cover ops tooling: audit invariants, recovery/requeue CLI, report CLI."""

from __future__ import annotations

import asyncio

import pytest

from lfg_core import config, db_path, fee_cover, fee_cover_settle
from lfg_core import fee_cover_store as store
from scripts import fee_cover_report, recover_fee_cover_refunds

NET = "testnet"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"


@pytest.fixture()
def app_db(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    monkeypatch.setattr(db_path, "app_db_path", lambda network=None: path)
    monkeypatch.setattr(config, "XRPL_NETWORK", NET)
    return path


def _refund(
    path, *, refund=80_642, fee=80_642, royalty=349_605, state="confirmed", budget=1_000_000
):
    conn = store.connect(path)
    knobs = store.Knobs(
        coverage_bps=10_000, budget_drops=budget, wallet_cap_drops=10_000_000, min_bid_drops=0
    )
    campaign, _ = store.start_campaign(
        conn, network=NET, actor="t", knobs=knobs, duration_seconds=None, now=1000
    )
    inp = store.PromiseInput(
        offer_index="BID1",
        network=NET,
        nft_id="NFT1",
        bidder="rB",
        bid_drops=5_075_000,
        ask_drops=4_990_000,
        broker=CAFE,
        broker_rate=0.01589,
        bid_expiration=None,
    )
    store.record_promise(conn, campaign, inp, promised_drops=80_642, decline_reason=None, now=1001)
    store.fill_promise(
        conn, "BID1", store.RefundDecision("ACC1", "rS", CAFE, fee, royalty, 80_642, None), now=1002
    )
    conn.execute("UPDATE fee_cover_refunds SET refund_drops = ?, state = ?", (refund, state))
    conn.commit()
    return conn


def test_audit_is_clean_for_a_correct_refund(app_db):
    conn = _refund(app_db)
    assert fee_cover.audit_violations(conn, NET) == []


def test_audit_flags_every_broken_invariant(app_db):
    conn = _refund(app_db, refund=200_000, fee=80_642, royalty=100_000, budget=150_000)
    problems = fee_cover.audit_violations(conn, NET)
    assert any("exceeds promise" in p for p in problems)
    assert any("exceeds observed fee" in p for p in problems)
    assert any("50% of observed royalty" in p for p in problems)
    assert any("exceeds budget" in p for p in problems)


def test_recover_cli_refuses_a_network_mismatch(app_db):
    assert recover_fee_cover_refunds.main(["--network", "mainnet"]) == 2


def test_recover_cli_requeues_a_parked_failure(app_db):
    conn = _refund(app_db, state="failed")
    conn.close()
    assert recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 0
    conn = store.connect(app_db)
    assert store.get_refund(conn, "ACC1")["state"] == "owed"
    assert (
        recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 1
    )  # not failed any more


def test_recover_cli_runs_chain_recovery(app_db, monkeypatch, capsys):
    seen = []

    async def fake_recover(deps):
        seen.append(deps)
        return {"ACC1": "confirmed"}

    def private_loop_run(coro):
        # main() uses asyncio.run(), which leaves the global event loop unset
        # on Python 3.10 and breaks later modules that call get_event_loop().
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(fee_cover_settle, "recover_refunds", fake_recover)
    monkeypatch.setattr(asyncio, "run", private_loop_run)
    assert recover_fee_cover_refunds.main(["--network", NET]) == 0
    assert isinstance(seen[0], fee_cover_settle.FeeCoverDeps) and seen[0].network == NET
    assert "ACC1: confirmed" in capsys.readouterr().out


def test_report_cli_exit_code_follows_the_audit(app_db, capsys):
    conn = _refund(app_db)
    conn.close()
    assert fee_cover_report.main(["--network", NET, "--audit"]) == 0
    out = capsys.readouterr().out
    assert "state: active" in out and "committed" in out
    conn = store.connect(app_db)
    conn.execute("UPDATE fee_cover_refunds SET refund_drops = 999999")
    conn.commit()
    conn.close()
    assert fee_cover_report.main(["--network", NET, "--audit"]) == 1
    assert "AUDIT FAIL" in capsys.readouterr().out
