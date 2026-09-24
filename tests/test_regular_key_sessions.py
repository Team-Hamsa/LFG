"""RegularKey session revocation (agent users spec §2, Amendment 1)."""

import asyncio
import json

import pytest

import lfg_service.app as app
from lfg_core.signing.key_authority import LOOKUP_FAILED, KeyAuthority
from lfg_service import identity as identity_store

ACCOUNT, SIGNER, OTHER = "rACCOUNT", "rSIGNER", "rOTHER"
REGULAR = {"key": "regular", "signer": SIGNER}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query = query or {}
        self._s: dict = {}

    def __getitem__(self, k):
        return self._s[k]

    def __setitem__(self, k, v):
        self._s[k] = v


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(identity_store, "DATABASE", str(tmp_path / "identity.db"))
    identity_store.ensure_identities_table()
    identity_store.ensure_revoked_sessions_table()
    app._revoked_sessions.clear()
    app._regular_keys.clear()
    app._regular_key_failed_at.clear()
    yield
    app._revoked_sessions.clear()
    app._regular_keys.clear()
    app._regular_key_failed_at.clear()


@pytest.fixture
def ledger(monkeypatch):
    state = {"authority": KeyAuthority(SIGNER, False, True), "calls": 0, "addresses": []}

    async def _lookup(address):
        state["calls"] += 1
        state["addresses"].append(address)
        return state["authority"]

    monkeypatch.setattr(app.xrpl_ops, "key_authority", _lookup)
    return state


def _token(**claims):
    return app.make_session_token(
        {"id": ACCOUNT, "name": "n", "platform": "web", "provider": "walletconnect", **claims}
    )


@app.require_auth
async def _handler(request):
    return app.web.json_response({"ok": True})


def _call(token):
    resp = _run(_handler(_Req({"Authorization": f"Bearer {token}"})))
    return resp.status, json.loads(resp.text)


def test_master_key_session_never_reads_the_ledger(ledger):
    assert _call(_token())[0] == 200
    assert ledger["calls"] == 0


def test_regular_key_session_is_rechecked_at_most_once_a_minute(ledger):
    tok = _token(**REGULAR)
    assert _call(tok)[0] == 200 and _call(tok)[0] == 200
    assert ledger["calls"] == 1
    assert ledger["addresses"] == [ACCOUNT]  # revocation looks up the session wallet
    checked, key = app._regular_keys[ACCOUNT]
    app._regular_keys[ACCOUNT] = (checked - app.REGULAR_KEY_RECHECK_SECONDS - 1, key)
    assert _call(tok)[0] == 200 and ledger["calls"] == 2


@pytest.mark.parametrize("now_on_ledger", [None, OTHER])  # removed, rotated
def test_a_removed_or_rotated_key_revokes_the_session(ledger, now_on_ledger):
    tok = _token(**REGULAR)
    ledger["authority"] = KeyAuthority(now_on_ledger, False, True)
    status, body = _call(tok)
    assert status == 401 and body["code"] == "key_revoked"
    assert app.verify_session_token(tok) is None  # denylisted
    ledger["authority"] = KeyAuthority(SIGNER, False, True)
    assert _call(tok)[0] == 401  # stays revoked if the key comes back


def test_a_lookup_error_keeps_the_last_answer(ledger):
    tok = _token(**REGULAR)
    assert _call(tok)[0] == 200
    checked, key = app._regular_keys[ACCOUNT]
    app._regular_keys[ACCOUNT] = (checked - 3600, key)
    ledger["authority"] = LOOKUP_FAILED
    assert _call(tok)[0] == 200


def test_a_lookup_error_with_no_answer_fails_closed(ledger):
    ledger["authority"] = LOOKUP_FAILED
    status, body = _call(_token(**REGULAR))
    assert status == 503 and body["code"] == "key_unverified"


def test_a_failed_lookup_is_not_retried_within_the_backoff_window(ledger):
    ledger["authority"] = LOOKUP_FAILED
    tok = _token(**REGULAR)
    assert _call(tok)[0] == 503 and ledger["calls"] == 1
    # Still inside REGULAR_KEY_RETRY_SECONDS: ride the (missing) answer, no
    # new RPC failover walk.
    assert _call(tok)[0] == 503 and ledger["calls"] == 1
    failed_at = app._regular_key_failed_at[ACCOUNT]
    app._regular_key_failed_at[ACCOUNT] = failed_at - app.REGULAR_KEY_RETRY_SECONDS - 1
    assert _call(tok)[0] == 503 and ledger["calls"] == 2


def test_an_out_of_order_write_cannot_clobber_a_fresher_entry(monkeypatch):
    async def _lookup(address):
        # A fresher write (e.g. the sign-in seed) lands, stamped with ITS OWN
        # (later) start time, while this lookup is still in flight.
        app._note_regular_key(ACCOUNT, "fresher-key")
        return KeyAuthority("stale-key", False, True)

    monkeypatch.setattr(app.xrpl_ops, "key_authority", _lookup)
    assert _run(app._regular_key_still_set(ACCOUNT, "stale-key")) is True
    checked, key = app._regular_keys[ACCOUNT]
    assert key == "fresher-key"  # this lookup's own (stale) answer did not clobber it


def test_sign_in_seeds_the_answer(monkeypatch, ledger):
    monkeypatch.setattr(app, "_warm_funder_cache", lambda wallet: None)
    r = _run(app._finish_web_signin(ACCOUNT, "walletconnect", signer=SIGNER))
    tok = json.loads(r.text)["session_token"]
    ledger["authority"] = LOOKUP_FAILED
    assert _call(tok)[0] == 200 and ledger["calls"] == 0


def test_require_wallet_endpoints_are_covered(ledger):
    @app.require_wallet
    async def wallet_handler(request):
        return app.web.json_response({"ok": True})

    ledger["authority"] = KeyAuthority(None, False, True)
    tok = _token(**REGULAR)
    resp = _run(wallet_handler(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 401 and json.loads(resp.text)["code"] == "key_revoked"


def test_the_event_stream_refuses_a_revoked_session(ledger):
    ledger["authority"] = KeyAuthority(None, False, True)
    resp = _run(app.handle_events_me(_Req(query={"token": _token(**REGULAR)})))
    assert resp.status == 401 and json.loads(resp.text)["code"] == "key_revoked"


def test_logout_and_disconnect_are_exempt_from_the_revocation_check(ledger):
    """Revocation only ever narrows access: logout and wallet disconnect must
    keep working even with no cached answer and an unreachable ledger (F2)."""
    ledger["authority"] = LOOKUP_FAILED
    tok = _token(**REGULAR)
    resp = _run(app.handle_logout(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 200
    assert app.verify_session_token(tok) is None  # denylisted despite the lookup failure

    tok2 = _token(**REGULAR)
    resp2 = _run(app.handle_wallet_disconnect(_Req({"Authorization": f"Bearer {tok2}"})))
    assert resp2.status == 200 and json.loads(resp2.text)["wallet"] is None
    assert ledger["calls"] == 0  # neither handler touched the ledger

    # A normal require_auth handler is not exempt: it still fails closed.
    status, body = _call(_token(**REGULAR))
    assert status == 503 and body["code"] == "key_unverified"
