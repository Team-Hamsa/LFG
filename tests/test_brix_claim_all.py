"""Claim-all BRIX across a user's linked wallets (#446).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from lfg_core import brix_drip, history_store, xrpl_ops
from lfg_service import app as server
from lfg_service import identity as identity_store


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
W_B = "rLinkedWalletB"
W_C = "rLinkedWalletC"


@pytest.fixture
def drip(monkeypatch, tmp_path):
    """Dev-mode auth + hermetic history AND identity DBs."""
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(
        server.config, "BRIX_DISTRIBUTOR_SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2", raising=False
    )
    monkeypatch.setattr(server.mock_economy, "DEV_OWNER", WALLET, raising=False)
    server.brix_trustline_payloads.clear()
    server.brix_claim_all_jobs.clear()
    path = str(tmp_path / "history.db")
    monkeypatch.setattr(history_store, "history_db_path", lambda net=None: path)
    conn = history_store.init_history_db(path)
    brix_drip.ensure_schema(conn)

    monkeypatch.setattr(identity_store, "DATABASE", str(tmp_path / "identity.db"))
    identity_store.ensure_identities_table()

    async def ok_trustline(*a, **k):
        return xrpl_ops.TrustlineState.PRESENT, Decimal(100)

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", ok_trustline)

    async def paid(destination, value, claim_id, max_last_ledger_seq=None):
        return xrpl_ops.ClaimPayment("confirmed", f"TX_{destination}", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", paid)
    return conn


def _accrue(conn, wallet=WALLET, count=3, epoch="2026-08-18"):
    brix_drip.record_accruals(
        conn,
        [brix_drip.Accrual(epoch, f"NFT_{wallet}_{i}", wallet, 1) for i in range(count)],
    )
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, epoch)


def _link_bucket(*wallets):
    """Co-observe one push token across wallets — the #445 signed-evidence edge."""
    for w in wallets:
        identity_store.observe_token(w, "raw-token-shared")


async def _drain_job(job_id):
    task = server.brix_claim_all_jobs[job_id].get("task")
    if task is not None:
        await task


# --- routes ---------------------------------------------------------------


def test_claim_all_routes_registered_before_the_claim_id_wildcard():
    """A POST to /api/brix/claim/all must not be swallowed by the
    /api/brix/claim/{claim_id} resource (405), so it registers first."""
    ordered = [
        getattr(r.resource, "canonical", "")
        for r in server.create_app().router.routes()
        if getattr(r.resource, "canonical", "").startswith("/api/brix")
    ]
    assert "/api/brix/claim/all" in ordered
    assert "/api/brix/claim/all/{job_id}" in ordered
    assert ordered.index("/api/brix/claim/all") < ordered.index("/api/brix/claim/{claim_id}")


# --- GET /api/brix linked view -------------------------------------------


def test_get_brix_reports_linked_wallets(drip):
    _link_bucket(WALLET, W_B)
    _accrue(drip)
    _accrue(drip, wallet=W_B, count=2)
    data = _body(_run(server.handle_brix_status(_Req())))
    linked = {r["wallet"]: r["claimable"] for r in data["linked"]}
    assert linked == {WALLET: 3, W_B: 2}
    assert data["linked"][0]["wallet"] == WALLET  # caller first
    assert data["linked_claimable_total"] == 5


def test_get_brix_linked_is_just_the_caller_when_unlinked(drip):
    _accrue(drip)
    data = _body(_run(server.handle_brix_status(_Req())))
    assert data["linked"] == [{"wallet": WALLET, "claimable": 3}]
    assert data["linked_claimable_total"] == 3


def test_get_brix_omits_linked_when_the_bucket_lookup_fails(drip, monkeypatch):
    """A broken identity DB must not take down the whole BRIX card."""

    def boom(w):
        raise identity_store.BucketLookupError("db down")

    monkeypatch.setattr(identity_store, "bucket_for_wallet", boom)
    _accrue(drip)
    data = _body(_run(server.handle_brix_status(_Req())))
    assert "linked" not in data
    assert data["claimable"] == 3


# --- POST /api/brix/claim/all --------------------------------------------


