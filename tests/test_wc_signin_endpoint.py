# tests/test_wc_signin_endpoint.py
# WalletConnect (Joey) sign-in (#447): POST /api/web/signin with
# provider="walletconnect" issues a durable nonce row instead of a XUMM
# payload, and POST /api/web/signin/proof trades a signed, never-submitted
# proof transaction for a platform="web" session token.
#
# Proofs are signed at the binary-codec level (as tests/test_signing_proof.py
# does) — xrpl-py's typed `Transaction.from_xrpl` rejects `Fee "0"`.
import asyncio
import json
import os
import time

import pytest

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from xrpl.core import keypairs
from xrpl.core.binarycodec import encode_for_signing
from xrpl.wallet import Wallet

import lfg_service.app as app
from lfg_core import memos
from lfg_core.signing import proof, store
from lfg_service import identity as identity_store


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, body=None, headers=None, match=None, remote="1.2.3.4"):
        self._body = body or {}
        self.headers = headers or {}
        self.match_info = match or {}
        self.remote = remote
        self._store: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(identity_store, "DATABASE", str(tmp_path / "identity.db"))
    identity_store.ensure_identities_table()
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "sign.db"))
    store.ensure_table()
    app.web_signin_payloads.clear()
    app._web_signin_hits.clear()
    app._web_proof_hits.clear()


def _fake_create():
    async def fake(return_url=None):
        return {"uuid": "u-1", "xumm_url": "https://xumm.app/sign/u-1"}

    return fake


def _sign(wallet, nonce, action=memos.ACTION_SIGNIN):
    tx = proof.build_proof_tx(wallet.classic_address, nonce, action)
    tx["SigningPubKey"] = wallet.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), wallet.private_key)
    return tx


