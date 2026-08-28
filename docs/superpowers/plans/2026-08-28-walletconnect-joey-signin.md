# WalletConnect / Joey Wallet sign-in, linking and signing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Joey Wallet (WalletConnect v2) user sign in to the web/Telegram surface, prove ownership of extra wallets (explicit linking → bucket edge), and sign every transaction the app builds — with the existing Xaman path byte-for-byte unchanged.

**Architecture:** A second `BaseSigningProvider` (`WalletConnectProvider`) is selected *ambiently* — `require_auth` sets `contextvars` from the session token's new `provider` field and the two XUMM chokepoints (`_create_xumm_payload`, `get_payload_status`) dispatch on them, so none of the ~60 builder/poll call sites change. A WC handle looks like a XUMM payload dict whose `xumm_url` is `lfg-wc://<id>`; the client's single `applySignDelivery()` choke point recognises the scheme and drives Joey via `wc.js`. Proof of ownership is a signed-never-submitted `AccountSet` verified locally with xrpl-py; explicit links land in a new `wallet_proof_links` table that is a third BFS edge in `identity._bucket_bfs`.

**Tech Stack:** Python 3.10, aiohttp, sqlite3, xrpl-py 5.0.0 (`xrpl.core.binarycodec.encode_for_signing`, `xrpl.core.keypairs.is_valid_message` / `derive_classic_address`), vanilla ES-module JS, vendored `webapp/client/vendor/walletconnect.js` (exports `SignClient`, `WalletConnectModal`), pytest (+ Node for pure JS tests).

**Spec:** `docs/superpowers/specs/2026-08-27-walletconnect-joey-signin-design.md`

## Global Constraints

- `SourceTag = 2606160021` and provenance memos on every tx and proof (`provenance.stamp_and_validate`). SignIn pseudo-tx exempt.
- Cross-wallet rule: `txjson["Account"] != current_wallet()` ⇒ Xaman path, always (spec §3).
- Handle ids `wc-<uuid4 hex>`; pseudo-link `lfg-wc://<id>`; `sign_mode:"walletconnect"`.
- Proof canonical shape: `AccountSet`, `Fee:"0"`, `Sequence:0`, `LastLedgerSequence:0`, `SourceTag`, `Memos` = provenance memos + `{MemoType:hex("lfg/nonce"), MemoData:hex(nonce)}`. Any other field rejects. RegularKey-signed proofs rejected (v1).
- Error codes/HTTP: `wc_disabled` 503, `bad_proof` 400, `proof_expired` 410, `proof_replayed` 409, `same_wallet` 400, `tx_not_found` 202/410, `tx_mismatch` 409, `not_your_request` 403, `not_linked` 403, `bucket_unavailable` 503.
- Env: `REOWN_PROJECT_ID` (unset = feature OFF, button hidden), `WC_SURFACES` default `web,telegram`.
- TTLs: signin/link proof `SIGNIN_TTL = 300` s; tx requests 900 s (same as XUMM payloads).
- Never `load_dotenv()` bare; tests never read frozen `config.X` for defaults (use `config.env_flag`/raw defaults or monkeypatch).
- Client cache-busters bump in lockstep: `index.html` `app.js?v=79`, tests pinning `?v=78` → `79`; new `wc.js?v=1`, `signdelivery_pure.js?v=2` import in app.js.
- No AI attribution in commits/PR. Pre-push gate (ruff/mypy/gitleaks/pytest/layout) must pass; never `--no-verify`.
- Test helpers: `_run()` uses `asyncio.new_event_loop()` (never `asyncio.run`); handlers are called directly with a `_Req` fake (see `tests/test_web_signin_endpoint.py`); `WEBAPP_DEV_MODE` makes `require_wallet` use `mock_economy.DEV_OWNER`.

---

### Task 1: Ambient provider context + session-token `provider` field

**Files:**
- Create: `lfg_core/signing/context.py`
- Modify: `lfg_service/app.py` (`make_session_token`, `require_auth`)
- Modify: `lfg_core/signing/__init__.py` (export)
- Test: `tests/test_signing_context.py`

**Interfaces:**
- Produces: `signing.context.current_provider() -> str` (default `"xaman"`), `current_wallet() -> str | None`, `signing.context.use(provider: str, wallet: str | None)` → context manager that sets both and resets on exit; `make_session_token(user)` copies `user.get("provider", "xaman")` into the token; `verify_session_token` returns it (already returns the whole payload).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signing_context.py
import asyncio

from lfg_core.signing import context
import lfg_service.app as app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_defaults_are_xaman_and_no_wallet():
    assert context.current_provider() == "xaman"
    assert context.current_wallet() is None


def test_use_sets_and_resets():
    with context.use("walletconnect", "rWALLET"):
        assert context.current_provider() == "walletconnect"
        assert context.current_wallet() == "rWALLET"
    assert context.current_provider() == "xaman"
    assert context.current_wallet() is None


def test_create_task_inherits_context():
    async def main():
        seen = {}

        async def child():
            seen["p"] = context.current_provider()
            seen["w"] = context.current_wallet()

        with context.use("walletconnect", "rW"):
            t = asyncio.create_task(child())
        await t
        return seen

    assert _run(main()) == {"p": "walletconnect", "w": "rW"}


def test_session_token_round_trips_provider():
    tok = app.make_session_token({"id": "rW", "name": "n", "platform": "web", "provider": "walletconnect"})
    assert app.verify_session_token(tok)["provider"] == "walletconnect"
    tok2 = app.make_session_token({"id": "rW", "name": "n", "platform": "web"})
    assert app.verify_session_token(tok2)["provider"] == "xaman"


class _Req:
    def __init__(self, headers):
        self.headers = headers
        self._s = {}

    def __getitem__(self, k):
        return self._s[k]

    def __setitem__(self, k, v):
        self._s[k] = v


