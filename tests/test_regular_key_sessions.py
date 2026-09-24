"""RegularKey session revocation (agent users spec §2, Amendment 1)."""

import asyncio
import json
import time

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
    yield
    app._revoked_sessions.clear()


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
    # The fresher answer decides the request too, not only the cache (#603 review):
    # the stale lookup's "stale-key" must neither be stored nor authorize.
    assert _run(app._regular_key_still_set(ACCOUNT, "stale-key")) is False
    checked, key = app._regular_keys[ACCOUNT]
    assert key == "fresher-key"  # this lookup's own (stale) answer did not clobber it


def test_a_stale_lookup_cannot_revoke_a_session_a_fresher_answer_allows(monkeypatch):
    async def _lookup(address):
        app._note_regular_key(ACCOUNT, SIGNER)  # the fresher answer: still SIGNER
        return KeyAuthority(OTHER, False, True)  # the stale read: rotated away

    monkeypatch.setattr(app.xrpl_ops, "key_authority", _lookup)
    assert _run(app._regular_key_still_set(ACCOUNT, SIGNER)) is True


def test_a_failed_lookup_defers_to_an_answer_written_meanwhile(monkeypatch):
    async def _lookup(address):
        app._note_regular_key(ACCOUNT, SIGNER)
        return LOOKUP_FAILED

    monkeypatch.setattr(app.xrpl_ops, "key_authority", _lookup)
    assert _run(app._regular_key_still_set(ACCOUNT, SIGNER)) is True  # not None → no 503


def test_pruning_tolerates_a_signature_another_thread_already_removed():
    class _Racing(dict):
        """items() still lists a signature a concurrent prune already deleted."""

        def items(self):
            return [*super().items(), ("gone", 0.0)]

    racing = _Racing({"live": 9e18})
    original = app._revoked_sessions
    app._revoked_sessions = racing
    try:
        app._prune_revoked_sessions(1.0)  # used to raise KeyError('gone')
    finally:
        app._revoked_sessions = original
    assert dict(racing) == {"live": 9e18}


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


def test_noting_an_older_answer_never_overwrites_a_newer_one():
    app._note_regular_key(ACCOUNT, SIGNER)
    checked, _ = app._regular_keys[ACCOUNT]
    app._note_regular_key(ACCOUNT, OTHER, checked - 1.0)  # read before the stored one
    assert app._regular_keys[ACCOUNT] == (checked, SIGNER)
    app._note_regular_key(ACCOUNT, OTHER, checked + 1.0)  # read after it
    assert app._regular_keys[ACCOUNT] == (checked + 1.0, OTHER)


@pytest.mark.parametrize("answer", [KeyAuthority(SIGNER, False, True), LOOKUP_FAILED])
def test_concurrent_stale_rechecks_share_one_ledger_read(monkeypatch, answer):
    calls = []

    async def _slow_lookup(address):
        calls.append(address)
        await asyncio.sleep(0.05)  # every other request arrives while this is in flight
        return answer

    monkeypatch.setattr(app.xrpl_ops, "key_authority", _slow_lookup)
    app._note_regular_key(ACCOUNT, SIGNER, time.monotonic() - 3600)  # stale answer on record

    async def burst():
        return await asyncio.gather(
            *(app._regular_key_still_set(ACCOUNT, SIGNER) for _ in range(5))
        )

    results = _run(burst())
    assert calls == [ACCOUNT]  # one read, not five
    assert results == [True] * 5  # each waiter judged its signer against the shared result


def test_a_wallets_recheck_lock_is_dropped_once_nothing_is_in_flight(ledger):
    app._note_regular_key(ACCOUNT, SIGNER, time.monotonic() - 3600)  # stale: forces a read
    assert _run(app._regular_key_still_set(ACCOUNT, SIGNER)) is True
    assert app._regular_key_locks == {}


def test_answers_older_than_a_session_are_swept_on_the_next_write():
    now = time.monotonic()
    # past a session's lifetime plus the grace: no token can still need it
    gone = now - app.SESSION_TTL - app.REGULAR_KEY_SWEEP_GRACE_SECONDS - 1
    # just past SESSION_TTL: its token was minted a moment after this lookup started,
    # so it may still be valid, and this may be its only answer
    edge = now - app.SESSION_TTL - 1
    app._note_regular_key("rGONE", SIGNER, gone)
    app._note_regular_key("rEDGE", SIGNER, edge)
    app._regular_key_failed_at["rGONE"] = gone
    app._note_regular_key(ACCOUNT, SIGNER)
    assert "rGONE" not in app._regular_keys
    assert "rGONE" not in app._regular_key_failed_at
    assert app._regular_keys["rEDGE"][1] == SIGNER
    assert app._regular_keys[ACCOUNT][1] == SIGNER
