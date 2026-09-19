"""Fee-cover ops tooling: audit invariants, recovery/requeue CLI, report CLI."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import re

import pytest

from lfg_core import config, db_path, fee_cover, fee_cover_settle, xrpl_ops
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
    store.record_promise(conn, campaign, inp, decline_reason=None, now=1001)
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


def _private_loop_run(coro):
    # main() uses asyncio.run(), which leaves the global event loop unset on
    # Python 3.10 and breaks later modules that call get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _chain_lookup(monkeypatch, *, found=None, error=None):
    calls = []

    async def fake_find(accept_tx_hash, *, destination, drops, min_ledger):
        calls.append((accept_tx_hash, destination, drops, min_ledger))
        if error is not None:
            raise error
        return found

    monkeypatch.setattr(xrpl_ops, "find_fee_cover_payment", fake_find)
    monkeypatch.setattr(asyncio, "run", _private_loop_run)
    return calls


def _failed_refund(app_db):
    conn = _refund(app_db, state="failed")
    conn.execute("UPDATE fee_cover_refunds SET last_ledger_seq = 7400, claim_ledger = 7000")
    conn.commit()
    conn.close()


def _state(app_db):
    conn = store.connect(app_db)
    try:
        return store.get_refund(conn, "ACC1")["state"]
    finally:
        conn.close()


def _requeue_audit_rows(app_db):
    conn = store.connect(app_db)
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT actor, result, details FROM fee_cover_audit WHERE action = 'requeue'"
            )
        ]
    finally:
        conn.close()


def test_recover_cli_requeues_a_parked_failure(app_db, monkeypatch):
    _failed_refund(app_db)
    calls = _chain_lookup(monkeypatch, found=None)
    assert recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 0
    # scanned from the claim ledger, never the deadline
    assert calls == [("ACC1", "rB", 80_642, 7000)]
    assert _state(app_db) == "owed"
    # audited as the OS account that ran it (from the real UID, not a flag)
    [audit] = _requeue_audit_rows(app_db)
    assert audit["actor"] == f"cli:{pwd.getpwuid(os.getuid()).pw_name}"
    assert audit["result"] == "requeued"
    assert json.loads(audit["details"])["from_state"] == "failed"
    assert (
        recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 1
    )  # not failed any more
    assert len(calls) == 1  # no chain read for a row that is not failed


def test_recover_cli_refuses_to_requeue_a_refund_found_on_ledger(app_db, monkeypatch, capsys):
    _failed_refund(app_db)
    _chain_lookup(monkeypatch, found="PAYOUTHASH")
    assert recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 1
    assert "PAYOUTHASH found on-ledger; not requeued" in capsys.readouterr().out
    assert _state(app_db) == "failed"


def test_recover_cli_refuses_to_requeue_when_the_chain_check_errors(app_db, monkeypatch, capsys):
    _failed_refund(app_db)
    _chain_lookup(monkeypatch, error=RuntimeError("account_tx error: {'error': 'tooBusy'}"))
    assert recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 1
    captured = capsys.readouterr()
    assert "account_tx error" in captured.out + captured.err
    assert _state(app_db) == "failed"


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


# --- #560: alert-webhook wiring (scripts/_alerts.post_alert, shared with
# audit_trait_economy.py and audit_closet_market.py) ---


def _run_report(monkeypatch, argv):
    posted: list[str] = []
    monkeypatch.setattr(
        fee_cover_report, "post_alert", lambda url, body: posted.append(body) or True
    )
    rc = fee_cover_report.main(argv)
    return rc, posted


def _non_clean_app_db(app_db):
    conn = _refund(app_db, refund=200_000, fee=80_642, royalty=100_000, budget=150_000)
    conn.close()
    return app_db


def test_report_cli_non_clean_with_webhook_posts_once_with_network_and_report(app_db, monkeypatch):
    _non_clean_app_db(app_db)
    rc, posted = _run_report(
        monkeypatch,
        ["--network", NET, "--audit", "--alert-webhook", "https://discord.invalid/webhook"],
    )
    assert rc == 1
    assert len(posted) == 1
    body = posted[0]
    assert NET in body
    assert "ACC1" in body  # the specific finding's accept hash, not just a count
    assert f"Report: scripts/fee_cover_report.py --network {NET} --audit" in body


def test_report_cli_non_clean_without_webhook_posts_nothing(app_db, monkeypatch):
    _non_clean_app_db(app_db)
    monkeypatch.delenv("ECONOMY_AUDIT_WEBHOOK_URL", raising=False)
    rc, posted = _run_report(monkeypatch, ["--network", NET, "--audit"])
    assert rc == 1  # same exit code as today
    assert posted == []


def test_report_cli_clean_with_webhook_posts_nothing(app_db, monkeypatch):
    conn = _refund(app_db)
    conn.close()
    rc, posted = _run_report(
        monkeypatch,
        ["--network", NET, "--audit", "--alert-webhook", "https://discord.invalid/webhook"],
    )
    assert rc == 0
    assert posted == []


def test_report_cli_without_audit_posts_nothing_even_with_webhook(app_db, monkeypatch):
    """A plain report (no --audit) is not a finding, even if the underlying
    data would fail an audit and a webhook is configured."""
    _non_clean_app_db(app_db)
    rc, posted = _run_report(
        monkeypatch, ["--network", NET, "--alert-webhook", "https://discord.invalid/webhook"]
    )
    assert rc == 0
    assert posted == []


def test_build_alert_body_many_rows_keeps_trailer_and_says_how_many_elided():
    """#560 review round 1: a night with many fee-cover violations must not
    truncate away the reproduction/requeue commands."""
    violations = [f"ACC{i}: refund {80000 + i} exceeds promise {70000 + i}" for i in range(200)]
    body = fee_cover_report.build_alert_body("mainnet", violations)
    assert len(body) <= 1900
    assert body.endswith(
        "Report: scripts/fee_cover_report.py --network mainnet --audit\n"
        "After fixing the cause, requeue with scripts/recover_fee_cover_refunds.py "
        "--network mainnet --requeue <accept_hash>"
    )
    assert re.search(r"\.\.\. and \d+ more", body)
    assert "ACC199" not in body