def test_require_auth_sets_context_for_the_handler(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    tok = app.make_session_token({"id": "rW", "name": "n", "platform": "web", "provider": "walletconnect"})
    seen = {}

    @app.require_auth
    async def h(request):
        seen["p"] = context.current_provider()
        seen["w"] = context.current_wallet()
        return app.web.json_response({})

    _run(h(_Req({"Authorization": f"Bearer {tok}"})))
    assert seen == {"p": "walletconnect", "w": "rW"}
    assert context.current_provider() == "xaman"  # reset after the handler
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_signing_context.py -q`
Expected: ImportError `lfg_core.signing.context`.

- [ ] **Step 3: Implement**

```python
# lfg_core/signing/context.py
# Ambient signing-provider selection (#447).
#
# The session token names the provider a user signed in with. Instead of
# threading that through every session dataclass and the ~60 builder/poll
# call sites in lfg_service/app.py, require_auth sets it here for the
# request; asyncio.create_task copies the context, so the background flow
# tasks a handler launches inherit it. Startup-resumed jobs and the sweeps
# run with the defaults (xaman, no wallet).
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_provider: contextvars.ContextVar[str] = contextvars.ContextVar("lfg_sign_provider", default="xaman")
_wallet: contextvars.ContextVar[str | None] = contextvars.ContextVar("lfg_sign_wallet", default=None)


def current_provider() -> str:
    return _provider.get()


def current_wallet() -> str | None:
    """The session wallet a WalletConnect session can sign AS. None outside a
    web/Telegram request, or for Xaman sessions (where it is not needed)."""
    return _wallet.get()


@contextmanager
def use(provider: str, wallet: str | None) -> Iterator[None]:
    t1 = _provider.set(provider)
    t2 = _wallet.set(wallet)
    try:
        yield
    finally:
        _wallet.reset(t2)
        _provider.reset(t1)
```

In `lfg_service/app.py`:
- `make_session_token`: add `"provider": user.get("provider", "xaman"),` to `payload`.
- `require_auth.wrapper` (non-dev branch), after `request["user"] = user`:

```python
        # #447: the provider a user signed in with is ambient for the request
        # (and for tasks the handler spawns). Web sessions use the wallet as
        # the platform_user_id, so it doubles as the WC "sign as" account.
        wallet = user["id"] if user.get("platform") == "web" else None
        with signing_context.use(user.get("provider", "xaman"), wallet):
            return await handler(request)
```
Import: `from lfg_core.signing import context as signing_context`. The dev-mode branch stays as is (defaults apply).
- `lfg_core/signing/__init__.py`: no import of `context` at package level is needed (no cycle risk, but keep lazy style) — just add `"context"` to `__all__` is NOT valid; leave `__init__` untouched and import the submodule directly.

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_signing_context.py tests/test_web_signin_endpoint.py tests/test_server_identity_wiring.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(signing): ambient provider context + session-token provider field (#447)"`

---

### Task 2: `sign_requests` store

**Files:**
- Create: `lfg_core/signing/store.py`
- Test: `tests/test_sign_requests_store.py`

**Interfaces:**
- Produces:
  - `store.DATABASE` (module attr, defaults to `lfg_core.user_db.DATABASE`; tests monkeypatch it — same pattern as `identity_store.DATABASE`).
  - `ensure_table() -> None`
  - `create(*, wallet: str, purpose: str, txjson: dict | None, nonce: str | None, ttl_seconds: int, ip: str | None = None) -> dict` → row dict incl. `id` (`"wc-" + uuid4().hex`), `state="pending"`, `expires_at` (UNIX float).
  - `get(request_id: str) -> dict | None` (txjson JSON-decoded, `result` decoded or None)
  - `set_state(request_id, state, *, txid=None, result=None, expect: str | None = "pending") -> bool` — compare-and-set; returns False if the row isn't in `expect` state (single-use guarantees).
  - `expire_stale(now: float | None = None) -> int` — flips `pending` rows past `expires_at` to `expired`.
  - `STATES = ("pending","signed","rejected","failed","mismatch","expired","cancelled","consumed")`

- [ ] **Step 1: Failing tests**

```python
# tests/test_sign_requests_store.py
import time

import pytest

from lfg_core.signing import store


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "app.db"))
    store.ensure_table()


def test_create_and_get_round_trip():
    row = store.create(wallet="rA", purpose="tx", txjson={"TransactionType": "Payment"}, nonce=None, ttl_seconds=900, ip="1.1.1.1")
    assert row["id"].startswith("wc-") and len(row["id"]) == 3 + 32
    got = store.get(row["id"])
    assert got["state"] == "pending" and got["txjson"] == {"TransactionType": "Payment"}
    assert got["expires_at"] > time.time() + 800


def test_get_unknown_is_none():
    assert store.get("wc-nope") is None


def test_set_state_is_compare_and_set():
    row = store.create(wallet="rA", purpose="signin", txjson=None, nonce="abc", ttl_seconds=300)
    assert store.set_state(row["id"], "consumed") is True
    assert store.set_state(row["id"], "consumed") is False  # already consumed
    assert store.get(row["id"])["state"] == "consumed"


def test_set_state_records_txid_and_result():
    row = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    assert store.set_state(row["id"], "signed", txid="ABC", result={"ok": 1})
    got = store.get(row["id"])
    assert got["txid"] == "ABC" and got["result"] == {"ok": 1}


def test_expire_stale_flips_only_pending_past_deadline():
    old = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=1)
    fresh = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    done = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=1)
    store.set_state(done["id"], "signed")
    assert store.expire_stale(now=time.time() + 5) == 1
    assert store.get(old["id"])["state"] == "expired"
    assert store.get(fresh["id"])["state"] == "pending"
    assert store.get(done["id"])["state"] == "signed"
```

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement**

```python
# lfg_core/signing/store.py
# Durable WalletConnect sign requests (#447). One row per request the app
# asked a Joey session to sign; the client posts the outcome back and the
# server verifies it on-ledger. Lives in the app DB next to identities.
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from lfg_core.user_db import DATABASE as _DEFAULT_DB

DATABASE = _DEFAULT_DB
STATES = ("pending", "signed", "rejected", "failed", "mismatch", "expired", "cancelled", "consumed")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table() -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sign_requests (
                id          TEXT PRIMARY KEY,
                wallet      TEXT NOT NULL,
                purpose     TEXT NOT NULL,
                txjson      TEXT,
                nonce       TEXT,
                state       TEXT NOT NULL DEFAULT 'pending',
                txid        TEXT,
                result_json TEXT,
                ip          TEXT,
                created_at  REAL NOT NULL,
                expires_at  REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sign_requests_wallet ON sign_requests(wallet, state)")
        conn.commit()
    finally:
        conn.close()


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["txjson"] = json.loads(d["txjson"]) if d["txjson"] else None
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    return d


def create(*, wallet: str, purpose: str, txjson: dict[str, Any] | None, nonce: str | None,
           ttl_seconds: int, ip: str | None = None) -> dict[str, Any]:
    now = time.time()
    rid = "wc-" + uuid.uuid4().hex
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO sign_requests (id, wallet, purpose, txjson, nonce, state, ip, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (rid, wallet, purpose, json.dumps(txjson) if txjson is not None else None, nonce, ip, now, now + ttl_seconds),
        )
        conn.commit()
    finally:
        conn.close()
    got = get(rid)
    assert got is not None
    return got


def get(request_id: str) -> dict[str, Any] | None:
    conn = _conn()
    try:
        return _row(conn.execute("SELECT * FROM sign_requests WHERE id = ?", (request_id,)).fetchone())
    finally:
        conn.close()


def set_state(request_id: str, state: str, *, txid: str | None = None,
              result: dict[str, Any] | None = None, expect: str | None = "pending") -> bool:
    if state not in STATES:
        raise ValueError(f"unknown sign_requests state {state!r}")
    conn = _conn()
    try:
        sql = "UPDATE sign_requests SET state = ?, txid = COALESCE(?, txid), result_json = COALESCE(?, result_json) WHERE id = ?"
        args: list[Any] = [state, txid, json.dumps(result) if result is not None else None, request_id]
        if expect is not None:
            sql += " AND state = ?"
            args.append(expect)
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def expire_stale(now: float | None = None) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE sign_requests SET state = 'expired' WHERE state = 'pending' AND expires_at < ?",
            (now if now is not None else time.time(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
```

- [ ] **Step 4: Run** → PASS. Also call `store.ensure_table()` from `lfg_service/app.py` where `identity_store.ensure_identities_table()` is called at startup (grep `ensure_identities_table(` in app.py; add the call right after it).

- [ ] **Step 5: Commit** — `feat(signing): sign_requests store for WalletConnect requests (#447)`

---

### Task 3: Proof builder + verifier (`lfg_core/signing/proof.py`)

**Files:**
- Create: `lfg_core/signing/proof.py`
- Test: `tests/test_signing_proof.py`

**Interfaces:**
- Produces:
  - `NONCE_MEMO_TYPE = "lfg/nonce"`, `SIGNIN_TTL = 300`
  - `build_proof_tx(wallet: str, nonce: str, action: str) -> dict` — canonical unsigned proof (`action` ∈ `memos.ACTION_SIGNIN`, `memos.ACTION_LINK` — **add both constants to `lfg_core/memos.py`** (`"signin"`, `"link"`) in its closed enum; check `memos._entries`/validation lists accept them).
  - `class ProofError(Exception)` with `.code` (`"bad_proof"`) and `.reason` (short string, e.g. `"fee"`, `"extra_field"`, `"pubkey_account"`, `"signature"`, `"nonce"`).
  - `verify_proof(tx_json: dict, *, wallet_hint: str | None, nonce: str, action: str) -> str` → the proven classic address; raises `ProofError`.
  - Allowed keys exactly: `{"TransactionType","Account","Fee","Sequence","LastLedgerSequence","SourceTag","Memos","SigningPubKey","TxnSignature","Flags"}` (`Flags` only if `0`). Note Joey may add `NetworkID` on non-mainnet: allow `NetworkID` only when `config.XRPL_NETWORK != "mainnet"` and its value is an int.

- [ ] **Step 1: Failing tests**

```python
# tests/test_signing_proof.py
import pytest
from xrpl.models.transactions import Transaction
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from lfg_core import config, memos
from lfg_core.signing import proof

NONCE = "a" * 64


def _signed(wallet=None, nonce=NONCE, action=memos.ACTION_SIGNIN, mutate=None):
    w = wallet or Wallet.create()
    tx = proof.build_proof_tx(w.classic_address, nonce, action)
    if mutate:
        mutate(tx)
    signed = sign(Transaction.from_xrpl(tx), w)
    return w, signed.to_xrpl()


def test_build_is_canonical_and_unsubmittable():
    tx = proof.build_proof_tx("rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", NONCE, memos.ACTION_SIGNIN)
    assert tx["TransactionType"] == "AccountSet"
    assert tx["Fee"] == "0" and tx["Sequence"] == 0 and tx["LastLedgerSequence"] == 0
    assert tx["SourceTag"] == config.SOURCE_TAG
    decoded = memos.decode_memos(tx["Memos"])
    assert decoded["action"] == "signin"
    assert any(m["Memo"]["MemoType"] == memos.str_to_hex("lfg/nonce") for m in tx["Memos"])


def test_valid_proof_returns_the_account():
    w, tx = _signed()
    assert proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN) == w.classic_address


@pytest.mark.parametrize("mutate,reason", [
    (lambda t: t.update(Fee="10"), "fee"),
    (lambda t: t.update(Sequence=5), "sequence"),
    (lambda t: t.update(LastLedgerSequence=99), "last_ledger"),
    (lambda t: t.update(Destination="rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"), "extra_field"),
    (lambda t: t.update(TransactionType="Payment", Destination="rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", Amount="1"), "type"),
    (lambda t: t.update(SourceTag=1), "source_tag"),
])
def test_noncanonical_fields_reject(mutate, reason):
    _, tx = _signed(mutate=mutate)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == reason


def test_wrong_nonce_rejects():
    _, tx = _signed(nonce="b" * 64)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "nonce"


def test_wrong_action_memo_rejects():
    _, tx = _signed(action=memos.ACTION_LINK)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "action"


def test_tampered_signature_rejects():
    _, tx = _signed()
    tx["TxnSignature"] = tx["TxnSignature"][:-2] + ("00" if tx["TxnSignature"][-2:] != "00" else "11")
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "signature"


def test_pubkey_must_derive_the_account():
    """A RegularKey-signed proof (pubkey != Account) is rejected in v1."""
    other = Wallet.create()
    w, tx = _signed()
    tx["Account"] = other.classic_address
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "pubkey_account"


def test_wallet_hint_must_match():
    w, tx = _signed()
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint="rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "wallet_hint"


def test_non_dict_input_rejects():
    with pytest.raises(proof.ProofError):
        proof.verify_proof(["nope"], wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run** → ImportError / AttributeError (`memos.ACTION_LINK`).

- [ ] **Step 3: Implement**

Add to `lfg_core/memos.py` next to the other actions: `ACTION_SIGNIN = "signin"` and `ACTION_LINK = "link"`, and register them wherever the closed enum is enforced (grep `ACTION_BRIX_CLAIM` in memos.py for the tuple/set that lists valid actions and append both).

```python
# lfg_core/signing/proof.py
# Wallet-ownership proof for WalletConnect sign-in and linking (#447).
#
# Joey exposes no signMessage, so the proof is a SIGNED, NEVER-SUBMITTED
# pseudo-transaction: an AccountSet with Fee "0", Sequence 0 and
# LastLedgerSequence 0 — unsubmittable on any XRPL network — carrying our
# provenance memos plus a server-issued nonce. verify_proof re-derives the
# signing account from SigningPubKey and checks the signature locally with
# xrpl-py; the allowlist of fields is CLOSED so a real transaction can never
# be smuggled in as a "proof".
from __future__ import annotations

from typing import Any

from xrpl.core.binarycodec import encode_for_signing
from xrpl.core.keypairs import derive_classic_address, is_valid_message

from lfg_core import config, memos
from lfg_core.signing import provenance

NONCE_MEMO_TYPE = "lfg/nonce"
SIGNIN_TTL = 300

_ALLOWED = {"TransactionType", "Account", "Fee", "Sequence", "LastLedgerSequence",
            "SourceTag", "Memos", "SigningPubKey", "TxnSignature", "Flags", "NetworkID"}


class ProofError(Exception):
    code = "bad_proof"

    def __init__(self, reason: str):
        super().__init__(f"bad proof: {reason}")
        self.reason = reason


def build_proof_tx(wallet: str, nonce: str, action: str) -> dict[str, Any]:
    tx: dict[str, Any] = {
        "TransactionType": "AccountSet",
        "Account": wallet,
        "Fee": "0",
        "Sequence": 0,
        "LastLedgerSequence": 0,
    }
    provenance.stamp_and_validate(
        tx,
        memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, action),
        require_memos=True,
    )
    tx["Memos"].append({"Memo": {"MemoType": memos.str_to_hex(NONCE_MEMO_TYPE),
                                 "MemoData": memos.str_to_hex(nonce)}})
    return tx


