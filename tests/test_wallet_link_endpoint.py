# tests/test_wallet_link_endpoint.py
# Explicit wallet linking (#447): an authed user proves a SECOND wallet is
# theirs — by signing our nonce (WalletConnect) or by completing a Xaman
# SignIn — and the two wallets join one bucket via wallet_proof_links.
#
# Dev-mode harness: config.WEBAPP_DEV_MODE makes require_wallet inject
# mock_economy.DEV_OWNER as the session wallet.
import asyncio
import json
import time

import pytest
from xrpl.core import keypairs
from xrpl.core.binarycodec import encode_for_signing
from xrpl.wallet import Wallet

import lfg_service.app as app
from lfg_core import memos
from lfg_core.signing import proof, store
from lfg_service import identity as identity_store

# Real classic addresses: the Xaman arm gates on is_valid_classic_address.
DEV_OWNER = Wallet.create().classic_address
XAMAN_OTHER = Wallet.create().classic_address


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


def _body(resp):
    return json.loads(resp.text)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(app.mock_economy, "DEV_OWNER", DEV_OWNER, raising=False)
    monkeypatch.setattr(identity_store, "DATABASE", str(tmp_path / "identity.db"))
    identity_store.ensure_identities_table()
    identity_store.link("web", DEV_OWNER, DEV_OWNER, DEV_OWNER)
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "sign.db"))
    store.ensure_table()
    app.wallet_link_payloads.clear()
    app._web_signin_hits.clear()
    app._web_proof_hits.clear()


def _sign(wallet, nonce, action=memos.ACTION_LINK):
    tx = proof.build_proof_tx(wallet.classic_address, nonce, action)
    tx["SigningPubKey"] = wallet.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), wallet.private_key)
    return tx


