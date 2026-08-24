"""Service endpoints for the BRIX daily drip (#48).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

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
    server.brix_trustline_payloads.clear()
    path = str(tmp_path / "history.db")
    monkeypatch.setattr(history_store, "history_db_path", lambda net=None: path)
    conn = history_store.init_history_db(path)
    brix_drip.ensure_schema(conn)

    # Claims never touch the network in these tests.
    async def ok_trustline(*a, **k):
        return xrpl_ops.TrustlineState.PRESENT, Decimal(100)

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", ok_trustline)

    async def paid(destination, value, claim_id, max_last_ledger_seq=None):
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
        return xrpl_ops.TrustlineState.ABSENT, None

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", no_line)
    _accrue(drip)
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 409
    assert _body(resp)["code"] == "trustline_required"
    # Nothing was bound, so the balance is intact and retryable.
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


def test_post_claim_when_trustline_lookup_fails_is_503_not_a_trustline_verdict(drip, monkeypatch):
    # A transient account_lines failure must NOT read as "no trustline": the
    # client pins the claim button on trustline_required, so a false verdict
    # would lock a real holder out of their payout. Report it as unavailable.
    async def lookup_failed(*a, **k):
        return xrpl_ops.TrustlineState.UNKNOWN, None

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", lookup_failed)
    _accrue(drip)
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 503
    assert _body(resp)["code"] == "claim_unavailable"
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


def test_post_claim_with_nothing_accrued_is_400(drip):
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 400
    assert _body(resp)["code"] == "nothing_to_claim"


def test_post_claim_while_one_is_in_flight_is_409(drip, monkeypatch):
    async def unknown(destination, value, claim_id, max_last_ledger_seq=None):
        return xrpl_ops.ClaimPayment("unknown", None, 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", unknown)
    _accrue(drip)
    _run(server.handle_brix_claim(_Req()))

    _accrue(drip, count=1, epoch="2026-08-19")
    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 409
    assert _body(resp)["code"] == "claim_in_flight"


def test_a_definitively_failed_payout_restores_the_balance(drip, monkeypatch):
    async def failed(destination, value, claim_id, max_last_ledger_seq=None):
        return xrpl_ops.ClaimPayment("failed", None, 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", failed)
    _accrue(drip)
    data = _body(_run(server.handle_brix_claim(_Req())))
    assert data["state"] == "failed"
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


def test_an_unknown_payout_keeps_the_balance_bound(drip, monkeypatch):
    """The ambiguous window: the payment may have landed, so the BRIX must NOT
    come back — that would let it be claimed twice."""

    async def unknown(destination, value, claim_id, max_last_ledger_seq=None):
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

    async def cannot_submit(destination, value, claim_id, max_last_ledger_seq=None):
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

    async def mystery(destination, value, claim_id, max_last_ledger_seq=None):
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

    async def mystery(destination, value, claim_id, max_last_ledger_seq=None):
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

    async def cancelled(destination, value, claim_id, max_last_ledger_seq=None):
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

    async def must_not_run(destination, value, claim_id, max_last_ledger_seq=None):
        raise AssertionError("no payout may be attempted without a deadline")

    monkeypatch.setattr(xrpl_ops, "current_validated_ledger_index", unreadable)
    monkeypatch.setattr(xrpl_ops, "send_brix_claim", must_not_run)
    _accrue(drip)

    resp = _run(server.handle_brix_claim(_Req()))
    assert resp.status == 503
    assert drip.execute("SELECT COUNT(*) FROM brix_claims").fetchone()[0] == 0
    assert _body(_run(server.handle_brix_status(_Req())))["claimable"] == 3


# --- get_trustline_state tri-state (PR #440) -----------------------------------


class _WsResp:
    def __init__(self, result, ok=True):
        self.result = result
        self._ok = ok

    def is_successful(self):
        return self._ok


def _fake_ws(monkeypatch, responses):
    class _Ws:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, req):
            return responses.pop(0)

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _Ws)


def _state(monkeypatch, responses):
    _fake_ws(monkeypatch, responses)
    return _run(xrpl_ops.get_trustline_state("rW", "BRIX", "rIssuer"))


def test_trustline_state_present_with_balance(monkeypatch):
    resp = _WsResp({"lines": [{"currency": "BRIX", "account": "rIssuer", "balance": "12.5"}]})
    assert _state(monkeypatch, [resp]) == (xrpl_ops.TrustlineState.PRESENT, Decimal("12.5"))


def test_trustline_state_absent_after_a_full_page_walk(monkeypatch):
    pages = [
        _WsResp({"lines": [{"currency": "OTHER", "account": "rIssuer"}], "marker": "m1"}),
        _WsResp({"lines": []}),
    ]
    assert _state(monkeypatch, pages) == (xrpl_ops.TrustlineState.ABSENT, None)


def test_trustline_state_error_response_is_unknown_not_absent(monkeypatch):
    """Greptile P1 on PR #440: xrpl-py returns (not raises) tooBusy & co. — a
    result with no `lines`/`marker` must not read as an exhausted walk."""
    resp = _WsResp({"error": "tooBusy", "status": "error"}, ok=False)
    assert _state(monkeypatch, [resp]) == (xrpl_ops.TrustlineState.UNKNOWN, None)


def test_trustline_state_transport_failure_is_unknown(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise ConnectionError("down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _Boom)
    assert _run(xrpl_ops.get_trustline_state("rW", "BRIX", "rI")) == (
        xrpl_ops.TrustlineState.UNKNOWN,
        None,
    )


# --- BRIX trustline flow (#441) -----------------------------------------------


def _trustline_state(monkeypatch, state):
    async def fake(*a, **k):
        return state, (Decimal(1) if state is xrpl_ops.TrustlineState.PRESENT else None)

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", fake)


def _fake_payload(monkeypatch, captured):
    async def create(account, currency, issuer, limit, **kw):
        captured.update(account=account, currency=currency, issuer=issuer, limit=limit, **kw)
        return {"uuid": "u-1", "qr_url": "q", "xumm_url": "x", "pushed": False, "push": None}

    monkeypatch.setattr(server.xumm_ops, "create_trustset_payload", create)


def test_trustline_start_builds_a_signer_pinned_brix_trustset(drip, monkeypatch):
    _trustline_state(monkeypatch, xrpl_ops.TrustlineState.ABSENT)
    captured = {}
    _fake_payload(monkeypatch, captured)
    monkeypatch.setattr(server.config, "BRIX_TRUSTLINE_LIMIT", "1000000000", raising=False)
    resp = _run(server.handle_brix_trustline(_Req()))
    assert resp.status == 200
    data = _body(resp)
    assert data["state"] == "pending"
    assert data["uuid"] == "u-1"
    assert data["xumm_url"] == "x"
    # _create_xumm_payload's key is qr_url; the wire field is qr_png (CR on #442).
    assert data["qr_png"] == "q"
    assert captured["account"] == WALLET
    assert captured["currency"] == server.config.BRIX_CURRENCY_HEX
    assert captured["issuer"] == server.config.BRIX_ISSUER
    assert captured["limit"] == "1000000000"


def test_trustline_start_is_a_noop_when_the_line_already_exists(drip, monkeypatch):
    _trustline_state(monkeypatch, xrpl_ops.TrustlineState.PRESENT)
    captured = {}
    _fake_payload(monkeypatch, captured)
    data = _body(_run(server.handle_brix_trustline(_Req())))
    assert data["state"] == "already_set"
    assert captured == {}  # nothing built


def test_trustline_start_builds_anyway_when_the_lookup_fails(drip, monkeypatch):
    # Unlike the claim path, nothing is bound here and the user explicitly
    # asked for the line; a redundant TrustSet on an existing line is harmless.
    _trustline_state(monkeypatch, xrpl_ops.TrustlineState.UNKNOWN)
    captured = {}
    _fake_payload(monkeypatch, captured)
    assert _body(_run(server.handle_brix_trustline(_Req())))["state"] == "pending"
    assert captured["account"] == WALLET


def test_trustline_start_503_when_xumm_is_unavailable(drip, monkeypatch):
    _trustline_state(monkeypatch, xrpl_ops.TrustlineState.ABSENT)

    async def none(*a, **k):
        return None

    monkeypatch.setattr(server.xumm_ops, "create_trustset_payload", none)
    resp = _run(server.handle_brix_trustline(_Req()))
    assert resp.status == 503
    assert _body(resp)["code"] == "signing_unavailable"


def _started(drip, monkeypatch):
    _trustline_state(monkeypatch, xrpl_ops.TrustlineState.ABSENT)
    _fake_payload(monkeypatch, {})
    return _body(_run(server.handle_brix_trustline(_Req())))["uuid"]


def _status(monkeypatch, **s):
    base = {
        "opened": False,
        "signed": False,
        "expired": False,
        "account": None,
        "txid": None,
        "user_token": None,
    }
    base.update(s)

    async def fake(uuid, **k):
        return base

    monkeypatch.setattr(server.xumm_ops, "get_payload_status", fake)


def _tx(monkeypatch, *, validated, result=None, raise_=None):
    async def get_tx(txid):
        if raise_:
            raise raise_
        return {"validated": True, "meta": {"TransactionResult": result}} if validated else {}

    monkeypatch.setattr(xrpl_ops, "get_tx", get_tx)


def test_trustline_status_unknown_uuid_is_404(drip):
    resp = _run(server.handle_brix_trustline_status(_Req(match_info={"uuid": "nope"})))
    assert resp.status == 404


def test_trustline_status_pending_then_signed_recaptures_push_token(drip, monkeypatch):
    uuid = _started(drip, monkeypatch)
    _status(monkeypatch)
    assert (
        _body(_run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))))["state"]
        == "pending"
    )

    saved = {}
    monkeypatch.setattr(
        server.identity_store, "set_user_token", lambda p, u, t: saved.update(p=p, u=u, t=t)
    )
    _status(monkeypatch, signed=True, account=WALLET, txid="TX1", user_token="tok-new")
    _tx(monkeypatch, validated=True, result="tesSUCCESS")
    data = _body(_run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))))
    assert data == {"state": "signed", "tx_hash": "TX1"}
    assert saved["t"] == "tok-new"
    # Single-use: the record is gone once terminal.
    assert _run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))).status == 404


def test_trustline_status_wrong_signer_is_rejected_and_persists_nothing(drip, monkeypatch):
    uuid = _started(drip, monkeypatch)
    saved = {}
    monkeypatch.setattr(server.identity_store, "set_user_token", lambda *a: saved.update(hit=True))
    _status(monkeypatch, signed=True, account="rSomeoneElse", txid="TX1", user_token="tok")
    data = _body(_run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))))
    assert data == {"state": "rejected", "code": "signer_mismatch"}
    assert saved == {}


def test_trustline_status_expired(drip, monkeypatch):
    uuid = _started(drip, monkeypatch)
    _status(monkeypatch, expired=True)
    assert (
        _body(_run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))))["state"]
        == "expired"
    )


def test_trustline_status_is_owner_scoped(drip, monkeypatch):
    """Keyed by (platform, user_id) like signin_payloads: another caller
    holding the uuid cannot read or complete it."""
    uuid = _started(drip, monkeypatch)
    _status(monkeypatch)
    server.brix_trustline_payloads[uuid]["user_id"] = "someone-else"
    assert _run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))).status == 404


def test_trustline_status_signed_but_unvalidated_keeps_polling(drip, monkeypatch):
    """Signed is not set (Greptile P1 on #442): only a validated tesSUCCESS
    clears the record; until then the client keeps polling."""
    uuid = _started(drip, monkeypatch)
    monkeypatch.setattr(server.identity_store, "set_user_token", lambda *a: None)
    _status(monkeypatch, signed=True, account=WALLET, txid="TX1")
    _tx(monkeypatch, validated=False)
    data = _body(_run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))))
    assert data == {"state": "validating", "tx_hash": "TX1"}
    assert uuid in server.brix_trustline_payloads
    # A lookup blip is not a verdict either.
    _tx(monkeypatch, validated=False, raise_=RuntimeError("rpc down"))
    data = _body(_run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))))
    assert data["state"] == "validating"
    assert uuid in server.brix_trustline_payloads


def test_trustline_status_validated_failure_is_rejected_tx_failed(drip, monkeypatch):
    uuid = _started(drip, monkeypatch)
    monkeypatch.setattr(server.identity_store, "set_user_token", lambda *a: None)
    _status(monkeypatch, signed=True, account=WALLET, txid="TX1")
    _tx(monkeypatch, validated=True, result="tecNO_PERMISSION")
    data = _body(_run(server.handle_brix_trustline_status(_Req(match_info={"uuid": uuid}))))
    assert data == {"state": "rejected", "code": "tx_failed", "tx_result": "tecNO_PERMISSION"}
    assert uuid not in server.brix_trustline_payloads


def test_trustline_records_are_pruned_by_ttl(drip, monkeypatch):
    """Abandoned flows must not accumulate for the process lifetime."""
    _started(drip, monkeypatch)
    stale = server.brix_trustline_payloads.pop("u-1")
    stale["user_id"] = "someone-else"  # not the caller's, so no reuse
    stale["created_at"] -= server.BRIX_TRUSTLINE_TTL + 1
    server.brix_trustline_payloads["u-stale"] = stale
    assert _run(server.handle_brix_trustline(_Req())).status == 200
    assert "u-stale" not in server.brix_trustline_payloads
    assert "u-1" in server.brix_trustline_payloads


def test_trustline_start_reuses_the_callers_live_payload(drip, monkeypatch):
    """Back-then-Retry hands the still-live request back instead of minting a
    second Xaman payload (#260 open-payload cap; Greptile on #442)."""
    uuid = _started(drip, monkeypatch)
    calls = []

    async def must_not_create(*a, **k):
        calls.append(1)

    monkeypatch.setattr(server.xumm_ops, "create_trustset_payload", must_not_create)
    data = _body(_run(server.handle_brix_trustline(_Req())))
    assert data["state"] == "pending" and data["uuid"] == uuid
    assert data["qr_png"] == "q" and data["xumm_url"] == "x"
    assert calls == []
    assert len(server.brix_trustline_payloads) == 1


def test_trustline_concurrent_starts_share_one_payload(drip, monkeypatch):
    """Overlapping starts for one caller serialize on the start lock, so the
    second sees the first's record and reuses it (Greptile on #442)."""
    _trustline_state(monkeypatch, xrpl_ops.TrustlineState.ABSENT)
    calls = []

    async def slow_create(*a, **k):
        calls.append(1)
        await asyncio.sleep(0.05)
        return {"uuid": f"u-{len(calls)}", "qr_url": "q", "xumm_url": "x"}

    monkeypatch.setattr(server.xumm_ops, "create_trustset_payload", slow_create)

    async def both():
        return await asyncio.gather(
            server.handle_brix_trustline(_Req()), server.handle_brix_trustline(_Req())
        )

    a, b = _run(both())
    assert _body(a)["uuid"] == _body(b)["uuid"] == "u-1"
    assert calls == [1]
    assert not server._brix_trustline_lock.locked()


def test_trustline_start_does_not_reuse_another_wallets_payload(drip, monkeypatch):
    """Re-registering to wallet B mid-flow must not hand back wallet A's
    signer-pinned payload (CodeRabbit on #442)."""
    uuid_a = _started(drip, monkeypatch)
    server.brix_trustline_payloads[uuid_a]["wallet"] = "rWalletA"
    captured = {}
    _fake_payload(monkeypatch, captured)
    data = _body(_run(server.handle_brix_trustline(_Req())))
    assert data["state"] == "pending"
    assert captured["account"] == WALLET  # a fresh payload for the current wallet