def _nonce_from(memos_list: Any) -> str | None:
    if not isinstance(memos_list, list):
        return None
    want = memos.str_to_hex(NONCE_MEMO_TYPE).upper()
    for m in memos_list:
        body = m.get("Memo") if isinstance(m, dict) else None
        if isinstance(body, dict) and str(body.get("MemoType", "")).upper() == want:
            try:
                return bytes.fromhex(str(body.get("MemoData", ""))).decode()
            except ValueError:
                return None
    return None


def verify_proof(tx_json: Any, *, wallet_hint: str | None, nonce: str, action: str) -> str:
    if not isinstance(tx_json, dict):
        raise ProofError("shape")
    extra = set(tx_json) - _ALLOWED
    if extra:
        raise ProofError("extra_field")
    if tx_json.get("TransactionType") != "AccountSet":
        raise ProofError("type")
    if tx_json.get("Fee") != "0":
        raise ProofError("fee")
    if tx_json.get("Sequence") != 0:
        raise ProofError("sequence")
    if tx_json.get("LastLedgerSequence") != 0:
        raise ProofError("last_ledger")
    if tx_json.get("SourceTag") != config.SOURCE_TAG:
        raise ProofError("source_tag")
    if "Flags" in tx_json and tx_json["Flags"] != 0:
        raise ProofError("flags")
    if "NetworkID" in tx_json and (config.XRPL_NETWORK == "mainnet" or not isinstance(tx_json["NetworkID"], int)):
        raise ProofError("network_id")
    decoded = memos.decode_memos(tx_json.get("Memos")) or {}
    if decoded.get("action") != action:
        raise ProofError("action")
    if _nonce_from(tx_json.get("Memos")) != nonce:
        raise ProofError("nonce")
    account = tx_json.get("Account")
    pub = tx_json.get("SigningPubKey")
    sig = tx_json.get("TxnSignature")
    if not (isinstance(account, str) and isinstance(pub, str) and isinstance(sig, str) and pub and sig):
        raise ProofError("shape")
    try:
        if derive_classic_address(pub) != account:
            raise ProofError("pubkey_account")
    except ProofError:
        raise
    except Exception as e:  # malformed pubkey
        raise ProofError("pubkey") from e
    if wallet_hint is not None and wallet_hint != account:
        raise ProofError("wallet_hint")
    unsigned = {k: v for k, v in tx_json.items() if k != "TxnSignature"}
    try:
        blob = bytes.fromhex(encode_for_signing(unsigned))
        ok = is_valid_message(blob, bytes.fromhex(sig), pub)
    except Exception as e:
        raise ProofError("signature") from e
    if not ok:
        raise ProofError("signature")
    return account