def _start_wc(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    r = _run(app.handle_wallet_link_start(_Req(body={"provider": "walletconnect"})))
    assert r.status == 200
    return _body(r)


# --- WalletConnect arm -------------------------------------------------------


def test_wc_link_start_requires_feature(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    r = _run(app.handle_wallet_link_start(_Req(body={"provider": "walletconnect"})))
    assert r.status == 503
    assert _body(r)["code"] == "wc_disabled"


def test_wc_link_start_issues_a_nonce_row_bound_to_the_session_wallet(monkeypatch):
    b = _start_wc(monkeypatch)
    assert b["provider"] == "walletconnect"
    assert len(b["nonce"]) == 64
    assert b["source_tag"] == app.config.SOURCE_TAG
    # Account-independent memos, carrying the LINK action (not signin).
    assert (
        b["memos"]
        == proof.build_proof_tx(app._MEMO_TEMPLATE_ACCOUNT, b["nonce"], memos.ACTION_LINK)["Memos"]
    )
    row = store.get(b["sign_id"])
    assert row["purpose"] == "link"
    # The row's wallet is the SESSION wallet — the one the prover must differ from.
    assert row["wallet"] == DEV_OWNER
    assert row["state"] == "pending"
    assert b["expires_at"] == row["expires_at"]


def test_valid_proof_links_the_new_wallet_into_the_bucket(monkeypatch):
    b = _start_wc(monkeypatch)
    w = Wallet.create()
    r = _run(
        app.handle_wallet_link_proof(
            _Req(body={"sign_id": b["sign_id"], "tx_json": _sign(w, b["nonce"])})
        )
    )
    assert r.status == 200
    body = _body(r)
    assert body["state"] == "linked"
    assert body["wallet"] == w.classic_address
    assert body["wallets"] == [DEV_OWNER] + sorted([w.classic_address])
    assert store.get(b["sign_id"])["state"] == "consumed"
    bucket = identity_store.bucket_for_wallet(w.classic_address)
    assert sorted(bucket["wallets"]) == sorted([DEV_OWNER, w.classic_address])


def test_proving_the_session_wallet_itself_is_400(monkeypatch):
    """Linking a wallet to itself proves nothing — refuse rather than no-op."""
    b = _start_wc(monkeypatch)
    # Craft a proof whose signer IS the session wallet by pinning DEV_OWNER to
    # a real keypair for this test.
    w = Wallet.create()
    app.mock_economy.DEV_OWNER = w.classic_address
    b = _start_wc(monkeypatch)
    r = _run(
        app.handle_wallet_link_proof(
            _Req(body={"sign_id": b["sign_id"], "tx_json": _sign(w, b["nonce"])})
        )
    )
    assert r.status == 400
    assert _body(r)["code"] == "same_wallet"


def test_link_proof_replay_is_409(monkeypatch):
    b = _start_wc(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, b["nonce"])
    assert (
        _run(
            app.handle_wallet_link_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx}))
        ).status
        == 200
    )
    r = _run(app.handle_wallet_link_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 409
    assert _body(r)["code"] == "proof_replayed"


def test_sign_id_for_another_session_wallet_is_404(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    row = store.create(
        wallet="rSomeoneElse", purpose="link", txjson=None, nonce="a" * 64, ttl_seconds=300
    )
    w = Wallet.create()
    r = _run(
        app.handle_wallet_link_proof(
            _Req(body={"sign_id": row["id"], "tx_json": _sign(w, "a" * 64)})
        )
    )
    assert r.status == 404
    assert store.get(row["id"])["state"] == "pending"


def test_signin_purpose_row_is_not_redeemable_as_a_link(monkeypatch):
    row = store.create(
        wallet=DEV_OWNER, purpose="signin", txjson=None, nonce="a" * 64, ttl_seconds=300
    )
    r = _run(app.handle_wallet_link_proof(_Req(body={"sign_id": row["id"], "tx_json": {}})))
    assert r.status == 404


def test_expired_link_proof_is_410(monkeypatch):
    b = _start_wc(monkeypatch)
    store.expire_stale(now=time.time() + proof.SIGNIN_TTL + 1)
    r = _run(app.handle_wallet_link_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": {}})))
    assert r.status == 410
    assert _body(r)["code"] == "proof_expired"


def test_bad_link_proof_is_400_and_row_stays_pending(monkeypatch):
    b = _start_wc(monkeypatch)
    w = Wallet.create()
    r = _run(
        app.handle_wallet_link_proof(
            _Req(body={"sign_id": b["sign_id"], "tx_json": _sign(w, "f" * 64)})
        )
    )
    assert r.status == 400
    assert _body(r)["code"] == "bad_proof"
    assert store.get(b["sign_id"])["state"] == "pending"


def test_a_signin_action_proof_is_not_accepted_for_a_link(monkeypatch):
    """The action is part of what is signed — a sign-in proof must not link."""
    b = _start_wc(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, b["nonce"], action=memos.ACTION_SIGNIN)
    r = _run(app.handle_wallet_link_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 400
    assert _body(r)["code"] == "bad_proof"


def test_link_proof_is_rate_limited_per_ip(monkeypatch):
    for _ in range(app.WEB_SIGNIN_RATE_MAX):
        assert (
            _run(
                app.handle_wallet_link_proof(
                    _Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}})
                )
            ).status
            == 404
        )
    r = _run(app.handle_wallet_link_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}})))
    assert r.status == 429
    assert _body(r)["code"] == "rate_limited"


# --- Xaman arm ---------------------------------------------------------------


def _fake_create(uuid="u-link-1"):
    async def fake(return_url=None):
        return {
            "uuid": uuid,
            "xumm_url": f"https://xumm.app/sign/{uuid}",
            "qr_url": "https://q/x.png",
        }

    return fake


def _fake_status(**over):
    s = {"signed": True, "account": XAMAN_OTHER, "expired": False, "opened": True}
    s.update(over)

    async def fake(uuid):
        return s

    return fake