def test_wc_start_requires_feature(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    r = _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"})))
    assert r.status == 503
    assert json.loads(r.text)["code"] == "wc_disabled"


def test_wc_disabled_does_not_consume_the_signin_budget(monkeypatch):
    """A 503 for an unconfigured provider must not burn the XUMM budget."""
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    for _ in range(app.WEB_SIGNIN_RATE_MAX + 3):
        r = _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"})))
        assert r.status == 503
    assert app._web_signin_hits == {}
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    assert _run(app.handle_web_signin_start(_Req(body={}))).status == 200


def test_wc_start_issues_nonce_row(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    r = _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"})))
    b = json.loads(r.text)
    assert b["sign_id"].startswith("wc-")
    assert len(b["nonce"]) == 64
    assert b["source_tag"] == app.config.SOURCE_TAG
    assert b["provider"] == "walletconnect"
    assert b["expires_at"] == store.get(b["sign_id"])["expires_at"]
    # The memos are account-independent; the client builds the proof tx itself.
    assert (
        b["memos"]
        == proof.build_proof_tx(app._MEMO_TEMPLATE_ACCOUNT, b["nonce"], memos.ACTION_SIGNIN)[
            "Memos"
        ]
    )
    row = store.get(b["sign_id"])
    assert row["purpose"] == "signin"
    assert row["wallet"] == ""
    assert row["state"] == "pending"


def test_wc_start_is_rate_limited(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    for _ in range(app.WEB_SIGNIN_RATE_MAX):
        assert (
            _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"}))).status
            == 200
        )
    r = _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"})))
    assert r.status == 429


def test_default_provider_is_still_xumm(monkeypatch):
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    r = _run(app.handle_web_signin_start(_Req(body={})))
    assert "uuid" in json.loads(r.text)


def _start(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    return json.loads(
        _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"}))).text
    )


def test_valid_proof_signs_in_with_wc_provider(monkeypatch):
    b = _start(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, b["nonce"])
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    body = json.loads(r.text)
    assert r.status == 200
    assert body["state"] == "signed"
    assert body["wallet"] == w.classic_address
    decoded = app.verify_session_token(body["session_token"])
    assert decoded["provider"] == "walletconnect"
    assert decoded["platform"] == "web"
    assert decoded["id"] == w.classic_address
    assert identity_store.resolve("web", w.classic_address) == w.classic_address
    assert store.get(b["sign_id"])["state"] == "consumed"


def test_proof_is_single_use(monkeypatch):
    b = _start(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, b["nonce"])
    assert (
        _run(
            app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx}))
        ).status
        == 200
    )
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 409
    assert json.loads(r.text)["code"] == "proof_replayed"


def test_bad_proof_is_400_and_row_stays_pending(monkeypatch):
    b = _start(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, "f" * 64)
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 400
    assert json.loads(r.text)["code"] == "bad_proof"
    assert store.get(b["sign_id"])["state"] == "pending"


def test_oversized_proof_is_rejected_before_verification(monkeypatch):
    b = _start(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, b["nonce"])
    tx["Memos"] = tx["Memos"] + [{"Memo": {"MemoType": "AA", "MemoData": "BB" * 5000}}]
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 400
    assert json.loads(r.text)["code"] == "bad_proof"
    assert store.get(b["sign_id"])["state"] == "pending"


def test_non_dict_proof_is_400(monkeypatch):
    b = _start(monkeypatch)
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": "nope"})))
    assert r.status == 400
    assert json.loads(r.text)["code"] == "bad_proof"


def test_expired_proof_is_410(monkeypatch):
    b = _start(monkeypatch)
    store.expire_stale(now=time.time() + proof.SIGNIN_TTL + 1)
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": {}})))
    assert r.status == 410
    assert json.loads(r.text)["code"] == "proof_expired"


def test_consumed_row_stays_409_after_expiry(monkeypatch):
    """A row already spent answers "you used this", not "this timed out"."""
    b = _start(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, b["nonce"])
    ok = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert ok.status == 200
    store.expire_stale(now=time.time() + proof.SIGNIN_TTL + 1)  # no-op: not pending
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 409
    assert json.loads(r.text)["code"] == "proof_replayed"


def test_proof_is_rate_limited_per_ip():
    for _ in range(app.WEB_SIGNIN_RATE_MAX):
        assert (
            _run(
                app.handle_web_signin_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}}))
            ).status
            == 404
        )
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}})))
    assert r.status == 429
    assert json.loads(r.text)["code"] == "rate_limited"
    # a different IP is unaffected
    other = _Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}}, remote="5.6.7.8")
    assert _run(app.handle_web_signin_proof(other)).status == 404


def test_proof_limiter_is_independent_of_the_start_limiter(monkeypatch):
    """A fumbled signature must never lock the caller out of the Xaman arm."""
    for _ in range(app.WEB_SIGNIN_RATE_MAX + 1):
        _run(app.handle_web_signin_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}})))
    assert app._web_signin_hits == {}
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    assert _run(app.handle_web_signin_start(_Req(body={}))).status == 200
    # …and the reverse: exhausting the start budget leaves /proof usable.
    app._web_proof_hits.clear()
    for _ in range(app.WEB_SIGNIN_RATE_MAX + 1):
        _run(app.handle_web_signin_start(_Req(body={})))
    assert (
        _run(
            app.handle_web_signin_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}}))
        ).status
        == 404
    )


def test_stale_proof_rate_limit_ips_are_pruned():
    app._web_proof_hits["203.0.113.9"] = [0.0]  # far in the past
    _run(app.handle_web_signin_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}})))
    assert "203.0.113.9" not in app._web_proof_hits


def test_unknown_sign_id_is_404():
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}})))
    assert r.status == 404


def test_wrong_purpose_row_is_404(monkeypatch):
    row = store.create(wallet="rX", purpose="tx", txjson=None, nonce="a" * 64, ttl_seconds=300)
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": row["id"], "tx_json": {}})))
    assert r.status == 404


def test_route_registered():
    routes = {
        (r.method, r.resource.canonical) for r in app.create_app().router.routes() if r.resource
    }
    assert ("POST", "/api/web/signin/proof") in routes