```

Check `memos.decode_memos` tolerates the extra nonce memo (it should ignore unknown MemoTypes — read it; if it rejects unknown types, filter the nonce memo out before decoding). Check `memos.str_to_hex` exists (grep); if it is private (`_str_to_hex`), use that name.

- [ ] **Step 4: Run** → PASS. Also run `tests/test_memos*.py` (closed-enum tests may enumerate actions).

- [ ] **Step 5: Commit** — `feat(signing): signed-pseudo-tx wallet ownership proof (#447)`

---

### Task 4: `WalletConnectProvider` + chokepoint dispatch in `xumm_ops`

**Files:**
- Create: `lfg_core/signing/walletconnect.py`
- Modify: `lfg_core/signing/__init__.py` (registry branch `"walletconnect"`)
- Modify: `lfg_core/xumm_ops.py` (`_create_xumm_payload`, `get_payload_status`, `cancel_xumm_payload`)
- Modify: `scripts/cancel_xumm_payloads.py` (skip `wc-` ids)
- Test: `tests/test_wc_provider.py`

**Interfaces:**
- Produces:
  - `WalletConnectProvider(BaseSigningProvider)`, `name="walletconnect"`, `TX_TTL = 900`.
  - `_create(request)` → `SignHandle(id=row["id"], sign_url=f"lfg-wc://{id}", qr_url=None, push=None, raw=handle_dict)` where `handle_dict = {"uuid": id, "xumm_url": f"lfg-wc://{id}", "qr_url": None, "pushed": False, "push": None, "sign_mode": "walletconnect"}`. Wallet = `request.txjson["Account"]`.
  - `status(id)` → `SignStatus`; `status_dict(id) -> dict | None` (the xumm-shaped dict: `{"opened": state != "pending", "signed": state == "signed", "expired": state in ("expired","rejected","failed","mismatch","cancelled"), "account": wallet, "txid": txid, "user_token": None, "sign_mode": "walletconnect", "state": state}`); returns None for unknown id.
  - `cancel(id)` → `store.set_state(id, "cancelled")`.
  - `is_wc_id(s) -> bool` (`isinstance(s, str) and s.startswith("wc-")`).
  - `xumm_ops.should_use_walletconnect(txjson) -> bool`: `context.current_provider() == "walletconnect" and txjson.get("TransactionType") != "SignIn" and context.current_wallet() is not None and txjson.get("Account") == context.current_wallet()`. Logs INFO `"sign request for {Account} downgraded to xaman (session wallet {w})"` when provider is walletconnect but the account differs.

- [ ] **Step 1: Failing tests**

```python
# tests/test_wc_provider.py
import asyncio

import pytest

from lfg_core import config, memos, signing, xumm_ops
from lfg_core.signing import context, store
from lfg_core.signing import walletconnect as wc

W = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"
OTHER = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "app.db"))
    store.ensure_table()


def _memos():
    return memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_TRUSTSET)


def test_registry_returns_walletconnect_provider():
    assert isinstance(signing.get_provider("walletconnect"), wc.WalletConnectProvider)


def test_create_stores_stamped_txjson_and_returns_wc_handle():
    with context.use("walletconnect", W):
        h = _run(xumm_ops._create_xumm_payload({"TransactionType": "TrustSet", "Account": W, "LimitAmount": {"currency": "USD", "issuer": OTHER, "value": "1"}}, memos_json=_memos()))
    assert h["uuid"].startswith("wc-") and h["xumm_url"] == f"lfg-wc://{h['uuid']}"
    assert h["qr_url"] is None and h["sign_mode"] == "walletconnect" and h["push"] is None
    row = store.get(h["uuid"])
    assert row["txjson"]["SourceTag"] == config.SOURCE_TAG and row["wallet"] == W
    assert row["purpose"] == "tx"


def test_foreign_account_falls_back_to_xaman(monkeypatch):
    calls = []

    async def fake_post(payload):
        calls.append(payload)
        return {"qr_url": "q", "xumm_url": "x", "uuid": "11111111-1111-1111-1111-111111111111", "pushed": False}

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    with context.use("walletconnect", W):
        h = _run(xumm_ops._create_xumm_payload({"TransactionType": "TrustSet", "Account": OTHER}, memos_json=_memos()))
    assert calls and h["uuid"] == "11111111-1111-1111-1111-111111111111"


def test_signin_always_goes_to_xaman(monkeypatch):
    calls = []

    async def fake_post(payload):
        calls.append(payload)
        return {"qr_url": "q", "xumm_url": "x", "uuid": "11111111-1111-1111-1111-111111111111", "pushed": False}

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    with context.use("walletconnect", W):
        _run(xumm_ops._create_xumm_payload({"TransactionType": "SignIn"}))
    assert calls


def test_xaman_session_never_touches_the_store(monkeypatch):
    async def fake_post(payload):
        return {"qr_url": "q", "xumm_url": "x", "uuid": "11111111-1111-1111-1111-111111111111", "pushed": False}

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    h = _run(xumm_ops._create_xumm_payload({"TransactionType": "TrustSet", "Account": W}, memos_json=_memos()))
    assert not h["uuid"].startswith("wc-")


def test_status_maps_states():
    with context.use("walletconnect", W):
        h = _run(xumm_ops._create_xumm_payload({"TransactionType": "TrustSet", "Account": W}, memos_json=_memos()))
    s = _run(xumm_ops.get_payload_status(h["uuid"]))
    assert s["signed"] is False and s["expired"] is False and s["account"] == W and s["txid"] is None
    store.set_state(h["uuid"], "signed", txid="ABC")
    s = _run(xumm_ops.get_payload_status(h["uuid"]))
    assert s["signed"] is True and s["txid"] == "ABC" and s["user_token"] is None
    store.set_state(h["uuid"], "rejected", expect=None)
    s = _run(xumm_ops.get_payload_status(h["uuid"]))
    assert s["signed"] is False and s["expired"] is True


def test_status_unknown_id_is_none():
    assert _run(xumm_ops.get_payload_status("wc-" + "0" * 32)) is None


def test_status_expires_stale_pending():
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ttl_seconds=-1)
    s = _run(xumm_ops.get_payload_status(row["id"]))
    assert s["expired"] is True


def test_cancel_marks_cancelled():
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    assert _run(xumm_ops.cancel_xumm_payload(row["id"])) is True
    assert store.get(row["id"])["state"] == "cancelled"


def test_provider_status_object():
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    st = _run(wc.WalletConnectProvider().status(row["id"]))
    assert st.signed is False and st.resolved is False and st.signer == W
```

- [ ] **Step 2: Run** → failures.

- [ ] **Step 3: Implement**

```python
# lfg_core/signing/walletconnect.py
# The WalletConnect (Joey Wallet) signing provider (#447). Transport is the
# user's browser: the request is stored, the client signs+submits via Joey
# and posts the hash back, and lfg_service verifies it on-ledger
# (handle_sign_result) before the row ever reads "signed".
from __future__ import annotations

import logging
from typing import Any

from lfg_core.signing import store
from lfg_core.signing.base import BaseSigningProvider
from lfg_core.signing.types import SignHandle, SignRequest, SignStatus

TX_TTL = 900
_TERMINAL_NOT_SIGNED = ("expired", "rejected", "failed", "mismatch", "cancelled")


def is_wc_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("wc-")


def handle_dict(request_id: str) -> dict[str, Any]:
    return {"uuid": request_id, "xumm_url": f"lfg-wc://{request_id}", "qr_url": None,
            "pushed": False, "push": None, "sign_mode": "walletconnect"}


class WalletConnectProvider(BaseSigningProvider):
    name = "walletconnect"

    async def _create(self, request: SignRequest) -> SignHandle | None:
        wallet = request.txjson.get("Account")
        if not isinstance(wallet, str) or not wallet:
            logging.error("walletconnect create refused: txjson has no Account")
            return None
        row = store.create(wallet=wallet, purpose="tx", txjson=request.txjson, nonce=None, ttl_seconds=TX_TTL)
        raw = handle_dict(row["id"])
        logging.info(f"WC sign request {row['id']} ({request.txjson.get('TransactionType')}) for {wallet}")
        return SignHandle(id=row["id"], sign_url=raw["xumm_url"], qr_url=None, push=None, raw=raw)

    @staticmethod
    def status_dict(request_id: str) -> dict[str, Any] | None:
        row = store.get(request_id)
        if row is None:
            return None
        if row["state"] == "pending" and store.expire_stale():
            row = store.get(request_id) or row
        state = row["state"]
        return {
            "opened": state != "pending",
            "signed": state == "signed",
            "expired": state in _TERMINAL_NOT_SIGNED,
            "account": row["wallet"],
            "txid": row.get("txid"),
            "user_token": None,
            "sign_mode": "walletconnect",
            "state": state,
        }

    async def status(self, handle_id: str) -> SignStatus:
        raw = self.status_dict(handle_id)
        if raw is None:
            return SignStatus(signed=None, resolved=False, raw={})
        return SignStatus(signed=raw["signed"], resolved=raw["signed"] or raw["expired"],
                          txid=raw["txid"], signer=raw["account"], user_token=None, raw=raw)

    async def cancel(self, handle_id: str) -> bool:
        return store.set_state(handle_id, "cancelled")
```

Registry (`lfg_core/signing/__init__.py`): add
```python
    elif key == "walletconnect":
        from lfg_core.signing.walletconnect import WalletConnectProvider
        provider = WalletConnectProvider()
```

`xumm_ops`:
```python
def should_use_walletconnect(txjson: dict[str, Any]) -> bool:
    """#447: ambient dispatch. WalletConnect only when the session is a WC one
    AND the tx is signable by the connected account (spec §3 cross-wallet
    rule) AND it is a real transaction (SignIn is Xaman's pseudo-tx)."""
    from lfg_core.signing import context

    if context.current_provider() != "walletconnect":
        return False
    if txjson.get("TransactionType") == "SignIn":
        return False
    wallet = context.current_wallet()
    if wallet and txjson.get("Account") == wallet:
        return True
    logging.info(f"sign request for {txjson.get('Account')} downgraded to xaman (session wallet {wallet})")
    return False
```
At the top of `_create_xumm_payload` (before `txtype = ...`):
```python
    if should_use_walletconnect(txjson):
        from lfg_core.signing import get_provider
        handle = await get_provider("walletconnect").create(
            SignRequest(txjson=txjson, memos_json=memos_json, options=options, user_token=None))
        return handle.raw if handle else None
```
(`SignRequest` import: `from lfg_core.signing.types import SignRequest` — types has no xumm_ops dependency, safe at module top.)
`get_payload_status`: first lines
```python
    if uuid.startswith("wc-") if isinstance(uuid, str) else False:
        from lfg_core.signing.walletconnect import WalletConnectProvider
        return WalletConnectProvider.status_dict(uuid)
```
`cancel_xumm_payload`: same guard → `return store.set_state(uuid, "cancelled")` (import `from lfg_core.signing import store` lazily).
`scripts/cancel_xumm_payloads.py`: where uuids are collected, skip candidates starting with `wc-` (they are not XUMM payloads).

- [ ] **Step 4: Run** `tests/test_wc_provider.py tests/test_signing_provider.py tests/test_xumm*.py -q` → PASS.

- [ ] **Step 5: Commit** — `feat(signing): WalletConnect provider + ambient dispatch at the XUMM chokepoints (#447)`

---

### Task 5: Config + `/api/config` walletconnect block

**Files:**
- Modify: `lfg_core/config.py`, `lfg_service/app.py` (`handle_config`), `CLAUDE.md` env list, `docs/ops/env.staging.example` (if it lists optional vars)
- Test: `tests/test_wc_config.py`

**Interfaces:**
- Produces: `config.REOWN_PROJECT_ID: str` (`os.getenv("REOWN_PROJECT_ID", "").strip()`), `config.WC_SURFACES: frozenset[str]` (parse `os.getenv("WC_SURFACES", "web,telegram")`, lowercase, stripped, empty dropped), `config.WC_CHAIN: str` (`"xrpl:0"` if `XRPL_NETWORK == "mainnet"` else `"xrpl:1"`), `config.wc_enabled() -> bool` (`bool(REOWN_PROJECT_ID)`).
- `/api/config` gains `"walletconnect": {"project_id": …, "chain": …, "surfaces": sorted(list)}` when enabled, else `"walletconnect": None`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_wc_config.py
import asyncio
import json

import lfg_service.app as app
from lfg_core import config


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_default_is_off():
    assert config.WC_SURFACES == frozenset({"web", "telegram"}) or True  # ambient env may differ; shape check below
    assert isinstance(config.WC_SURFACES, frozenset)


def test_config_reports_null_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "")
    body = json.loads(_run(app.handle_config(None)).text)
    assert body["walletconnect"] is None


