# tests/test_bulk_mint_service.py
# Task 10 (#215): service-layer wiring for bulk mint — start/status endpoints,
# per-user active-job lock, and a startup resume sweep.
#
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants at import time; set the same defaults test_bulk_mint_flow.py /
# test_smoke.py use so collection order can't strand them.
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import pytest  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import bulk_mint_flow, headroom, mint_flow, supply  # noqa: E402
from lfg_service import app as server  # noqa: E402


def _outstanding(tmp_path):
    """Total reserved+pending units in the fixture's hermetic headroom store
    (#226 review): pins the pre-launch release_job_headroom sites — a leaked
    reservation here would let one user exhaust all remaining headroom."""
    return headroom.outstanding(str(tmp_path / "hr.db"))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _post_request(path, body):
    """Mirrors tests/test_market_api.py's _post_request: stub request.json()
    since there's no full aiohttp TestClient fixture wired for these routes."""
    req = make_mocked_request("POST", path)

    async def _json():
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


class _StatusReq:
    """Minimal GET-status request stand-in (match_info + a settable per-request
    store), mirroring tests/test_market_api.py's _StatusReq."""

    headers: dict = {}

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self._store = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


@pytest.fixture
def dev_auth(monkeypatch, tmp_path):
    """require_auth/require_wallet in dev mode inject user {'id': 'dev'} and a
    fixed dev wallet address; isolate the module-level bulk_sessions dict and
    the durable job-record directory (the start handler now persists an
    AWAITING_PAYMENT record, #228)."""
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "bulk_sessions", {})
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    # Hermetic consumed-payment ledger (#228): cancel()'s claimed-payment
    # guard reads sqlite via config.DB_PATH.
    monkeypatch.setattr(bulk_mint_flow.config, "DB_PATH", str(tmp_path / "app.db"))
    # Hermetic headroom-reservation store (#226): clamp_to_headroom writes
    # the per-network app DB and try_reserve reads the index-backed supply.
    monkeypatch.setattr(
        bulk_mint_flow.db_path, "app_db_path", lambda net=None: str(tmp_path / "hr.db")
    )
    monkeypatch.setattr(supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(headroom.nft_index, "index_db_path", lambda net: str(tmp_path / "idx.db"))
    return server.bulk_sessions


def test_bulk_routes_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/mint/bulk" in paths
    assert "/api/mint/bulk/active" in paths
    assert "/api/mint/bulk/{session_id}" in paths


def test_bulk_route_registered_before_mint_session_wildcard():
    app = server.create_app()
    ordered = [
        getattr(r.resource, "canonical", "")
        for r in app.router.routes()
        if getattr(r.resource, "canonical", "").startswith("/api/mint/")
    ]
    # /api/mint/bulk(/active) must precede /api/mint/{session_id} or the
    # wildcard swallows "bulk" as a session id (aiohttp dispatches in
    # registration order).
    assert ordered.index("/api/mint/bulk") < ordered.index("/api/mint/{session_id}")
    assert ordered.index("/api/mint/bulk/active") < ordered.index("/api/mint/{session_id}")


def test_bulk_start_rejects_invalid_quantity(dev_auth):
    req = _post_request("/api/mint/bulk", {"quantity": 0})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 400
    import json as _json

    assert _json.loads(resp.body.decode())["error"] == "invalid_quantity"


@pytest.mark.parametrize(
    "bad_body",
    [
        {"quantity": True},
        {"quantity": 1.5},
        {"quantity": "3"},
        {},
    ],
    ids=["bool-true", "float", "string", "missing"],
)
def test_bulk_start_rejects_non_int_quantity(dev_auth, bad_body):
    req = _post_request("/api/mint/bulk", bad_body)
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 400
    import json as _json

    assert _json.loads(resp.body.decode())["error"] == "invalid_quantity"


def test_bulk_start_rejects_when_collection_full(dev_auth, monkeypatch):
    # #226: the clamp now grants against an atomic reservation; a full index
    # supply means try_reserve grants 0 -> CollectionFull -> 409.
    monkeypatch.setattr(supply, "current_supply", lambda net: server.config.MAX_COLLECTION_SIZE)
    req = _post_request("/api/mint/bulk", {"quantity": 5})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 409
    import json as _json

    payload = _json.loads(resp.body.decode())
    assert payload["error"] == "collection_full"


def test_bulk_status_not_found_for_unknown_session(dev_auth):
    resp = _run(server.handle_bulk_mint_status(_StatusReq("nope")))
    assert resp.status == 404


def test_bulk_active_returns_null_when_none(dev_auth):
    resp = _run(server.handle_bulk_mint_active(_StatusReq(None)))
    import json as _json

    assert _json.loads(resp.body.decode()) == {"session": None}


def test_bulk_active_returns_live_job(dev_auth):
    job = bulk_mint_flow.BulkMintJob(
        discord_id="dev", wallet_address="rTest", requested_qty=2, platform="discord"
    )
    dev_auth[job.id] = job
    resp = _run(server.handle_bulk_mint_active(_StatusReq(None)))
    import json as _json

    body = _json.loads(resp.body.decode())
    assert body["session"]["id"] == job.id


def test_bulk_start_inserts_before_awaiting_prepare_payment(dev_auth, monkeypatch):
    """Regression for the active-job race: the job must land in bulk_sessions
    BEFORE prepare_payment() is awaited, so a concurrent request racing in
    right after the insert sees this job as active. If the insert happened
    only after prepare_payment (the bug), a second request that starts while
    the first is still awaiting prepare_payment would see an empty
    bulk_sessions dict and slip past the active-job guard."""
    seen_in_sessions_during_prepare = {}

    async def spy_prepare_payment(self):
        # At the moment prepare_payment runs, the job must already be
        # registered under its own id in bulk_sessions.
        seen_in_sessions_during_prepare["present"] = dev_auth.get(self.id) is self
        # Fake a successful prepare: the real one has no XUMM in tests and
        # would produce a link-less job, which the handler now fails closed.
        self.pay_amount = "10"
        self.payment_link = "https://xumm/pay"

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", spy_prepare_payment)

    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))

    assert seen_in_sessions_during_prepare.get("present") is True
    assert resp.status == 200
    import json as _json

    body = _json.loads(resp.body.decode())
    assert body["id"] in dev_auth