def test_claim_all_claims_every_linked_wallet_sequentially(drip):
    _link_bucket(WALLET, W_B, W_C)
    _accrue(drip)
    _accrue(drip, wallet=W_B, count=2)
    _accrue(drip, wallet=W_C, count=5)

    async def go():
        resp = server_resp = await server.handle_brix_claim_all(_Req())
        body = _body(server_resp)
        await _drain_job(body["job_id"])
        return resp, body

    resp, body = _run(go())
    assert resp.status == 200
    poll = _body(
        _run(server.handle_brix_claim_all_status(_Req(match_info={"job_id": body["job_id"]})))
    )
    assert poll["state"] == "done"
    rows = {r["wallet"]: r for r in poll["wallets"]}
    assert set(rows) == {WALLET, W_B, W_C}
    for w, amt in ((WALLET, 3), (W_B, 2), (W_C, 5)):
        assert rows[w]["status"] == "confirmed"
        assert rows[w]["amount"] == amt
        assert rows[w]["tx_hash"] == f"TX_{w}"
    # every wallet's balance is drained
    for w in (WALLET, W_B, W_C):
        assert brix_drip.claimable(drip, w) == 0


def test_claim_all_pays_each_wallet_to_its_own_address(drip, monkeypatch):
    """Linking must never redirect a payout: the Payment destination is always
    the accrual owner's own wallet."""
    destinations = []

    async def paid(destination, value, claim_id, max_last_ledger_seq=None):
        destinations.append(destination)
        return xrpl_ops.ClaimPayment("confirmed", f"TX_{destination}", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", paid)
    _link_bucket(WALLET, W_B)
    _accrue(drip)
    _accrue(drip, wallet=W_B, count=2)

    async def go():
        body = _body(await server.handle_brix_claim_all(_Req()))
        await _drain_job(body["job_id"])

    _run(go())
    assert sorted(destinations) == sorted([WALLET, W_B])


def test_claim_all_skips_a_wallet_without_a_trustline_and_continues(drip, monkeypatch):
    async def only_b_missing(wallet, *a, **k):
        if wallet == W_B:
            return xrpl_ops.TrustlineState.ABSENT, None
        return xrpl_ops.TrustlineState.PRESENT, Decimal(100)

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", only_b_missing)
    _link_bucket(WALLET, W_B, W_C)
    _accrue(drip)
    _accrue(drip, wallet=W_B, count=2)
    _accrue(drip, wallet=W_C, count=5)

    async def go():
        body = _body(await server.handle_brix_claim_all(_Req()))
        await _drain_job(body["job_id"])
        return body

    body = _run(go())
    poll = _body(
        _run(server.handle_brix_claim_all_status(_Req(match_info={"job_id": body["job_id"]})))
    )
    rows = {r["wallet"]: r for r in poll["wallets"]}
    assert rows[W_B]["status"] == "trustline_required"
    assert rows[WALLET]["status"] == "confirmed"
    assert rows[W_C]["status"] == "confirmed"
    assert poll["state"] == "done"
    # the skipped wallet's balance is untouched and retryable
    assert brix_drip.claimable(drip, W_B) == 2


def test_claim_all_an_unexpected_per_wallet_error_does_not_kill_the_job(drip, monkeypatch):
    async def b_explodes(destination, value, claim_id, max_last_ledger_seq=None):
        if destination == W_B:
            raise RuntimeError("boom")
        return xrpl_ops.ClaimPayment("confirmed", f"TX_{destination}", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", b_explodes)
    _link_bucket(WALLET, W_B, W_C)
    _accrue(drip)
    _accrue(drip, wallet=W_B, count=2)
    _accrue(drip, wallet=W_C, count=5)

    async def go():
        body = _body(await server.handle_brix_claim_all(_Req()))
        await _drain_job(body["job_id"])
        return body

    body = _run(go())
    poll = _body(
        _run(server.handle_brix_claim_all_status(_Req(match_info={"job_id": body["job_id"]})))
    )
    rows = {r["wallet"]: r for r in poll["wallets"]}
    # W_B hit the unexpected-error path (fail-closed, accruals stay bound)
    assert rows[W_B]["status"] == "claim_unconfirmed"
    assert rows[W_C]["status"] == "confirmed"
    assert poll["state"] == "done"


def test_claim_all_with_nothing_claimable_anywhere_is_400(drip):
    _link_bucket(WALLET, W_B)
    resp = _run(server.handle_brix_claim_all(_Req()))
    assert resp.status == 400
    assert _body(resp)["code"] == "nothing_to_claim"


def test_claim_all_without_distributor_seed_is_503(drip, monkeypatch):
    monkeypatch.setattr(server.config, "BRIX_DISTRIBUTOR_SEED", "", raising=False)
    _accrue(drip)
    resp = _run(server.handle_brix_claim_all(_Req()))
    assert resp.status == 503
    assert _body(resp)["code"] == "claims_disabled"


def test_claim_all_when_the_bucket_lookup_fails_is_503_not_a_solo_claim(drip, monkeypatch):
    """Fail-closed: silently claiming only the caller's wallet would read as
    'claim-all done' while linked balances quietly stay behind."""

    def boom(w):
        raise identity_store.BucketLookupError("db down")

    monkeypatch.setattr(identity_store, "bucket_for_wallet", boom)
    _accrue(drip)
    resp = _run(server.handle_brix_claim_all(_Req()))
    assert resp.status == 503
    assert _body(resp)["code"] == "bucket_unavailable"


def test_claim_all_unlinked_caller_still_claims_their_own_wallet(drip):
    _accrue(drip)

    async def go():
        body = _body(await server.handle_brix_claim_all(_Req()))
        await _drain_job(body["job_id"])
        return body

    body = _run(go())
    poll = _body(
        _run(server.handle_brix_claim_all_status(_Req(match_info={"job_id": body["job_id"]})))
    )
    assert [r["wallet"] for r in poll["wallets"]] == [WALLET]
    assert poll["wallets"][0]["status"] == "confirmed"


def test_claim_all_status_hides_another_wallets_job(drip, monkeypatch):
    _accrue(drip)

    async def go():
        body = _body(await server.handle_brix_claim_all(_Req()))
        await _drain_job(body["job_id"])
        return body

    body = _run(go())
    monkeypatch.setattr(server.mock_economy, "DEV_OWNER", "rSomeoneElse", raising=False)
    resp = _run(server.handle_brix_claim_all_status(_Req(match_info={"job_id": body["job_id"]})))
    assert resp.status == 404


def test_claim_all_status_unknown_job_is_404(drip):
    resp = _run(server.handle_brix_claim_all_status(_Req(match_info={"job_id": "nope"})))
    assert resp.status == 404


def test_claim_all_refuses_a_second_job_while_one_runs(drip, monkeypatch):
    """Two overlapping jobs would just trade claim_in_flight errors; refuse
    the second up front."""
    gate = asyncio.Event()

    async def slow_paid(destination, value, claim_id, max_last_ledger_seq=None):
        await gate.wait()
        return xrpl_ops.ClaimPayment("confirmed", f"TX_{destination}", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", slow_paid)
    _accrue(drip)

    async def go():
        body = _body(await server.handle_brix_claim_all(_Req()))
        resp2 = await server.handle_brix_claim_all(_Req())
        gate.set()
        await _drain_job(body["job_id"])
        return resp2

    resp2 = _run(go())
    assert resp2.status == 409
    assert _body(resp2)["code"] == "claim_all_in_flight"


# --- single-claim parity (the refactor must not change behavior) ----------


def test_single_claim_still_pays_out_after_the_refactor(drip):
    _accrue(drip)
    data = _body(_run(server.handle_brix_claim(_Req())))
    assert data["state"] == "confirmed"
    assert data["amount"] == 3


# --- trustline flow for a linked wallet (#446) ----------------------------


def _fake_trustline_payload(monkeypatch):
    created = {}

    async def create(wallet, currency, issuer, limit, user_token=None, platform=None):
        created["wallet"] = wallet
        return {
            "uuid": "UUID-1",
            "qr_url": "https://q",
            "xumm_url": "https://x",
            "pushed": False,
            "push": None,
        }

    monkeypatch.setattr(server.xumm_ops, "create_trustset_payload", create)
    return created


def test_trustline_post_accepts_a_linked_wallet(drip, monkeypatch):
    created = _fake_trustline_payload(monkeypatch)

    async def absent(*a, **k):
        return xrpl_ops.TrustlineState.ABSENT, None

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", absent)
    _link_bucket(WALLET, W_B)
    req = _Req(body={"wallet": W_B})
    resp = _run(server.handle_brix_trustline(req))
    assert _body(resp)["state"] == "pending"
    assert created["wallet"] == W_B


def test_trustline_post_refuses_a_wallet_outside_the_callers_bucket(drip, monkeypatch):
    _fake_trustline_payload(monkeypatch)
    _link_bucket(WALLET, W_B)  # W_C is NOT in the bucket
    req = _Req(body={"wallet": W_C})
    resp = _run(server.handle_brix_trustline(req))
    assert resp.status == 403
    assert _body(resp)["code"] == "not_linked"


def test_trustline_post_bucket_lookup_failure_is_503(drip, monkeypatch):
    _fake_trustline_payload(monkeypatch)

    def boom(w):
        raise identity_store.BucketLookupError("db down")

    monkeypatch.setattr(identity_store, "bucket_for_wallet", boom)
    req = _Req(body={"wallet": W_B})
    resp = _run(server.handle_brix_trustline(req))
    assert resp.status == 503
    assert _body(resp)["code"] == "bucket_unavailable"


def test_claim_all_two_overlapping_starts_cannot_both_mint_a_job(drip, monkeypatch):
    """The running-job scan sits before awaited bucket/claimable reads; without
    the start lock two overlapping POSTs both pass it and race duplicate jobs
    over the same wallets (Greptile P1 on #450)."""
    release = asyncio.Event()
    real_lookup = identity_store.bucket_for_wallet
    calls = {"n": 0}

    def slow_lookup(w):
        calls["n"] += 1
        return real_lookup(w)

    monkeypatch.setattr(identity_store, "bucket_for_wallet", slow_lookup)

    async def slow_paid(destination, value, claim_id, max_last_ledger_seq=None):
        await release.wait()
        return xrpl_ops.ClaimPayment("confirmed", f"TX_{destination}", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", slow_paid)
    _accrue(drip)

    async def go():
        # launch both POSTs concurrently — with the lock, exactly one job wins
        r1, r2 = await asyncio.gather(
            server.handle_brix_claim_all(_Req()),
            server.handle_brix_claim_all(_Req()),
        )
        release.set()
        for job in list(server.brix_claim_all_jobs.values()):
            task = job.get("task")
            if task is not None:
                await task
        return sorted([r1.status, r2.status])

    statuses = _run(go())
    assert statuses == [200, 409]
    assert len(server.brix_claim_all_jobs) == 1


def test_claim_all_a_bucket_sibling_cannot_start_an_overlapping_job(drip, monkeypatch):
    """The duplicate guard must compare wallet sets, not owners: two different
    wallets of one bucket would otherwise race per-wallet claims over the same
    balances (Greptile P1 round 2 on #450)."""
    release = asyncio.Event()

    async def slow_paid(destination, value, claim_id, max_last_ledger_seq=None):
        await release.wait()
        return xrpl_ops.ClaimPayment("confirmed", f"TX_{destination}", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", slow_paid)
    _link_bucket(WALLET, W_B)
    _accrue(drip)
    _accrue(drip, wallet=W_B, count=2)

    async def go():
        body = _body(await server.handle_brix_claim_all(_Req()))
        # the same human, authed as the sibling wallet, tries again mid-run
        monkeypatch.setattr(server.mock_economy, "DEV_OWNER", W_B, raising=False)
        resp2 = await server.handle_brix_claim_all(_Req())
        release.set()
        await _drain_job(body["job_id"])
        return resp2

    resp2 = _run(go())
    assert resp2.status == 409
    assert _body(resp2)["code"] == "claim_all_in_flight"
    assert len(server.brix_claim_all_jobs) == 1