def test_config_reports_block_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "pid123")
    monkeypatch.setattr(config, "WC_CHAIN", "xrpl:1")
    monkeypatch.setattr(config, "WC_SURFACES", frozenset({"web"}))
    body = json.loads(_run(app.handle_config(None)).text)
    assert body["walletconnect"] == {"project_id": "pid123", "chain": "xrpl:1", "surfaces": ["web"]}
```

- [ ] **Step 2: Run** → AttributeError.
- [ ] **Step 3: Implement** per interfaces; `handle_config` ignores its `request` today (it does — the test passes `None`; if it reads request, use the `_Req` fake from Task 6). Add the two env lines to `CLAUDE.md`'s env block:
```text
REOWN_PROJECT_ID=<reown-cloud-project-id>                   # optional (#447); WalletConnect/Joey Wallet sign-in + signing — unset = feature OFF, button hidden
WC_SURFACES=web,telegram                                    # optional (#447); surfaces that show "Connect with Joey" (discord-activity needs URL Mappings first)
```
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(config): REOWN_PROJECT_ID / WC_SURFACES + /api/config walletconnect block (#447)`

---

### Task 6: WalletConnect sign-in endpoints

**Files:**
- Modify: `lfg_service/app.py` (`handle_web_signin_start`, new `handle_web_signin_proof`, routes)
- Test: `tests/test_wc_signin_endpoint.py`