def test_bulk_start_second_concurrent_request_is_rejected(dev_auth, monkeypatch, tmp_path):
    """End-to-end race simulation: while the first request's prepare_payment
    is still in flight (i.e. after the insert but before it returns), a
    second start request for the same user must be rejected 409 rather than
    creating a second concurrent bulk job."""
    second_response = {}

    async def interleaving_prepare_payment(self):
        # Simulate a concurrent second request arriving while the first is
        # suspended awaiting prepare_payment — at this point the first job
        # must already be visible in bulk_sessions for the guard to work.
        second_req = _post_request("/api/mint/bulk", {"quantity": 1})
        second_response["resp"] = await server.handle_bulk_mint_start(second_req)
        self.pay_amount = "10"
        self.payment_link = "https://xumm/pay"

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", interleaving_prepare_payment)

    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))

    assert resp.status == 200
    assert second_response["resp"].status == 409
    import json as _json

    assert _json.loads(second_response["resp"].body.decode())["error"] == (
        "bulk mint already in progress"
    )
    # Only one job for this user should have been registered.
    assert len(dev_auth) == 1
    # #226 review: the rejected request's clamp reserved headroom under a
    # fresh bulk:<id> claimant before losing the active check — the 409
    # branch MUST release it. Only the first job's 1-unit grant may remain;
    # without the release, spamming POST /api/mint/bulk during an active job
    # leaks BULK_MINT_MAX units per attempt until the collection reads full.
    assert _outstanding(tmp_path) == 1


def test_bulk_start_prepare_payment_failure_frees_slot(dev_auth, monkeypatch, tmp_path):
    """If prepare_payment raises, the job must not wedge the user's bulk slot
    forever: it must end up terminal (or evicted) so a follow-up start does
    NOT 409."""

    async def _boom(self):
        raise RuntimeError("xumm is down")

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _boom)

    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status >= 500
    # #226 review: the failed job is terminal before launch — its clamp-time
    # reservation must be released, or every attempt during a XUMM outage
    # leaks a unit of headroom until the next restart.
    assert _outstanding(tmp_path) == 0

    # The user's slot must be free: a follow-up start must not 409.
    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _fake_prepare_ok())
    req2 = _post_request("/api/mint/bulk", {"quantity": 1})
    resp2 = _run(server.handle_bulk_mint_start(req2))
    assert resp2.status == 200


async def _noop():
    return None


def test_bulk_start_prepare_payment_timeout_frees_slot(dev_auth, monkeypatch, tmp_path):
    """A hung XUMM call must not hang the request forever: prepare_payment is
    bounded (mirrors the single-mint path's asyncio.wait_for(..., timeout=8)).
    On timeout the job is marked FAILED (frees the slot) and the request
    returns payment_setup_failed, same as any other prepare_payment error."""

    async def _hang(self):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _hang)

    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 500
    import json as _json

    assert _json.loads(resp.body.decode())["error"] == "payment_setup_failed"
    # #226 review: terminal-before-launch must release the reservation.
    assert _outstanding(tmp_path) == 0

    # Slot must be free: a follow-up start must not 409.
    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _fake_prepare_ok())
    req2 = _post_request("/api/mint/bulk", {"quantity": 1})
    resp2 = _run(server.handle_bulk_mint_start(req2))
    assert resp2.status == 200


