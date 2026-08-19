"""Service endpoints for the BRIX daily drip (#48).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lfg_core import brix_drip, history_store, xrpl_ops
from lfg_service import app as server


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    headers: dict = {}

    def __init__(self, body=None, match_info=None, query=None):
        self._body = body or {}
        self.match_info = match_info or {}
        self.query = query or {}
        self._store = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _body(resp):
    return json.loads(resp.body.decode())


WALLET = "rDevWallet"


@pytest.fixture
def drip(monkeypatch, tmp_path):
    """Dev-mode auth + a hermetic history DB holding the drip tables."""
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(
        server.config, "BRIX_DISTRIBUTOR_SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2", raising=False
    )
    monkeypatch.setattr(server.mock_economy, "DEV_OWNER", WALLET, raising=False)
    path = str(tmp_path / "history.db")
    monkeypatch.setattr(history_store, "history_db_path", lambda net=None: path)
    conn = history_store.init_history_db(path)
    brix_drip.ensure_schema(conn)

    # Claims never touch the network in these tests.
    async def ok_trustline(*a, **k):
        return 100

    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", ok_trustline)

    async def paid(destination, value, claim_id):
        return xrpl_ops.ClaimPayment("confirmed", "TXHASH", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", paid)
    return conn


def _accrue(conn, wallet=WALLET, count=3, epoch="2026-08-18"):
    brix_drip.record_accruals(
        conn,
        [brix_drip.Accrual(epoch, f"NFT_{wallet}_{i}", wallet, 1) for i in range(count)],
    )
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, epoch)


def test_brix_routes_registered():
    paths = {getattr(r.resource, "canonical", "") for r in server.create_app().router.routes()}
    assert "/api/brix" in paths
    assert "/api/brix/claim" in paths
    assert "/api/brix/claim/{claim_id}" in paths


def test_brix_claim_route_precedes_the_claim_id_wildcard():
    """aiohttp dispatches in registration order; if the wildcard came first it
    would swallow a POST path segment."""
    ordered = [
        getattr(r.resource, "canonical", "")
        for r in server.create_app().router.routes()
        if getattr(r.resource, "canonical", "").startswith("/api/brix")
    ]
    assert ordered.index("/api/brix/claim") < ordered.index("/api/brix/claim/{claim_id}")


def test_get_brix_reports_balance_and_last_epoch(drip):
    _accrue(drip)
    resp = _run(server.handle_brix_status(_Req()))
    data = _body(resp)
    assert data["wallet"] == WALLET
    assert data["claimable"] == 3
    assert data["unlisted_last_epoch"] == 3
    assert data["last_epoch"] == "2026-08-18"
    assert data["open_claim"] is None


def test_get_brix_never_hits_the_network(drip, monkeypatch):
    """unlisted_last_epoch must be a pure DB count — a per-request clio sweep
    would put a multi-thousand-token scan on a page load."""

    async def explode(*a, **k):
        raise AssertionError("no network call may happen on GET /api/brix")

    monkeypatch.setattr(xrpl_ops, "get_nft_sell_offers", explode)
    monkeypatch.setattr(brix_drip, "fetch_sell_offer_state", explode)
    _accrue(drip)
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


def test_get_brix_on_a_fresh_wallet_is_zero_not_an_error(drip):
    data = _body(_run(server.handle_brix_status(_Req())))
    assert data["claimable"] == 0
    assert data["unlisted_last_epoch"] == 0


def test_post_claim_pays_out_and_zeroes_the_balance(drip):
    _accrue(drip)
    data = _body(_run(server.handle_brix_claim(_Req())))
    assert data["state"] == "confirmed"
    assert data["amount"] == 3
    assert data["tx_hash"] == "TXHASH"
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 0


def test_post_claim_without_a_trustline_is_refused_before_any_state_change(drip, monkeypatch):
    async def no_line(*a, **k):
        return None

    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", no_line)
    _accrue(drip)
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 409
    assert _body(resp)["code"] == "trustline_required"
    # Nothing was bound, so the balance is intact and retryable.
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


def test_post_claim_with_nothing_accrued_is_400(drip):
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 400
    assert _body(resp)["code"] == "nothing_to_claim"


def test_post_claim_while_one_is_in_flight_is_409(drip, monkeypatch):
    async def unknown(destination, value, claim_id):
        return xrpl_ops.ClaimPayment("unknown", None, 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", unknown)
    _accrue(drip)
    _run(server.handle_brix_claim(_Req()))

    _accrue(drip, count=1, epoch="2026-08-19")
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 409
    assert _body(resp)["code"] == "claim_in_flight"


def test_a_definitively_failed_payout_restores_the_balance(drip, monkeypatch):
    async def failed(destination, value, claim_id):
        return xrpl_ops.ClaimPayment("failed", None, 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", failed)
    _accrue(drip)
    data = _body(_run(server.handle_brix_claim(_Req())))
    assert data["state"] == "failed"
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


def test_an_unknown_payout_keeps_the_balance_bound(drip, monkeypatch):
    """The ambiguous window: the payment may have landed, so the BRIX must NOT
    come back — that would let it be claimed twice."""

    async def unknown(destination, value, claim_id):
        return xrpl_ops.ClaimPayment("unknown", None, 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", unknown)
    _accrue(drip)
    _run(server.handle_brix_claim(_Req()))
    status = _body(_run(server.handle_brix_status(_Req())))
    assert status["claimable"] == 0
    assert status["open_claim"]["state"] == "submitted"


def test_claim_status_returns_the_callers_own_claim(drip):
    _accrue(drip)
    claim_id = _body(_run(server.handle_brix_claim(_Req())))["claim_id"]
    resp = _run(server.handle_brix_claim_status(_Req(match_info={"claim_id": str(claim_id)})))
    assert _body(resp)["state"] == "confirmed"


def test_claim_status_hides_another_wallets_claim(drip):
    conn = drip
    conn.execute(
        "INSERT INTO brix_claims (claim_id, wallet, amount, state) VALUES (99, 'rSomeoneElse', 5, 'confirmed')"
    )
    conn.commit()
    resp = _run(server.handle_brix_claim_status(_Req(match_info={"claim_id": "99"})))
    assert resp.status == 404


def test_claim_status_rejects_a_non_numeric_id(drip):
    resp = _run(server.handle_brix_claim_status(_Req(match_info={"claim_id": "abc"})))
    assert resp.status == 404


def test_post_claim_is_503_when_no_distributor_seed_is_configured(drip, monkeypatch):
    """Accrual works without any signing config; only the payout half needs it,
    so an unconfigured deployment must refuse cleanly rather than bind rows it
    can never pay."""
    monkeypatch.setattr(server.config, "BRIX_DISTRIBUTOR_SEED", None, raising=False)
    _accrue(drip)
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 503
    assert _body(resp)["code"] == "claims_disabled"
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


def test_a_pre_submission_failure_releases_the_balance(drip, monkeypatch):
    """If the payout raises BEFORE anything is submitted, the claim must not be
    left pending with a NULL last_ledger_seq: recovery deliberately leaves such
    claims untouched, so the wallet would be blocked by claim_in_flight forever
    and its BRIX would be unreachable."""

    async def cannot_submit(destination, value, claim_id):
        raise xrpl_ops.ClaimNotSubmitted("could not read the validated ledger index")

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", cannot_submit)
    _accrue(drip)
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 503
    # Balance restored and claimable again — nothing was ever submitted.
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3
    assert _body(_run(server.handle_brix_status(_Req())))["open_claim"] is None


def test_an_unexpected_payout_error_keeps_the_balance_bound(drip, monkeypatch):
    """An error we cannot prove happened BEFORE submission must fail closed:
    the payment may have landed, so the accruals stay bound for recovery."""

    async def mystery(destination, value, claim_id):
        raise RuntimeError("something unexpected after submit")

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", mystery)
    _accrue(drip)
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 502
    status = _body(_run(server.handle_brix_status(_Req())))
    assert status["claimable"] == 0
    assert status["open_claim"]["state"] == "submitted"


def test_an_unexpected_payout_error_still_records_a_ledger_deadline(drip, monkeypatch):
    """Failing closed is only safe if recovery can eventually reach a verdict.
    A claim marked unknown with a NULL last_ledger_seq is one recover() skips
    forever, stranding the accruals — so a conservative deadline is persisted."""

    async def mystery(destination, value, claim_id):
        raise RuntimeError("broke before the deadline existed")

    async def ledger():
        return 500000

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", mystery)
    monkeypatch.setattr(xrpl_ops, "current_validated_ledger_index", ledger)
    _accrue(drip)
    assert _run(server.handle_brix_claim(_Req())).status == 502

    row = drip.execute("SELECT last_ledger_seq, state FROM brix_claims").fetchone()
    assert row["state"] == "submitted"
    assert row["last_ledger_seq"] is not None
    # Generous on purpose: too EARLY a deadline would let recovery declare a
    # still-live payment failed and unbind accruals that were really paid.
    assert row["last_ledger_seq"] > 500000


def test_a_claim_row_never_exists_without_a_deadline(drip, monkeypatch):
    """Recovery skips NULL-deadline claims forever, so a claim that exists
    without one is unrecoverable. The deadline is written in the same
    transaction as the insert, making that state unrepresentable rather than
    merely unlikely — no crash or cancellation can land between them."""
    seen = {}

    async def cancelled(destination, value, claim_id):
        row = drip.execute(
            "SELECT last_ledger_seq FROM brix_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        seen["deadline_at_submit_time"] = row["last_ledger_seq"]
        raise asyncio.CancelledError()

    async def ledger():
        return 500000

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", cancelled)
    monkeypatch.setattr(xrpl_ops, "current_validated_ledger_index", ledger)
    _accrue(drip)

    with pytest.raises(asyncio.CancelledError):
        _run(server.handle_brix_claim(_Req()))

    # The deadline was already durable when the payout began, so recovery can
    # still reach a verdict on this claim after the restart.
    assert seen["deadline_at_submit_time"] is not None
    assert seen["deadline_at_submit_time"] > 500000


def test_no_claim_is_opened_when_the_ledger_cannot_be_read(drip, monkeypatch):
    """Refuse before binding anything: with no readable ledger there is no
    deadline to record, and a bound claim without one can never be recovered.
    Nothing bound means nothing stranded, and the holder just retries."""

    async def unreadable():
        return None

    async def must_not_run(destination, value, claim_id):
        raise AssertionError("no payout may be attempted without a deadline")

    monkeypatch.setattr(xrpl_ops, "current_validated_ledger_index", unreadable)
    monkeypatch.setattr(xrpl_ops, "send_brix_claim", must_not_run)
    _accrue(drip)

    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 503
    assert drip.execute("SELECT COUNT(*) FROM brix_claims").fetchone()[0] == 0
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3