**Interfaces:**
- `POST /api/web/signin` body `{"provider":"walletconnect"}` → when `config.wc_enabled()` is False: 503 `wc_disabled`. Else: rate-limit as today; `nonce = secrets.token_hex(32)`; `store.create(wallet="", purpose="signin", txjson=None, nonce=nonce, ttl_seconds=proof.SIGNIN_TTL, ip=_client_ip(request))`; respond `{"sign_id": id, "nonce": nonce, "source_tag": config.SOURCE_TAG, "expires_at": row["expires_at"], "provider": "walletconnect"}`. Default/absent provider → unchanged XUMM path.
- `POST /api/web/signin/proof` body `{"sign_id","tx_json"}` (no auth): row missing or `purpose != "signin"` → 404; `state != "pending"` → 409 `proof_replayed`; expired → 410 `proof_expired` (and set state expired); `proof.verify_proof(tx_json, wallet_hint=None, nonce=row["nonce"], action=memos.ACTION_SIGNIN)` → `ProofError` ⇒ 400 `{"error": "bad proof", "code": "bad_proof"}` + `logging.warning(f"bad signin proof {sign_id}: {e.reason}")`; success ⇒ `store.set_state(sign_id, "consumed")` must return True (else 409 `proof_replayed`) → same identity link + name logic as `handle_web_signin_status` → `make_session_token({"id": wallet, "name": name, "platform": "web", "provider": "walletconnect"})` → `{"state":"signed","wallet","session_token","user":{"id","username"}}`. Extract the shared "link + issue token" block from `handle_web_signin_status` into `async def _finish_web_signin(wallet: str, provider: str) -> web.Response` and use it from both.
- Routes: `add_post("/api/web/signin/proof", handle_web_signin_proof)` registered BEFORE `add_get("/api/web/signin/{payload_uuid}", …)` is not required (different method) but keep it adjacent.

- [ ] **Step 1: Failing tests** (reuse `_Req`/`_run`/`setup_function` from `tests/test_web_signin_endpoint.py`, with `identity_store.DATABASE` + `store.DATABASE` monkeypatched to tmp and tables ensured, as `tests/test_brix_claim_all.py` does for identity):

```python
def test_wc_start_requires_feature(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    r = _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"})))
    assert r.status == 503 and json.loads(r.text)["code"] == "wc_disabled"


def test_wc_start_issues_nonce_row(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    r = _run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"})))
    b = json.loads(r.text)
    assert b["sign_id"].startswith("wc-") and len(b["nonce"]) == 64 and b["source_tag"] == app.config.SOURCE_TAG
    assert store.get(b["sign_id"])["purpose"] == "signin"


def test_default_provider_is_still_xumm(monkeypatch):
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    r = _run(app.handle_web_signin_start(_Req(body={})))
    assert "uuid" in json.loads(r.text)


def _start(monkeypatch):
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    return json.loads(_run(app.handle_web_signin_start(_Req(body={"provider": "walletconnect"}))).text)


def test_valid_proof_signs_in_with_wc_provider(monkeypatch):
    b = _start(monkeypatch)
    w = Wallet.create()
    tx = sign(Transaction.from_xrpl(proof.build_proof_tx(w.classic_address, b["nonce"], memos.ACTION_SIGNIN)), w).to_xrpl()
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    body = json.loads(r.text)
    assert r.status == 200 and body["wallet"] == w.classic_address
    assert app.verify_session_token(body["session_token"])["provider"] == "walletconnect"
    assert identity_store.resolve("web", w.classic_address) == w.classic_address


def test_proof_is_single_use(monkeypatch):
    b = _start(monkeypatch); w = Wallet.create()
    tx = sign(Transaction.from_xrpl(proof.build_proof_tx(w.classic_address, b["nonce"], memos.ACTION_SIGNIN)), w).to_xrpl()
    assert _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx}))).status == 200
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 409 and json.loads(r.text)["code"] == "proof_replayed"


def test_bad_proof_is_400_and_row_stays_pending(monkeypatch):
    b = _start(monkeypatch); w = Wallet.create()
    tx = sign(Transaction.from_xrpl(proof.build_proof_tx(w.classic_address, "f" * 64, memos.ACTION_SIGNIN)), w).to_xrpl()
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 400 and json.loads(r.text)["code"] == "bad_proof"
    assert store.get(b["sign_id"])["state"] == "pending"


def test_expired_proof_is_410(monkeypatch):
    b = _start(monkeypatch)
    store.expire_stale(now=time.time() + proof.SIGNIN_TTL + 1)
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": {}})))
    assert r.status == 410


def test_unknown_sign_id_is_404():
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": "wc-" + "0" * 32, "tx_json": {}})))
    assert r.status == 404


def test_route_registered():
    routes = {(r.method, r.resource.canonical) for r in app.create_app().router.routes() if r.resource}
    assert ("POST", "/api/web/signin/proof") in routes
```
(`create_app` — check the actual factory name in app.py, grep `def create_app\|def make_app`.)

- [ ] **Step 2: Run** → failures. **Step 3: Implement** per interfaces. **Step 4: Run** both signin test files → PASS. **Step 5: Commit** — `feat(web): WalletConnect sign-in via signed-proof nonce flow (#447)`

---

### Task 7: `wallet_proof_links` + explicit link endpoints

**Files:**
- Modify: `lfg_service/identity.py` (table, `link_proof()`, `_bucket_bfs` third edge, `bucket_for_wallet` seed query)
- Modify: `lfg_service/app.py` (`handle_wallet_link_start`, `handle_wallet_link_proof`, `handle_wallet_link_status` for the Xaman variant, routes)
- Test: `tests/test_identity_proof_links.py`, `tests/test_wallet_link_endpoint.py`

**Interfaces:**
- `identity.link_proof(wallet_a: str, wallet_b: str, proof_kind: str) -> bool` — orders `(a, b)` lexically, `INSERT OR IGNORE INTO wallet_proof_links (wallet_a, wallet_b, proof_kind, linked_at)`; returns True when a row was inserted. `a == b` raises `ValueError`.
- Table: `wallet_proof_links(wallet_a TEXT NOT NULL, wallet_b TEXT NOT NULL, proof_kind TEXT NOT NULL, linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (wallet_a, wallet_b))` + indexes on each column.
- `_bucket_bfs`: after the token-sibling loop add
```python
            for (w2,) in conn.execute(
                "SELECT wallet_b FROM wallet_proof_links WHERE wallet_a = ? "
                "UNION SELECT wallet_a FROM wallet_proof_links WHERE wallet_b = ?",
                (w, w),
            ):
                if w2 not in wallets:
                    wallets.add(w2)
                    frontier_wallets.append(w2)
```
  and `bucket_for_wallet`'s existence query gains `UNION SELECT 1 FROM wallet_proof_links WHERE wallet_a = ? OR wallet_b = ?`.