def _fake_prepare_ok():
    async def _prep(self):
        self.pay_with, self.unit_price = "XRP", "10"
        self.pay_amount = "10"
        self.payment_link, self.payment_uuid = "https://xumm.app/sign/u1", "u1"

    return _prep


def test_bulk_start_persists_awaiting_payment_record(dev_auth, monkeypatch):
    """#228: once prepare_payment succeeds the job must be durable — a crash
    after the user was shown (and maybe signed) the payment request resumes
    the ledger watch instead of taking money with no record."""
    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _fake_prepare_ok())

    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 200
    import json as _json

    body = _json.loads(resp.body.decode())
    resumable = bulk_mint_flow.load_all_resumable()
    assert [r.id for r in resumable] == [body["id"]]
    assert resumable[0].state == bulk_mint_flow.AWAITING_PAYMENT
    assert resumable[0].payment_uuid == "u1"
    assert resumable[0].payment_link == "https://xumm.app/sign/u1"


def test_bulk_start_prepare_failure_leaves_no_record(dev_auth, monkeypatch, tmp_path):
    """A failed start frees the slot AND leaves no zombie job file for the
    startup sweep to resurrect (#228). The stub persists BEFORE raising so
    the handler's delete_record is load-bearing (mutation-detectable), not
    vacuously green because no record ever existed on the failure path."""

    async def _boom(self):
        bulk_mint_flow.persist(self)  # simulate a flow that persisted, then died
        raise RuntimeError("xumm is down")

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _boom)

    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 500
    assert bulk_mint_flow.load_all_resumable() == []
    assert list(tmp_path.glob("*.json")) == []


def test_bulk_active_payment_link_null_while_preparing(dev_auth):
    """Contract (#228 / BulkMintJob.to_dict): the job is registered in
    bulk_sessions BEFORE prepare_payment finishes (race-free active-guard
    ordering), so /active may return an AWAITING_PAYMENT session whose
    payment_link is null — meaning "still preparing, keep polling", not an
    error."""
    job = bulk_mint_flow.BulkMintJob(
        discord_id="dev", wallet_address="rTest", requested_qty=1, platform="discord"
    )
    dev_auth[job.id] = job  # as inserted pre-prepare_payment
    resp = _run(server.handle_bulk_mint_active(_StatusReq(None)))
    import json as _json

    body = _json.loads(resp.body.decode())
    assert body["session"]["state"] == bulk_mint_flow.AWAITING_PAYMENT
    assert body["session"]["payment_link"] is None


def test_bulk_cancel_route_registered():
    app = server.create_app()
    method_paths = {(r.method, getattr(r.resource, "canonical", "")) for r in app.router.routes()}
    assert ("POST", "/api/mint/bulk/{session_id}/cancel") in method_paths


def test_bulk_cancel_awaiting_payment_job(dev_auth):
    job = bulk_mint_flow.BulkMintJob(
        discord_id="dev", wallet_address="rTest", requested_qty=1, platform="discord"
    )
    dev_auth[job.id] = job
    resp = _run(server.handle_bulk_mint_cancel(_StatusReq(job.id)))
    assert resp.status == 200
    import json as _json

    body = _json.loads(resp.body.decode())
    assert body["state"] == bulk_mint_flow.CANCELLED
    assert job.state == bulk_mint_flow.CANCELLED


def test_bulk_cancel_not_found(dev_auth):
    resp = _run(server.handle_bulk_mint_cancel(_StatusReq("nope")))
    assert resp.status == 404