def test_xaman_is_the_default_provider_and_needs_no_feature_flag(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    b = _body(_run(app.handle_wallet_link_start(_Req(body={}))))
    assert b["provider"] == "xaman"
    assert b["uuid"] == "u-link-1"
    assert b["signin_link"].endswith("u-link-1")
    assert b["qr_url"] == "https://q/x.png"
    assert app.wallet_link_payloads["u-link-1"]["wallet"] == DEV_OWNER


def test_xaman_signed_status_links_the_wallet(monkeypatch):
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    _run(app.handle_wallet_link_start(_Req(body={})))
    monkeypatch.setattr(app.xumm_ops, "get_payload_status", _fake_status())
    r = _run(app.handle_wallet_link_status(_Req(match={"payload_uuid": "u-link-1"})))
    assert r.status == 200
    b = _body(r)
    assert b["state"] == "linked"
    assert b["wallet"] == XAMAN_OTHER
    assert b["wallets"] == [DEV_OWNER, XAMAN_OTHER]
    assert "u-link-1" not in app.wallet_link_payloads


def test_xaman_signing_with_the_session_wallet_is_400(monkeypatch):
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    _run(app.handle_wallet_link_start(_Req(body={})))
    monkeypatch.setattr(app.xumm_ops, "get_payload_status", _fake_status(account=DEV_OWNER))
    r = _run(app.handle_wallet_link_status(_Req(match={"payload_uuid": "u-link-1"})))
    assert r.status == 400
    assert _body(r)["code"] == "same_wallet"
    assert "u-link-1" not in app.wallet_link_payloads


def test_xaman_status_for_another_users_payload_is_404(monkeypatch):
    app.wallet_link_payloads["u-x"] = {"wallet": "rSomeoneElse", "created_at": time.time()}
    r = _run(app.handle_wallet_link_status(_Req(match={"payload_uuid": "u-x"})))
    assert r.status == 404


def test_xaman_unknown_uuid_is_404():
    r = _run(app.handle_wallet_link_status(_Req(match={"payload_uuid": "nope"})))
    assert r.status == 404


def test_xaman_expired_and_pending_states(monkeypatch):
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    _run(app.handle_wallet_link_start(_Req(body={})))
    monkeypatch.setattr(app.xumm_ops, "get_payload_status", _fake_status(signed=False))
    assert (
        _body(_run(app.handle_wallet_link_status(_Req(match={"payload_uuid": "u-link-1"}))))[
            "state"
        ]
        == "opened"
    )
    monkeypatch.setattr(
        app.xumm_ops, "get_payload_status", _fake_status(signed=False, expired=True)
    )
    assert (
        _body(_run(app.handle_wallet_link_status(_Req(match={"payload_uuid": "u-link-1"}))))[
            "state"
        ]
        == "expired"
    )
    assert "u-link-1" not in app.wallet_link_payloads


def test_stale_xaman_link_payloads_are_pruned(monkeypatch):
    app.wallet_link_payloads["old"] = {"wallet": DEV_OWNER, "created_at": 0.0}
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    _run(app.handle_wallet_link_start(_Req(body={})))
    assert "old" not in app.wallet_link_payloads


def test_routes_registered():
    routes = {
        (r.method, r.resource.canonical) for r in app.create_app().router.routes() if r.resource
    }
    assert ("POST", "/api/wallet/link") in routes
    assert ("POST", "/api/wallet/link/proof") in routes
    assert ("GET", "/api/wallet/link/{payload_uuid}") in routes


def test_a_link_row_cannot_be_redeemed_as_a_signin(monkeypatch):
    """Escalation guard: a link proof must never mint a web SESSION.

    Both halves are checked independently — the row's `purpose` and the signed
    `action` — so neither one alone is load-bearing.
    """
    b = _start_wc(monkeypatch)
    w = Wallet.create()
    tx = _sign(w, b["nonce"], action=memos.ACTION_LINK)
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 404
    assert store.get(b["sign_id"])["state"] == "pending"