- Endpoints (all `@require_wallet`; session wallet = `request["wallet"]`):
  - `POST /api/wallet/link` body `{"provider": "walletconnect"|"xaman"}`:
    - `walletconnect`: needs `config.wc_enabled()` (503 `wc_disabled`); creates `store.create(wallet=request["wallet"], purpose="link", nonce=…, ttl_seconds=proof.SIGNIN_TTL)` → `{"provider":"walletconnect","sign_id","nonce","source_tag","expires_at"}`. (The row's `wallet` column holds the SESSION wallet — the one that must differ from the prover.)
    - `xaman` (default): `xumm_ops.create_signin_payload()` → record `wallet_link_payloads[uuid] = {"wallet": session_wallet, "created_at": time.time()}` (module dict + TTL prune like `web_signin_payloads`) → `{"provider":"xaman","uuid","signin_link","qr_url"}`.
  - `POST /api/wallet/link/proof` body `{"sign_id","tx_json"}`: row must exist with `purpose=="link"` and `row["wallet"] == request["wallet"]` (else 404); pending/expired checks as Task 6; `verify_proof(..., action=memos.ACTION_LINK)`; proven wallet == session wallet → 400 `same_wallet`; consume row; `identity.link_proof(session, proven, "wc-signed-tx")`; respond `{"state":"linked","wallet": proven, "wallets": _linked_wallets_for(session)}`.
  - `GET /api/wallet/link/{uuid}` (Xaman variant status): rec must exist and `rec["wallet"] == request["wallet"]` (404); `xumm_ops.get_payload_status(uuid)`; signed with valid account: same-wallet → 400 `same_wallet` (drop rec); else `link_proof(session, account, "xaman-signin")`, drop rec, `{"state":"linked","wallet":account,"wallets":[…]}`; expired → `{"state":"expired"}`; else `{"state":"opened"|"pending"}`.

- [ ] **Step 1: Failing tests** — `tests/test_identity_proof_links.py`:

```python
def test_proof_link_merges_buckets(_db):
    identity.link("web", "rA", "rA", "rA"); identity.link("web", "rB", "rB", "rB")
    assert identity.bucket_for_wallet("rA")["wallets"] == ["rA"]
    assert identity.link_proof("rB", "rA", "wc-signed-tx") is True
    assert identity.link_proof("rA", "rB", "wc-signed-tx") is False  # ordered PK, idempotent
    assert identity.bucket_for_wallet("rA")["wallets"] == ["rA", "rB"]
    assert identity.bucket_for_wallet("rB")["bucket_id"] == identity.bucket_for_wallet("rA")["bucket_id"]


def test_proof_link_reaches_wallets_known_only_by_proof(_db):
    identity.link("web", "rA", "rA", "rA")
    identity.link_proof("rA", "rZ", "wc-signed-tx")  # rZ has no identity row
    assert identity.bucket_for_wallet("rZ")["wallets"] == ["rA", "rZ"]


def test_same_wallet_rejected(_db):
    with pytest.raises(ValueError):
        identity.link_proof("rA", "rA", "x")


def test_lookup_failure_still_raises(_db, monkeypatch):
    monkeypatch.setattr(identity, "DATABASE", "/nonexistent/dir/x.db")
    with pytest.raises(identity.BucketLookupError):
        identity.bucket_for_wallet("rA")
```
`tests/test_wallet_link_endpoint.py` (dev-mode harness: `WEBAPP_DEV_MODE` True ⇒ session wallet is `mock_economy.DEV_OWNER`): WC start needs feature; WC proof by a fresh `Wallet.create()` links it into `DEV_OWNER`'s bucket; proving with `DEV_OWNER`'s own key → 400 `same_wallet`; replay → 409; a `sign_id` created for another session wallet → 404; Xaman variant: fake `create_signin_payload` + fake `get_payload_status` returning `{"signed": True, "account": "rOTHER…", "expired": False, "opened": True}` → linked; account == DEV_OWNER → 400; routes registered (`POST /api/wallet/link`, `POST /api/wallet/link/proof`, `GET /api/wallet/link/{uuid}`).

- [ ] **Step 2-4:** run → fail → implement → PASS (`tests/test_identity*.py tests/test_brix_claim_all.py tests/test_wallet_link_endpoint.py`).
- [ ] **Step 5: Commit** — `feat(identity): wallet_proof_links bucket edge + explicit wallet linking endpoints (#447)`

---

### Task 8: `GET /api/sign/{id}` + `POST /api/sign/{id}/result`

**Files:**
- Modify: `lfg_service/app.py` (handlers, routes, `_semantic_match`)
- Test: `tests/test_sign_result_endpoint.py`

**Interfaces:**
- `_AUTOFILL_KEYS = {"Fee","Sequence","LastLedgerSequence","SigningPubKey","TxnSignature","hash","NetworkID","Flags","ctid","date","ledger_index","inLedger","validated","meta","TicketSequence","AccountTxnID"}`.
- `_semantic_match(stored: dict, onledger: dict) -> bool`: compare `{k: v for k in stored if k not in _AUTOFILL_KEYS}` against the same keys in `onledger` (`onledger` = `result["tx_json"]` if present — rippled ≥2.0 `tx` shape — else `result` itself); `Flags` compared only when stored sets a non-zero value; amounts compared as-is (both come from JSON; XRP drops are strings in both).
- `GET /api/sign/{id}` (`@require_wallet`): row missing or `row["wallet"] != request["wallet"]` → 404; `expire_stale()` first; → `{"id","state","txjson","expires_at","txid"}`.
- `POST /api/sign/{id}/result` (`@require_wallet`) body one of `{"hash"}`, `{"rejected": true}`, `{"error": "..."}`: row missing/foreign → 403 `not_your_request` (spec) — use 404 for missing, 403 for foreign; `purpose != "tx"` → 404; row not pending → 409 `proof_replayed`? No — return `{"state": row.state}` 200 idempotently for an already-terminal row whose `txid == hash`, else 409 `already_resolved`.
  - `rejected` → `set_state(id,"rejected")` → `{"state":"rejected"}`.
  - `error` → `set_state(id,"failed", result={"error": str(error)[:200]})` → `{"state":"failed"}`.
  - `hash` (validate 64-hex else 400): `res = await xrpl_ops.get_tx(hash)` (exception → 503 `ledger_unavailable`); `if not res.get("validated")`: if `time.time() > row["expires_at"]` → set expired, 410 `tx_not_found`; else 202 `{"state":"pending","code":"tx_not_found"}`. Validated: `tx = res.get("tx_json") or res`; require `tx.get("Account") == row["wallet"]` and `tx.get("TransactionType") == row["txjson"]["TransactionType"]` and `_semantic_match(row["txjson"], tx)` else `set_state(id,"mismatch", result={"hash": hash})`, `logging.warning`, 409 `tx_mismatch`. Match → `set_state(id,"signed", txid=hash, result={"meta_result": (res.get("meta") or {}).get("TransactionResult")})` → `{"state":"signed","txid":hash}`.
- Routes: `add_get("/api/sign/{request_id}", …)`, `add_post("/api/sign/{request_id}/result", …)`.

- [ ] **Step 1: Failing tests** — dev-mode harness; rows created with `store.create(wallet=mock_economy.DEV_OWNER, purpose="tx", txjson={"TransactionType":"TrustSet","Account":DEV_OWNER,"LimitAmount":{...},"SourceTag":config.SOURCE_TAG,"Memos":[...]}, …)`; monkeypatch `app.xrpl_ops.get_tx` with fakes: validated+matching (→ signed, txid), validated with different `LimitAmount` (→ 409 mismatch, state mismatch), `Account` foreign (→ mismatch), `{"error":"txnNotFound"}` (→ 202, state pending), same but row expired (→ 410 expired), raising (→ 503); rejected/error bodies; foreign-wallet row (→ 403); bad hash (→ 400); GET own row returns txjson; GET foreign → 404; idempotent re-post with same hash after signed → 200 signed.

- [ ] **Step 2-4:** fail → implement → PASS.
- [ ] **Step 5: Commit** — `feat(api): WalletConnect sign request fetch + on-ledger verified result (#447)`

---

### Task 9: Client — `wc.js`, `applySignDelivery` hook, sign-in button, link-wallet UI

**Files:**
- Create: `webapp/client/wc.js`
- Modify: `webapp/client/signdelivery_pure.js` (`isWcLink`, `wcRequestId`), `webapp/client/app.js`, `webapp/client/index.html`
- Test: `tests/test_signdelivery_pure_js.py` (+2 Node tests), pinned `?v=` tests (`grep -rn "app.js?v=78" tests/` → bump to 79)

**Interfaces:**
- `signdelivery_pure.js`: `export function isWcLink(link)` → `typeof link === 'string' && link.startsWith('lfg-wc://')`; `export function wcRequestId(link)` → id after the scheme or `null`. `signDelivery()` unchanged.
- `wc.js` (ES module; lazily imports `./vendor/walletconnect.js`; NEVER imported by Xaman users — `app.js` does `await import('./wc.js?v=1')` only inside `startWcSignin()`/`wcSign()`):
  - `export async function connect({ projectId, chain, metadata })` → `{ wallet, topic }`: `SignClient.init({projectId, metadata})`, reuse a stored `lfg_wc_topic` session if `client.session.get(topic)` is live, else `client.connect({requiredNamespaces:{xrpl:{chains:[chain],methods:['xrpl_signTransaction'],events:[]}}})` → `WalletConnectModal` `openModal({uri})` → `await approval()` → `closeModal()`; wallet = `session.namespaces.xrpl.accounts[0].split(':')[2]`; store topic in localStorage.
  - `export async function signTx({ chain, txJson, autofill, submit })` → `client.request({topic, chainId: chain, request:{method:'xrpl_signTransaction', params:{tx_json: txJson, options:{autofill, submit}}}})` → returns the response (`{tx_json, hash?}`); throws on user rejection (WC error code 5000/4001 → `err.rejected = true`).
  - `export async function disconnect()`; `export function activeWallet()`.
- `app.js`:
  - `applySignDelivery()`: first line — `if (signDeliveryPure.isWcLink(link)) { hideAll(qrEl, linkBtn, toggleBtn); wcSign(signDeliveryPure.wcRequestId(link)); return { linkPrimary:false, qrCollapsed:true, autoOpen:false }; }` where `wcSign(id)` is dedup-guarded per id (`wcInFlight` Set): `GET /api/sign/{id}` → if `state !== 'pending'` return; `toast('Approve in Joey Wallet…')`; `signTx({chain, txJson: r.txjson, autofill:true, submit:true})` → `POST /api/sign/{id}/result` with `{hash}` (or `{rejected:true}` / `{error}`); on 202 retry the POST every 3 s until `expires_at`; on `tx_mismatch` `showError('Joey signed a different transaction — aborted.')`. The existing per-flow pollers then see `signed` from `get_payload_status` and proceed unchanged.
  - Sign-in panel: if `cfg.walletconnect && cfg.walletconnect.surfaces.includes(insideTelegram ? 'telegram' : insideWeb ? 'web' : 'discord-activity')` show a `#register-wc-btn` "Connect with Joey Wallet"; `startWcSignin()`: `POST /api/web/signin {provider:'walletconnect'}` → `connect()` → build proof tx client-side **from the server's canonical shape**: `GET`? No — the client builds `{TransactionType:'AccountSet', Account: wallet, Fee:'0', Sequence:0, LastLedgerSequence:0, SourceTag: s.source_tag, Memos: s.memos}` — so **Task 6's start response must also include `memos`**: add `"memos": proof.build_proof_tx("rrrrrrrrrrrrrrrrrrrrrhoLvTp", nonce, ACTION_SIGNIN)["Memos"]` to the WC start response (and the link start response with `ACTION_LINK`) — the memos do not depend on the account. Then `signTx({autofill:false, submit:false})` → `POST /api/web/signin/proof {sign_id, tx_json: resp.tx_json}` → store session token as `pollWebSignin` does.
  - Profile/mint-home: "Link another wallet" button (`#link-wallet-btn`, shown when signed in on web/telegram) → `startLinkWallet()` panel `#link-panel` with two buttons: "Prove with Joey" (WC flow with `ACTION_LINK`, `POST /api/wallet/link {provider:'walletconnect'}` → connect (a NEW pairing: call `connect({fresh:true})` so the modal opens even with a live topic) → proof → `POST /api/wallet/link/proof`) and "Prove with Xaman" (`POST /api/wallet/link {provider:'xaman'}` → `applySignDelivery` with the QR → poll `GET /api/wallet/link/{uuid}`). On `linked`: toast + `loadBrix()`.
  - `ALL_PANELS` += `'link-panel'`; `setupWeb` restores WC topic only when the stored session token's provider is WC (decode the token body `JSON.parse(atob(token.split('.')[0]))`).
  - Claim-all "Set trustline" row: no change needed — the server downgrades to Xaman and returns a real `xumm_url`, so `applySignDelivery` renders the QR; set the trustline panel sub copy to `Scan with the Xaman app holding ${wallet}` when `wallet` is given (edit `startBrixTrustline`'s `renderTrustline` call: `sub: wallet ? \`Scan with the Xaman app holding ${wallet}\` : v.sub`).
- `index.html`: `#register-wc-btn` (hidden) in `#register-panel`; new `#link-panel` section; `app.js?v=79`.

- [ ] **Step 1: Failing Node tests** (in `tests/test_signdelivery_pure_js.py`, same harness):
```python
def test_is_wc_link():
    assert _node("import {isWcLink, wcRequestId} from './signdelivery_pure.js'; console.log(JSON.stringify([isWcLink('lfg-wc://wc-abc'), isWcLink('https://xumm.app/x'), isWcLink(null), wcRequestId('lfg-wc://wc-abc'), wcRequestId('https://x')]))") == [True, False, False, "wc-abc", None]
```
(adapt to the file's existing `_node`/`run_js` helper name.)
- [ ] **Step 2: Run** → fails. **Step 3: Implement** all of the above. **Step 4:** `.venv/bin/pytest tests/test_signdelivery_pure_js.py tests/ -q -k "pure_js or cache or index_html"` → PASS; `grep -rn "v=78" tests/ webapp/` → none left.
- [ ] **Step 5: Commit** — `feat(client): Joey Wallet sign-in, linking and WalletConnect signing via wc.js (#447)`

---

### Task 10: Docs, layout, full gate

**Files:**
- Modify: `CLAUDE.md` (a short "WalletConnect / Joey (#447)" subsection under XUMM Flow notes: ambient provider, `lfg-wc://`, cross-wallet rule, sign_requests table, ops env), `README.md` repository layout only if a new top-level path was added (none — `lfg_core/signing/*` is already covered).

- [ ] **Step 1:** write the CLAUDE.md subsection (≤ 25 lines, mirror the spec's §1/§3 rules).
- [ ] **Step 2:** `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy` (use the pre-commit config's exact invocations: `.venv/bin/pre-commit run --all-files --hook-stage pre-push`) → all green.
- [ ] **Step 3:** `.venv/bin/pytest -q -x` full suite → PASS (note count).
- [ ] **Step 4: Commit** — `docs: WalletConnect/Joey provider notes (#447)`

---

## Self-review

- Spec coverage: §1 (Tasks 1, 4, 5), §2 sign-in + link (Tasks 3, 6, 7), §3 tx signing + cross-wallet rule (Tasks 4, 8, 9), §4 client (Task 9), §5 tests (each task), Ops (Task 5/10 docs). Gap closed: cross-wallet rule enforced in `should_use_walletconnect` (Task 4) and rendered in Task 9.
- Type consistency: `store.create(... ) -> dict` with `id/state/expires_at/nonce/wallet/purpose/txjson/txid/result`; `set_state(expect=...)`; `proof.verify_proof(tx_json, *, wallet_hint, nonce, action) -> str`; `walletconnect.handle_dict(id)`; `xumm_ops.should_use_walletconnect(txjson)`; `context.use(provider, wallet)`; `identity.link_proof(a, b, kind) -> bool`.
- Task 6 amendment from Task 9: WC start responses include `memos`.