def test_bulk_cancel_during_prepare_never_fulfills(dev_auth, monkeypatch, tmp_path):
    """Cancel racing the start handler's prepare_payment await (the job id is
    reachable via /active in that window, task still None) must never let the
    job fall through to fulfillment with zero payment confirmed: the handler
    re-checks state post-prepare and run_bulk_mint_job refuses terminal
    states, so no record is persisted, no watch is launched, nothing mints."""
    calls = {"wait": 0, "mint": 0}

    async def _wait(**kw):
        calls["wait"] += 1
        return True

    async def _mint(**kw):
        calls["mint"] += 1
        raise AssertionError("must never mint")

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _wait)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _mint)

    cancel_result = {}

    async def _prepare_with_concurrent_cancel(self):
        # Concurrent POST /api/mint/bulk/{id}/cancel lands while the start
        # handler is suspended awaiting prepare_payment.
        cancel_result["resp"] = await server.handle_bulk_mint_cancel(_StatusReq(self.id))
        self.pay_with, self.unit_price = "XRP", "10"
        self.pay_amount = "20"
        self.payment_link, self.payment_uuid = "https://xumm.app/sign/u1", "u1"

    monkeypatch.setattr(
        bulk_mint_flow.BulkMintJob, "prepare_payment", _prepare_with_concurrent_cancel
    )

    req = _post_request("/api/mint/bulk", {"quantity": 2})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 200
    import json as _json

    body = _json.loads(resp.body.decode())
    assert cancel_result["resp"].status == 200
    assert body["state"] == bulk_mint_flow.CANCELLED

    job = dev_auth[body["id"]]
    assert job.task is None  # fulfillment task never launched
    assert calls == {"wait": 0, "mint": 0}
    assert bulk_mint_flow.load_all_resumable() == []  # no resurrectable record
    assert list(tmp_path.glob("*.json")) == []


def test_bulk_start_fails_closed_without_payment_link_or_persist(dev_auth, monkeypatch, tmp_path):
    """Before money moves, fail-closed is free: a start whose prepare finished
    without a usable payment_link — or whose durable record could not be
    written — must 500 and clean up, never show an orphanable payment
    request or launch the watch."""

    async def _prepare_no_link(self):
        self.pay_amount = "10"  # ran "successfully" but produced no link

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _prepare_no_link)
    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 500
    assert bulk_mint_flow.load_all_resumable() == []
    # #226 review: the payment_setup_failed branch must release the clamp-time
    # reservation (terminal before launch).
    assert _outstanding(tmp_path) == 0

    async def _prepare_ok(self):
        self.pay_amount = "10"
        self.payment_link = "https://xumm/pay"

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _prepare_ok)
    monkeypatch.setattr(bulk_mint_flow, "persist", lambda job: False)
    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 500
    assert _outstanding(tmp_path) == 0


def test_resume_bulk_jobs_rebuilds_headroom_and_relaunches(dev_auth, monkeypatch, tmp_path):
    """#226 review: the startup sweep's headroom.rebuild wiring was previously
    exercised by zero tests (deleting the rebuild call left the whole suite
    green) — yet it is the ONLY thing that ever prunes crash-orphan rows.
    End-to-end through server.resume_bulk_jobs: orphan mint:*/bulk:* rows from
    a dead process are dropped, a live resumable job's reservation is
    re-asserted (with its minted unit re-asserted as pending), keep-set
    claimants (live in-memory sessions) survive, and the job is relaunched."""
    hr = str(tmp_path / "hr.db")
    net = server.config.XRPL_NETWORK

    # Orphans from a dead process (no record, no live session).
    assert headroom.try_reserve(hr, "mint:dead", 1, net) == 1
    assert headroom.try_reserve(hr, "bulk:dead", 3, net) == 3

    # A live in-memory single-mint session (keep-set claimant).
    monkeypatch.setattr(server, "mint_sessions", {})
    live = mint_flow.MintSession(discord_id="dev", wallet_address="rTest")
    server.mint_sessions[live.id] = live
    assert headroom.try_reserve(hr, f"mint:{live.id}", 1, net) == 1

    # A durable resumable job: paid, one unit minted (on-chain, maybe not yet
    # indexed), one unit still pending fulfillment.
    job = bulk_mint_flow.BulkMintJob("dev", "rTest", 2, platform="discord")
    job.clamp_to_headroom()  # reserves 2 under bulk:<id>
    job.state = bulk_mint_flow.PAID
    job.units[0].state = bulk_mint_flow.MINTED
    job.units[0].nft_id = "NFT-RESUMED"
    assert bulk_mint_flow.persist(job)

    launched = []

    async def _no_run(j):
        launched.append(j.id)

    monkeypatch.setattr(bulk_mint_flow, "run_bulk_mint_job", _no_run)

    async def _drive():
        await server.resume_bulk_jobs()
        await asyncio.sleep(0)  # let the relaunched job task run its stub

    _run(_drive())

    assert headroom.reserved_for(hr, "mint:dead") == 0  # orphan dropped
    assert headroom.reserved_for(hr, "bulk:dead") == 0  # orphan dropped
    assert headroom.reserved_for(hr, f"mint:{live.id}") == 1  # keep survived
    assert headroom.reserved_for(hr, f"bulk:{job.id}") == 1  # unminted unit
    # outstanding = live mint (1) + job's unminted unit (1) + the minted
    # unit re-asserted as a pending row (1).
    assert headroom.outstanding(hr) == 3
    assert launched == [job.id]
    assert job.id in dev_auth  # re-attached to bulk_sessions
