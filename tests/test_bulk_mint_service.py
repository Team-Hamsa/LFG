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
import json  # noqa: E402

import pytest  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import bulk_mint_flow  # noqa: E402
from lfg_service import app as server  # noqa: E402


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
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 0)
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


def test_bulk_start_second_concurrent_request_is_rejected(dev_auth, monkeypatch):
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


def test_bulk_start_prepare_payment_failure_frees_slot(dev_auth, monkeypatch):
    """If prepare_payment raises, the job must not wedge the user's bulk slot
    forever: it must end up terminal (or evicted) so a follow-up start does
    NOT 409."""

    async def _boom(self):
        raise RuntimeError("xumm is down")

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _boom)

    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status >= 500

    # The user's slot must be free: a follow-up start must not 409.
    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _fake_prepare_ok())
    req2 = _post_request("/api/mint/bulk", {"quantity": 1})
    resp2 = _run(server.handle_bulk_mint_start(req2))
    assert resp2.status == 200


async def _noop():
    return None


def test_bulk_start_prepare_payment_timeout_frees_slot(dev_auth, monkeypatch):
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

    async def _prepare_ok(self):
        self.pay_amount = "10"
        self.payment_link = "https://xumm/pay"

    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", _prepare_ok)
    monkeypatch.setattr(bulk_mint_flow, "persist", lambda job: False)
    req = _post_request("/api/mint/bulk", {"quantity": 1})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 500


class _AcceptReq(_StatusReq):
    def __init__(self, session_id, index):
        super().__init__(session_id)
        self.match_info = {"session_id": session_id, "index": index}

    async def json(self):
        return {}


def _offered_job(sessions):
    job = bulk_mint_flow.BulkMintJob(
        discord_id="dev", wallet_address="rDEV", requested_qty=2, platform="discord"
    )
    job.quantity = 2
    job.units = [bulk_mint_flow.Unit(index=0), bulk_mint_flow.Unit(index=1)]
    job.units[0].state = bulk_mint_flow.OFFERED
    job.units[0].offer_id = "OFFERIDX0"
    job.state = bulk_mint_flow.FULFILLING
    sessions[job.id] = job
    return job


def test_bulk_unit_accept_route_registered():
    routes = {
        (r.method, r.resource.canonical)
        for r in server.create_app().router.routes()
        if r.resource is not None
    }
    assert ("POST", "/api/mint/bulk/{session_id}/units/{index}/accept") in routes


def test_bulk_unit_accept_happy_path(dev_auth, monkeypatch):
    job = _offered_job(dev_auth)
    seen = {}

    async def fake_payload(offer_id, return_url=None, user_token=None, platform=None, **kw):
        seen.update(offer_id=offer_id, user_token=user_token, platform=platform)
        return {"qr_url": "QR", "xumm_url": "LINK", "uuid": "U", "pushed": False, "push": None}

    monkeypatch.setattr(server.xumm_ops, "create_accept_offer_payload", fake_payload)
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "0")))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["link"] == "LINK" and body["qr"] == "QR"
    assert seen["offer_id"] == "OFFERIDX0"
    assert seen["platform"] == server.memos.platform_for_surface("discord")


def test_bulk_unit_accept_rejects_non_offered_unit(dev_auth):
    job = _offered_job(dev_auth)
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "1")))  # unit 1 is PENDING
    assert resp.status == 409


def test_bulk_unit_accept_rejects_bad_index(dev_auth):
    job = _offered_job(dev_auth)
    for bad in ("7", "-1", "zero"):
        resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, bad)))
        assert resp.status == 400, bad


def test_bulk_unit_accept_unknown_job_404(dev_auth):
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq("nope", "0")))
    assert resp.status == 404


def test_bulk_unit_accept_already_claimed_unit_409(dev_auth):
    # offered with offer_id None = gift offer already accepted while the
    # service was down (see BulkMintJob.to_dict contract) — nothing to sign.
    job = _offered_job(dev_auth)
    job.units[0].offer_id = None
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "0")))
    assert resp.status == 409


def test_bulk_unit_accept_payload_failure_502(dev_auth, monkeypatch):
    job = _offered_job(dev_auth)

    async def none_payload(*a, **kw):
        return None

    monkeypatch.setattr(server.xumm_ops, "create_accept_offer_payload", none_payload)
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "0")))
    assert resp.status == 502
