# tests/test_agent_signin_endpoint.py
# The `agent` web sign-in provider (agent users spec §1): POST
# /api/web/signin with provider="agent" is the same WalletConnect-shaped
# proof flow, gated on AGENT_SIGNIN_ENABLED, with platform="agent" proof
# memos instead of platform="webapp".
#
# Proofs are signed at the binary-codec level (as tests/test_signing_proof.py
# does) — xrpl-py's typed `Transaction.from_xrpl` rejects `Fee "0"`.
import asyncio
import json
import os

import pytest

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from xrpl.core import keypairs
from xrpl.core.binarycodec import encode_for_signing
from xrpl.wallet import Wallet

import lfg_service.app as app
from lfg_core import memos
from lfg_core.signing import proof, store
from lfg_core.signing.key_authority import NOT_FOUND
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


@pytest.fixture(autouse=True)
def _stub_proof_creation_ledger(monkeypatch):
    """No network from the start endpoints: pin the observed validated ledger."""

    async def _ledger():
        return 1000

    monkeypatch.setattr(app.xrpl_ops, "current_validated_ledger_index", _ledger)


@pytest.fixture(autouse=True)
def ledger_keys(monkeypatch):
    """No network from the proof endpoints: the account's keys as the ledger
    reports them. Default: found, no RegularKey, master enabled."""
    state = {"authority": NOT_FOUND, "addresses": []}

    async def _lookup(address):
        state["addresses"].append(address)
        return state["authority"]

    monkeypatch.setattr(app.xrpl_ops, "key_authority", _lookup)
    return state


def _sign(wallet, nonce, platform=memos.PLATFORM_AGENT, action=memos.ACTION_SIGNIN):
    tx = proof.build_proof_tx(wallet.classic_address, nonce, action, platform=platform)
    # What Joey does before signing (autofill: true).
    tx.update(Fee="12", Sequence=42, LastLedgerSequence=1500)
    tx["SigningPubKey"] = wallet.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), wallet.private_key)
    return tx


def _start_agent(monkeypatch, enabled=True):
    monkeypatch.setattr(app.config, "AGENT_SIGNIN_ENABLED", enabled)
    r = _run(app.handle_web_signin_start(_Req(body={"provider": "agent"})))
    return r.status, json.loads(r.text)


def _redeem(b, tx):
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    return r.status, json.loads(r.text)


def test_agent_arm_is_off_by_default(monkeypatch):
    status, body = _start_agent(monkeypatch, enabled=False)
    assert status == 503 and body["code"] == "agent_disabled"
    assert app._web_signin_hits == {}  # a disabled arm doesn't burn the sign-in budget


def test_agent_signin_enabled_shipped_default_is_off(monkeypatch):
    # The test above sets the flag False by hand; this pins the SHIPPED
    # default itself — with the env var unset, env_flag reads off, so a
    # deploy that forgets to set AGENT_SIGNIN_ENABLED still ships disabled.
    monkeypatch.delenv("AGENT_SIGNIN_ENABLED", raising=False)
    assert app.config.env_flag("AGENT_SIGNIN_ENABLED", "0") is False


def test_agent_start_issues_an_agent_row_with_agent_memos(monkeypatch):
    status, b = _start_agent(monkeypatch)
    assert status == 200 and b["provider"] == "agent" and b["sign_id"].startswith("wc-")
    assert store.get(b["sign_id"])["provider"] == "agent"
    assert memos.decode_memos(b["memos"])["platform"] == "agent"
    assert b["expires_at"] == store.get(b["sign_id"])["expires_at"]


def test_agent_proof_signs_in_with_the_agent_provider(monkeypatch):
    _, b = _start_agent(monkeypatch)
    w = Wallet.create()
    status, body = _redeem(b, _sign(w, b["nonce"]))
    assert status == 200
    decoded = app.verify_session_token(body["session_token"])
    assert decoded["provider"] == "agent" and decoded["platform"] == "web"
    assert identity_store.resolve("web", w.classic_address) == w.classic_address


def test_an_agent_row_refuses_a_webapp_proof(monkeypatch):
    _, b = _start_agent(monkeypatch)
    status, body = _redeem(b, _sign(Wallet.create(), b["nonce"], platform=memos.PLATFORM_WEBAPP))
    assert status == 400 and body["code"] == "bad_proof"


def test_a_walletconnect_row_refuses_an_agent_proof(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    b = json.loads(_run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"}))).text)
    status, body = _redeem(b, _sign(Wallet.create(), b["nonce"]))
    assert status == 400 and body["code"] == "bad_proof"


def test_a_null_provider_row_redeems_as_walletconnect(monkeypatch):
    row = store.create(
        wallet="",
        purpose="signin",
        txjson=None,
        nonce="n" * 64,
        ttl_seconds=300,
        created_ledger=1000,
    )  # a pre-deploy row: no provider
    w = Wallet.create()
    status, body = _redeem(
        {"sign_id": row["id"]}, _sign(w, "n" * 64, platform=memos.PLATFORM_WEBAPP)
    )
    assert status == 200
    assert app.verify_session_token(body["session_token"])["provider"] == "walletconnect"


def test_a_bogus_provider_row_refuses_bad_proof_not_500(monkeypatch):
    row = store.create(
        wallet="",
        purpose="signin",
        txjson=None,
        nonce="n" * 64,
        ttl_seconds=300,
        created_ledger=1000,
        provider="bogus",
    )  # an unexpected stored provider must refuse cleanly, never KeyError -> 500
    w = Wallet.create()
    status, body = _redeem(
        {"sign_id": row["id"]}, _sign(w, "n" * 64, platform=memos.PLATFORM_WEBAPP)
    )
    assert status == 400 and body["code"] == "bad_proof"


def test_agent_proof_is_single_use(monkeypatch):
    _, b = _start_agent(monkeypatch)
    tx = _sign(Wallet.create(), b["nonce"])
    assert _redeem(b, tx)[0] == 200
    status, body = _redeem(b, tx)
    assert status == 409 and body["code"] == "proof_replayed"


def test_agent_start_shares_the_sign_in_rate_limit(monkeypatch):
    for _ in range(app.WEB_SIGNIN_RATE_MAX):
        assert _start_agent(monkeypatch)[0] == 200
    status, body = _start_agent(monkeypatch)
    assert status == 429 and body["code"] == "rate_limited"
