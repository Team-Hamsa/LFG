# WalletConnect / Joey Wallet Sign-in, Linking & Signing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a web/Telegram user sign in, link a second wallet, and sign every app transaction with Joey Wallet over WalletConnect v2, with the server verifying every proof/transaction itself.

**Architecture:** A `WalletConnectProvider` joins the #433 `lfg_core/signing` seam and is dispatched at the two `xumm_ops` chokepoints (`_create_xumm_payload`, `get_payload_status`) keyed on a per-session `provider` string, so no flow learns a new API. Sign-in/link proofs are signed-never-submitted `AccountSet` pseudo-transactions verified locally with xrpl-py; real transactions are autofilled+submitted by Joey and verified on-ledger by hash. A new `wallet_proof_links` table becomes the third edge type in the identity bucket BFS.

**Tech Stack:** Python 3.10 / aiohttp / sqlite / xrpl-py 5.0.0 (`xrpl.core.keypairs`, `xrpl.core.binarycodec`); vanilla-JS ES modules; vendored `webapp/client/vendor/walletconnect.js` (`SignClient` 2.21.1 + `WalletConnectModal` 2.7.0, PR #449).

**Spec:** `docs/superpowers/specs/2026-08-27-walletconnect-joey-signin-design.md`

## Global Constraints

- Every real transaction carries `SourceTag = 2606160021` and provenance memos; the base class enforces this — never override `create()`.
- Sign-in/link proof shape (verbatim): `AccountSet`, `Fee:"0"`, `Sequence:0`, `LastLedgerSequence:0`, `SourceTag`, memos = provenance triple + `lfg/nonce`. Any other field → reject.
- Proof pubkey must derive `Account` (RegularKey not accepted in v1).
- Server never trusts the client: tx results are verified via `xrpl_ops.get_tx` (`validated`, `Account`, semantic match).
- `provider` defaults to `"xaman"` everywhere — zero behavior change for existing paths; the whole existing suite must stay green.
- Handle ids for WalletConnect are `wc-<uuid4>`; XUMM uuids are RFC uuids — routing is by prefix.
- WC expiry = 900 s (`SIGNIN_TTL` for proofs, `DEFAULT_EXPIRE_MINUTES*60` for tx).
- Feature OFF when `REOWN_PROJECT_ID` unset (`/api/config` omits `walletconnect`, endpoints 503 `wc_disabled`). Surfaces gated by `WC_SURFACES` (default `web,telegram`).
- No AI attribution in commits/PRs. Tests: `.venv/bin/pytest`. Pre-push gate runs ruff/mypy/pytest — keep mypy clean (annotate everything).
- Client `?v=` cache-busters bump in the same PR as client changes (`index.html` `app.js?v=78` → 79; `mint_pure.js?v=24` untouched unless edited).
- Work in a worktree off `main` (`~/LFG` sits on `deploy`): `git worktree add ../LFG-447 -b feat/447-walletconnect origin/main && ln -s ~/LFG/.venv ../LFG-447/.venv`.

---

## File map

| File | Responsibility |
|---|---|
| `lfg_core/signing/store.py` (new) | `sign_requests` table: create/get/update rows for WC handles + proof nonces |
| `lfg_core/signing/proof.py` (new) | Build the canonical proof txjson; verify a signed proof locally |
| `lfg_core/signing/walletconnect.py` (new) | `WalletConnectProvider(BaseSigningProvider)` |
| `lfg_core/signing/__init__.py` | register `"walletconnect"` |
| `lfg_core/signing/result.py` (new) | On-ledger verification of a client-reported tx hash (`verify_submitted`) |
| `lfg_core/xumm_ops.py` | `provider=` kwarg on `_create_xumm_payload` + every builder; `get_payload_status` routes `wc-` |
| `lfg_core/memos.py` | `ACTION_SIGNIN`, `ACTION_LINK` |
| `lfg_core/config.py` | `REOWN_PROJECT_ID`, `WC_SURFACES`, `WC_CHAIN` |
| `lfg_core/{mint,swap,market,bulk_mint,burn2mint}_flow.py` | `provider` field on sessions, threaded into builders |
| `lfg_service/identity.py` | `wallet_proof_links` table + `link_wallets_by_proof` + BFS edge |
| `lfg_service/app.py` | session-token `provider`; WC sign-in start/proof; `/api/sign/{id}/result`; `/api/wallet/link*`; `/api/config` block; constructor sites pass `provider` |
| `webapp/client/wc.js` (new) | WalletConnect client wrapper (connect / restore / signTransaction / disconnect) |
| `webapp/client/app.js`, `index.html` | Joey sign-in button + proof flow; `sign_mode:"walletconnect"` handling in `applySignDelivery` callers; link-wallet UI |
| `tests/test_sign_store.py`, `tests/test_signing_proof.py`, `tests/test_wc_provider.py`, `tests/test_sign_result.py`, `tests/test_web_signin_wc.py`, `tests/test_identity_proof_links.py`, `tests/test_wallet_link_endpoint.py` | per-task tests |
| `CLAUDE.md`, `docs/ops/env.staging.example` | env docs |

---

### Task 1: `sign_requests` store

**Files:**
- Create: `lfg_core/signing/store.py`
- Test: `tests/test_sign_store.py`

**Interfaces:**
- Produces:
  ```python
  DB_PATH_FN = db_path.app_db_path            # patched in tests
  def ensure_schema(conn) -> None
  def new_id() -> str                         # "wc-<uuid4>"
  def create(*, wallet: str, purpose: str, txjson: dict | None, nonce: str | None,
             ip: str | None, ttl_seconds: int) -> dict        # returns the row
  def get(row_id: str) -> dict | None
  def set_state(row_id: str, state: str, *, txid: str | None = None,
                result_json: dict | None = None, expected_from: str | None = None) -> bool
  def expire_stale(now: float | None = None) -> int
  ```
  Row dict keys: `id, wallet, purpose, txjson (dict|None), nonce, state, txid, result_json (dict|None), ip, created_at (float), expires_at (float)`.
  States: `pending | signed | rejected | failed | mismatch | expired | cancelled | consumed`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sign_store.py
import time

from lfg_core.signing import store

W = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"


def _db(tmp_path, monkeypatch):
    p = str(tmp_path / "app.db")
    monkeypatch.setattr(store, "DB_PATH_FN", lambda network=None: p)
    return p


def test_create_and_get_roundtrip(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    row = store.create(wallet=W, purpose="tx", txjson={"TransactionType": "Payment"},
                       nonce=None, ip="1.2.3.4", ttl_seconds=900)
    assert row["id"].startswith("wc-") and row["state"] == "pending"
    got = store.get(row["id"])
    assert got["txjson"] == {"TransactionType": "Payment"}
    assert got["expires_at"] - got["created_at"] == 900


def test_set_state_is_compare_and_set(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    row = store.create(wallet=W, purpose="signin", txjson=None, nonce="abc", ip=None, ttl_seconds=10)
    assert store.set_state(row["id"], "consumed", expected_from="pending") is True
    # second consume must fail: single-use nonce
    assert store.set_state(row["id"], "consumed", expected_from="pending") is False
    assert store.get(row["id"])["state"] == "consumed"


def test_set_state_records_txid_and_result(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ip=None, ttl_seconds=10)
    store.set_state(row["id"], "signed", txid="AB" * 32, result_json={"ok": 1})
    got = store.get(row["id"])
    assert got["txid"] == "AB" * 32 and got["result_json"] == {"ok": 1}


def test_expire_stale_only_touches_pending(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    old = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ip=None, ttl_seconds=1)
    done = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ip=None, ttl_seconds=1)
    store.set_state(done["id"], "signed")
    assert store.expire_stale(now=time.time() + 5) == 1
    assert store.get(old["id"])["state"] == "expired"
    assert store.get(done["id"])["state"] == "signed"


def test_get_unknown_is_none(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert store.get("wc-nope") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_sign_store.py -q`
Expected: `ImportError: cannot import name 'store'`

- [ ] **Step 3: Implement**

```python
# lfg_core/signing/store.py
# Durable handle store for non-XUMM signing providers (#447).
#
# XUMM keeps payload state on its side and we poll it; WalletConnect has no
# such server — the browser IS the transport — so the service keeps the
# pending request (the stamped txjson, or a sign-in nonce) here and the
# client reports the outcome. Rows live in the app DB (lfg_nfts.db, network-
# aware via db_path.app_db_path) next to identities.
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from lfg_core import db_path

DB_PATH_FN = db_path.app_db_path

STATES = frozenset(
    {"pending", "signed", "rejected", "failed", "mismatch", "expired", "cancelled", "consumed"}
)
PURPOSES = frozenset({"tx", "signin", "link"})


def ensure_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sign_requests_state ON sign_requests(state)")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH_FN())
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def new_id() -> str:
    return f"wc-{uuid.uuid4()}"


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["txjson"] = json.loads(d["txjson"]) if d["txjson"] else None
    d["result_json"] = json.loads(d["result_json"]) if d["result_json"] else None
    return d


def create(
    *,
    wallet: str,
    purpose: str,
    txjson: dict[str, Any] | None,
    nonce: str | None,
    ip: str | None,
    ttl_seconds: int,
) -> dict[str, Any]:
    if purpose not in PURPOSES:
        raise ValueError(f"bad purpose {purpose!r}")
    now = time.time()
    row_id = new_id()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO sign_requests (id, wallet, purpose, txjson, nonce, ip, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                wallet,
                purpose,
                json.dumps(txjson) if txjson is not None else None,
                nonce,
                ip,
                now,
                now + ttl_seconds,
            ),
        )
        conn.commit()
        out = _row(conn.execute("SELECT * FROM sign_requests WHERE id = ?", (row_id,)).fetchone())
    finally:
        conn.close()
    assert out is not None
    return out


def get(row_id: str) -> dict[str, Any] | None:
    conn = _conn()
    try:
        return _row(conn.execute("SELECT * FROM sign_requests WHERE id = ?", (row_id,)).fetchone())
    finally:
        conn.close()


def set_state(
    row_id: str,
    state: str,
    *,
    txid: str | None = None,
    result_json: dict[str, Any] | None = None,
    expected_from: str | None = None,
) -> bool:
    """Compare-and-set when `expected_from` is given (single-use nonces,
    idempotent result posts). Returns whether a row changed."""
    if state not in STATES:
        raise ValueError(f"bad state {state!r}")
    conn = _conn()
    try:
        sql = "UPDATE sign_requests SET state = ?, txid = COALESCE(?, txid), result_json = COALESCE(?, result_json) WHERE id = ?"
        params: list[Any] = [state, txid, json.dumps(result_json) if result_json is not None else None, row_id]
        if expected_from is not None:
            sql += " AND state = ?"
            params.append(expected_from)
        cur = conn.execute(sql, params)
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

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_sign_store.py -q` → 5 passed.
- [ ] **Step 5: Commit** — `git add lfg_core/signing/store.py tests/test_sign_store.py && git commit -m "feat(signing): sign_requests store for WalletConnect handles (#447)"`

---

### Task 2: Proof builder + verifier

**Files:**
- Modify: `lfg_core/memos.py` (add `ACTION_SIGNIN = "signin"`, `ACTION_LINK = "link"` to the constants and `_ACTIONS`)
- Create: `lfg_core/signing/proof.py`
- Test: `tests/test_signing_proof.py`

**Interfaces:**
- Produces:
  ```python
  NONCE_MEMO_TYPE = "lfg/nonce"
  class ProofError(ValueError): code: str      # "bad_proof" subcodes in .reason
  def build_proof_txjson(wallet: str, nonce: str, action: str) -> dict   # action = memos.ACTION_SIGNIN|ACTION_LINK
  def verify_proof(tx_json: dict, *, nonce: str, action: str) -> str     # returns the proven wallet, raises ProofError
  ```

- [ ] **Step 1: Failing tests**

```python
# tests/test_signing_proof.py
import pytest
from xrpl.core import binarycodec, keypairs
from xrpl.wallet import Wallet

from lfg_core import memos
from lfg_core.signing import proof

NONCE = "00" * 32


def _sign(tx: dict, w: Wallet) -> dict:
    tx = dict(tx)
    tx["SigningPubKey"] = w.public_key
    tx["TxnSignature"] = keypairs.sign(
        bytes.fromhex(binarycodec.encode_for_signing(tx)), w.private_key
    )
    return tx


@pytest.fixture
def w():
    return Wallet.create()


def test_roundtrip(w):
    tx = proof.build_proof_txjson(w.classic_address, NONCE, memos.ACTION_SIGNIN)
    assert tx["Fee"] == "0" and tx["Sequence"] == 0 and tx["LastLedgerSequence"] == 0
    assert proof.verify_proof(_sign(tx, w), nonce=NONCE, action=memos.ACTION_SIGNIN) == w.classic_address


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda t: t.update(Fee="12"), "fee"),
        (lambda t: t.update(Sequence=5), "sequence"),
        (lambda t: t.update(LastLedgerSequence=99), "last_ledger_sequence"),
        (lambda t: t.update(Destination="rrrrrrrrrrrrrrrrrrrrBZbvji"), "unexpected_field"),
        (lambda t: t.update(TransactionType="Payment"), "transaction_type"),
        (lambda t: t.update(SourceTag=1), "source_tag"),
        (lambda t: t.pop("Memos"), "memos"),
    ],
)
def test_rejects_mutations(w, mutate, reason):
    tx = proof.build_proof_txjson(w.classic_address, NONCE, memos.ACTION_SIGNIN)
    mutate(tx)
    with pytest.raises(proof.ProofError) as e:
        proof.verify_proof(_sign(tx, w), nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert e.value.reason == reason


def test_rejects_wrong_nonce(w):
    tx = _sign(proof.build_proof_txjson(w.classic_address, NONCE, memos.ACTION_SIGNIN), w)
    with pytest.raises(proof.ProofError) as e:
        proof.verify_proof(tx, nonce="11" * 32, action=memos.ACTION_SIGNIN)
    assert e.value.reason == "nonce"


def test_rejects_wrong_action(w):
    tx = _sign(proof.build_proof_txjson(w.classic_address, NONCE, memos.ACTION_SIGNIN), w)
    with pytest.raises(proof.ProofError) as e:
        proof.verify_proof(tx, nonce=NONCE, action=memos.ACTION_LINK)
    assert e.value.reason == "action"


def test_rejects_pubkey_not_matching_account(w):
    other = Wallet.create()
    tx = proof.build_proof_txjson(w.classic_address, NONCE, memos.ACTION_SIGNIN)
    with pytest.raises(proof.ProofError) as e:
        proof.verify_proof(_sign(tx, other), nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert e.value.reason == "pubkey_account"


def test_rejects_tampered_signature(w):
    tx = _sign(proof.build_proof_txjson(w.classic_address, NONCE, memos.ACTION_SIGNIN), w)
    tx["TxnSignature"] = tx["TxnSignature"][:-2] + ("00" if tx["TxnSignature"][-2:] != "00" else "11")
    with pytest.raises(proof.ProofError) as e:
        proof.verify_proof(tx, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert e.value.reason == "signature"


def test_rejects_missing_signature_fields(w):
    tx = proof.build_proof_txjson(w.classic_address, NONCE, memos.ACTION_SIGNIN)
    with pytest.raises(proof.ProofError) as e:
        proof.verify_proof(tx, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert e.value.reason == "unsigned"
```

- [ ] **Step 2: Run** — `.venv/bin/pytest tests/test_signing_proof.py -q` → ImportError.
- [ ] **Step 3: Implement**

In `lfg_core/memos.py`, after `ACTION_BRIX_CLAIM`:
```python
ACTION_SIGNIN = "signin"  # #447: never-submitted WalletConnect sign-in proof
ACTION_LINK = "link"  # #447: never-submitted second-wallet link proof
```
and add both names to the `_ACTIONS = frozenset({...})` literal.

```python
# lfg_core/signing/proof.py
# Sign-in / wallet-link proof for providers with no message-signing RPC (#447).
#
# Joey Wallet exposes only xrpl_signTransaction, so ownership is proven by
# signing an AccountSet that can never validate on any chain: Fee "0"
# (temBAD_FEE), Sequence 0 and LastLedgerSequence 0 (already past). The
# server issued nonce rides in a memo. Verification is local (xrpl-py), no
# network, and the field set is an ALLOWLIST — anything else is refused so a
# real transaction can never be smuggled through as a "proof".
from __future__ import annotations

from typing import Any

from xrpl.core import binarycodec, keypairs
from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.utils import hex_to_str, str_to_hex

from lfg_core import config, memos

NONCE_MEMO_TYPE = "lfg/nonce"
_ALLOWED_FIELDS = frozenset(
    {
        "TransactionType", "Account", "Fee", "Sequence", "LastLedgerSequence",
        "SourceTag", "Memos", "SigningPubKey", "TxnSignature", "Flags",
    }
)
_ACTIONS = frozenset({memos.ACTION_SIGNIN, memos.ACTION_LINK})


class ProofError(ValueError):
    code = "bad_proof"

    def __init__(self, reason: str):
        super().__init__(f"bad proof: {reason}")
        self.reason = reason


def build_proof_txjson(wallet: str, nonce: str, action: str) -> dict[str, Any]:
    if action not in _ACTIONS:
        raise ValueError(f"proof action must be signin|link, got {action!r}")
    prov = memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, action)
    return {
        "TransactionType": "AccountSet",
        "Account": wallet,
        "Fee": "0",
        "Sequence": 0,
        "LastLedgerSequence": 0,
        "SourceTag": config.SOURCE_TAG,
        "Memos": prov + [{"Memo": {"MemoType": str_to_hex(NONCE_MEMO_TYPE), "MemoData": str_to_hex(nonce)}}],
    }


def _nonce_from_memos(raw: Any) -> str | None:
    if not isinstance(raw, list):
        return None
    for entry in raw:
        memo = entry.get("Memo") if isinstance(entry, dict) else None
        if not isinstance(memo, dict):
            continue
        try:
            if hex_to_str(memo.get("MemoType", "")) == NONCE_MEMO_TYPE:
                return hex_to_str(memo.get("MemoData", ""))
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def verify_proof(tx_json: dict[str, Any], *, nonce: str, action: str) -> str:
    """Return the wallet the proof establishes ownership of, or raise ProofError."""
    if not isinstance(tx_json, dict):
        raise ProofError("shape")
    extra = set(tx_json) - _ALLOWED_FIELDS
    if extra:
        raise ProofError("unexpected_field")
    if tx_json.get("TransactionType") != "AccountSet":
        raise ProofError("transaction_type")
    if str(tx_json.get("Fee")) != "0":
        raise ProofError("fee")
    if tx_json.get("Sequence") != 0:
        raise ProofError("sequence")
    if tx_json.get("LastLedgerSequence") != 0:
        raise ProofError("last_ledger_sequence")
    if tx_json.get("SourceTag") != config.SOURCE_TAG:
        raise ProofError("source_tag")
    if tx_json.get("Flags") not in (None, 0):
        raise ProofError("flags")
    account = tx_json.get("Account")
    if not isinstance(account, str) or not is_valid_classic_address(account):
        raise ProofError("account")
    if "Memos" not in tx_json:
        raise ProofError("memos")
    decoded = memos.decode_memos([m for m in tx_json["Memos"] if _nonce_from_memos([m]) is None])
    if decoded is None or decoded.get("action") != action:
        raise ProofError("action")
    if _nonce_from_memos(tx_json["Memos"]) != nonce:
        raise ProofError("nonce")
    pub = tx_json.get("SigningPubKey")
    sig = tx_json.get("TxnSignature")
    if not pub or not sig:
        raise ProofError("unsigned")
    try:
        if keypairs.derive_classic_address(pub) != account:
            raise ProofError("pubkey_account")
        signing = binarycodec.encode_for_signing(tx_json)
        ok = keypairs.is_valid_message(bytes.fromhex(signing), bytes.fromhex(sig), pub)
    except ProofError:
        raise
    except Exception as e:  # malformed hex / codec errors
        raise ProofError("signature") from e
    if not ok:
        raise ProofError("signature")
    return account
```

Note: `decode_memos` rejects unknown keys? Check `memos.decode_memos` — if it refuses the `lfg/nonce` key, the filter above (drop the nonce memo before decoding) already handles it; if it rejects duplicates only, the filter is harmless.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_signing_proof.py tests/test_memos.py -q` → all pass (adjust `tests/test_memos*.py` enum-count assertions if any enumerate `_ACTIONS`).
- [ ] **Step 5: Commit** — `git commit -am "feat(signing): AccountSet pseudo-tx ownership proof builder + verifier (#447)"`

---

### Task 3: `WalletConnectProvider` + chokepoint dispatch

**Files:**
- Create: `lfg_core/signing/walletconnect.py`
- Modify: `lfg_core/signing/__init__.py` (registry), `lfg_core/xumm_ops.py` (`_create_xumm_payload`, `get_payload_status`, `cancel_xumm_payload`)
- Test: `tests/test_wc_provider.py`

**Interfaces:**
- Produces:
  ```python
  WC_PREFIX = "wc-"
  class WalletConnectProvider(BaseSigningProvider):  name = "walletconnect"
      async def _create(self, request) -> SignHandle | None   # raw = {uuid, sign_mode:"walletconnect", txjson, xumm_url:None, qr_url:None, pushed:False, push:None, expires_at}
      async def status(self, handle_id) -> SignStatus          # raw = {opened, signed, expired, account, txid, user_token:None, sign_mode, state}
      async def cancel(self, handle_id) -> bool
      def wallet_for(handle_id) -> str | None                  # module fn
  xumm_ops._create_xumm_payload(txjson, options=None, user_token=None, memos_json=None, *, provider="xaman")
  xumm_ops.get_payload_status(uuid, *, force=False)   # "wc-…" routes to the provider
  xumm_ops.cancel_xumm_payload(uuid)                  # "wc-…" routes to the provider
  ```

- [ ] **Step 1: Failing tests**

```python
# tests/test_wc_provider.py
import asyncio

from lfg_core import memos, xumm_ops
from lfg_core.signing import get_provider, store
from lfg_core.signing.walletconnect import WalletConnectProvider

W = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH_FN", lambda network=None: str(tmp_path / "a.db"))


def test_registry_returns_walletconnect():
    assert isinstance(get_provider("walletconnect"), WalletConnectProvider)


def test_create_stores_stamped_txjson_and_returns_handle(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    memo = memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_PAYMENT)
    res = _run(xumm_ops._create_xumm_payload(
        {"TransactionType": "Payment", "Account": W, "Destination": W, "Amount": "1"},
        memos_json=memo, provider="walletconnect"))
    assert res["uuid"].startswith("wc-") and res["sign_mode"] == "walletconnect"
    assert res["xumm_url"] is None and res["qr_url"] is None and res["push"] is None
    assert res["txjson"]["SourceTag"] == 2606160021 and res["txjson"]["Memos"]
    row = store.get(res["uuid"])
    assert row["wallet"] == W and row["purpose"] == "tx" and row["state"] == "pending"


def test_create_without_account_is_refused(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    memo = memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_PAYMENT)
    res = _run(xumm_ops._create_xumm_payload(
        {"TransactionType": "Payment", "Destination": W, "Amount": "1"},
        memos_json=memo, provider="walletconnect"))
    assert res is None  # #314 signer pinning: WC txs MUST carry Account


def test_status_maps_states(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ip=None, ttl_seconds=900)
    s = _run(xumm_ops.get_payload_status(row["id"]))
    assert s["signed"] is False and s["expired"] is False and s["account"] == W and s["txid"] is None
    store.set_state(row["id"], "signed", txid="AB" * 32)
    s = _run(xumm_ops.get_payload_status(row["id"]))
    assert s["signed"] is True and s["txid"] == "AB" * 32 and s["user_token"] is None
    for term in ("rejected", "expired", "mismatch", "failed", "cancelled"):
        store.set_state(row["id"], term)
        s = _run(xumm_ops.get_payload_status(row["id"]))
        assert s["signed"] is False and s["expired"] is True, term


def test_status_unknown_handle_is_none(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert _run(xumm_ops.get_payload_status("wc-does-not-exist")) is None


def test_cancel_routes(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ip=None, ttl_seconds=900)
    assert _run(xumm_ops.cancel_xumm_payload(row["id"])) is True
    assert store.get(row["id"])["state"] == "cancelled"
    assert _run(xumm_ops.cancel_xumm_payload(row["id"])) is False


def test_xaman_default_untouched(monkeypatch):
    called = {}

    async def fake_post(payload):
        called["p"] = payload
        return {"uuid": "u", "pushed": False, "xumm_url": "x", "qr_url": "q"}

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    monkeypatch.setattr(xumm_ops, "watch_payload", lambda u: None)
    memo = memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_PAYMENT)
    res = _run(xumm_ops._create_xumm_payload({"TransactionType": "Payment", "Account": W}, memos_json=memo))
    assert res["uuid"] == "u" and "sign_mode" not in res
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement**

```python
# lfg_core/signing/walletconnect.py
# WalletConnect (Joey Wallet) signing provider (#447).
#
# No server round trip: the stamped txjson is parked in `sign_requests` and
# handed to the browser through the session poll; the browser has Joey sign
# (and submit) it and reports back to POST /api/sign/{id}/result, where the
# service verifies the outcome on-ledger (lfg_core/signing/result.py). The
# handle dict mirrors xumm_ops' so every flow keeps reading uuid/xumm_url/
# push exactly as today — they just come back None here.
from __future__ import annotations

import logging
from typing import Any

from lfg_core import xumm_ops
from lfg_core.signing import store
from lfg_core.signing.base import BaseSigningProvider
from lfg_core.signing.types import SignHandle, SignRequest, SignStatus

WC_PREFIX = "wc-"
TX_TTL_SECONDS = xumm_ops.DEFAULT_EXPIRE_MINUTES * 60
_TERMINAL_FAIL = frozenset({"rejected", "failed", "mismatch", "expired", "cancelled"})


def is_wc_handle(handle_id: Any) -> bool:
    return isinstance(handle_id, str) and handle_id.startswith(WC_PREFIX)


def wallet_for(handle_id: str) -> str | None:
    row = store.get(handle_id)
    return row["wallet"] if row else None


def status_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Same keys xumm_ops.get_payload_status returns."""
    state = row["state"]
    return {
        "opened": state != "pending",
        "signed": state == "signed",
        "expired": state in _TERMINAL_FAIL,
        "account": row["wallet"],
        "txid": row["txid"],
        "user_token": None,
        "sign_mode": "walletconnect",
        "state": state,
    }


class WalletConnectProvider(BaseSigningProvider):
    name = "walletconnect"

    async def _create(self, request: SignRequest) -> SignHandle | None:
        tx = request.txjson
        wallet = tx.get("Account")
        if not isinstance(wallet, str) or not wallet:
            # #314: a WC tx with no pinned signer could be signed by any wallet
            # Joey holds; every builder passes account= — refuse otherwise.
            logging.error(f"walletconnect create refused ({tx.get('TransactionType')}): no Account")
            return None
        row = store.create(wallet=wallet, purpose="tx", txjson=tx, nonce=None, ip=None,
                           ttl_seconds=TX_TTL_SECONDS)
        raw: dict[str, Any] = {
            "uuid": row["id"],
            "sign_mode": "walletconnect",
            "txjson": tx,
            "xumm_url": None,
            "qr_url": None,
            "pushed": False,
            "push": None,
            "expires_at": row["expires_at"],
        }
        logging.info(f"WC sign request {row['id']} ({tx.get('TransactionType')}) for {wallet}")
        return SignHandle(id=row["id"], sign_url=None, qr_url=None, push=None, raw=raw)

    async def status(self, handle_id: str) -> SignStatus:
        row = store.get(handle_id)
        if row is None:
            return SignStatus(signed=None, resolved=False, raw={})
        raw = status_dict(row)
        return SignStatus(
            signed=raw["signed"] if raw["signed"] or raw["expired"] else None,
            resolved=raw["signed"] or raw["expired"],
            txid=raw["txid"],
            signer=raw["account"],
            user_token=None,
            raw=raw,
        )

    async def cancel(self, handle_id: str) -> bool:
        return store.set_state(handle_id, "cancelled", expected_from="pending")
```

Registry (`lfg_core/signing/__init__.py`), in `get_provider` after the `xaman` branch:
```python
    elif key == "walletconnect":
        from lfg_core.signing.walletconnect import WalletConnectProvider

        provider = WalletConnectProvider()
```

`xumm_ops._create_xumm_payload` — add `*, provider: str = "xaman"` to the signature and, as the first statement of the body:
```python
    if provider != "xaman":
        from lfg_core.signing import get_provider  # lazy: avoids the import cycle

        handle = await get_provider(provider).create(
            SignRequest(txjson=txjson, memos_json=memos_json, options=options, user_token=user_token)
        )
        return handle.raw if handle else None
```
(`from lfg_core.signing.types import SignRequest` at module top is safe — `types` has no xumm import.)

`get_payload_status` — first statement:
```python
    if uuid.startswith("wc-") if isinstance(uuid, str) else False:
        from lfg_core.signing import store as _wc_store
        from lfg_core.signing.walletconnect import status_dict

        row = _wc_store.get(uuid)
        return status_dict(row) if row else None
```
`cancel_xumm_payload` — first statement:
```python
    if isinstance(uuid, str) and uuid.startswith("wc-"):
        from lfg_core.signing import store as _wc_store

        return _wc_store.set_state(uuid, "cancelled", expected_from="pending")
```

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_wc_provider.py tests/test_signing_provider.py tests/test_xumm*.py -q` → pass.
- [ ] **Step 5: Commit** — `git commit -am "feat(signing): WalletConnect provider dispatched at the xumm_ops chokepoints (#447)"`

---

### Task 4: Thread `provider` through builders, sessions and the session token

**Files:**
- Modify: `lfg_core/xumm_ops.py` — every `create_*_payload` (lines ~324–627 except `create_signin_payload`) gains `provider: str = "xaman"` and forwards `provider=provider` to `_create_xumm_payload`.
- Modify: `lfg_core/mint_flow.py` (`MintSession.__init__` :91, :107, builder calls :221 and each `user_token=…` site listed by `grep -n push_user_token`), `lfg_core/swap_flow.py` (:133/:143/:188/:613), `lfg_core/market_flow.py` (dataclasses :217/:781 + builder sites), `lfg_core/bulk_mint_flow.py` (:155/:163/:250/:345/:371/:655), `lfg_core/burn2mint_flow.py` (:91/:98/:118/:136/:275/:463).
- Modify: `lfg_service/app.py` — `make_session_token` / `_platform`; new `_provider(user)`; every session constructor site that passes `push_user_token=` also passes `provider=_provider(user)`.
- Test: `tests/test_provider_threading.py`

**Interfaces:**
- Produces: `app._provider(user: dict) -> str` (reads `user.get("provider", "xaman")`); `make_session_token(user)` includes `"provider"`; each session has `.provider: str = "xaman"` and passes it to every builder.

- [ ] **Step 1: Failing test**

```python
# tests/test_provider_threading.py
import inspect
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

import lfg_service.app as app
from lfg_core import bulk_mint_flow, burn2mint_flow, market_flow, mint_flow, swap_flow, xumm_ops

BUILDERS = [
    n for n, f in inspect.getmembers(xumm_ops, inspect.iscoroutinefunction)
    if n.startswith("create_") and n.endswith("_payload") and n != "create_signin_payload"
]


def test_every_builder_accepts_provider():
    for n in BUILDERS:
        assert "provider" in inspect.signature(getattr(xumm_ops, n)).parameters, n


def test_sessions_default_to_xaman():
    assert mint_flow.MintSession(discord_id="u", wallet_address="r", platform="web").provider == "xaman"
    assert market_flow.ListSession(discord_id="u", wallet_address="r", nft_id="n", listing_kind="character").provider == "xaman"
    assert market_flow.BuySession.__dataclass_fields__["provider"].default == "xaman"


def test_session_token_carries_provider():
    tok = app.make_session_token({"id": "r", "name": "n", "platform": "web", "provider": "walletconnect"})
    assert app.verify_session_token(tok)["provider"] == "walletconnect"
    assert app._provider(app.verify_session_token(app.make_session_token({"id": "r", "name": "n"}))) == "xaman"


def test_flow_sources_reference_provider():
    # every flow that builds a payload must forward the session's provider
    for mod in (mint_flow, swap_flow, market_flow, bulk_mint_flow, burn2mint_flow):
        src = inspect.getsource(mod)
        assert src.count("user_token=") <= src.count("provider="), mod.__name__
```

- [ ] **Step 2: Run** → fails on builder signatures.
- [ ] **Step 3: Implement**

Builders: for each `async def create_*_payload(...)` add `provider: str = "xaman",` as the last parameter and pass `provider=provider` into its `_create_xumm_payload(...)` call.

Sessions (pattern, repeat in each file):
```python
# constructor / dataclass
provider: str = "xaman"          # dataclass field (market_flow), or
self.provider = provider         # __init__ kwarg `provider: str = "xaman"` (mint/swap/bulk/burn2mint)
# every builder call that has user_token=self.push_user_token / session.push_user_token:
provider=self.provider,          # resp. provider=session.provider
# to_dict()/from_dict() in bulk_mint_flow + burn2mint_flow:
"provider": self.provider,   /   provider=d.get("provider", "xaman"),
```
Free functions in `mint_flow.py` that take `push_user_token: str | None` (:475, :604, :777) gain `provider: str = "xaman"` and forward it.

`lfg_service/app.py`:
```python
def _provider(user: dict[str, Any]) -> str:
    return user.get("provider", "xaman")
```
`make_session_token`: add `"provider": user.get("provider", "xaman"),` to `payload`.
Constructor sites (`grep -n "push_user_token=push_user_token\|push_user_token=await _push_token" lfg_service/app.py`): add `provider=_provider(user)` (or `_provider(request["user"])`). Inline builder calls in app.py (`grep -n "user_token=" lfg_service/app.py`, ~11 sites) add `provider=_provider(request["user"])`.

- [ ] **Step 4: Run** — `.venv/bin/pytest -q` (full suite; mechanical change, everything must stay green; mypy: `.venv/bin/mypy lfg_core lfg_service`).
- [ ] **Step 5: Commit** — `git commit -am "feat(signing): thread session provider into every payload builder (#447)"`

---

### Task 5: Config + `/api/config`

**Files:**
- Modify: `lfg_core/config.py`, `lfg_service/app.py::handle_config`, `CLAUDE.md` env block, `docs/ops/env.staging.example`
- Test: `tests/test_wc_config.py`

**Interfaces:**
- Produces: `config.REOWN_PROJECT_ID: str` (default `""`), `config.WC_SURFACES: frozenset[str]` (default `{"web","telegram"}`), `config.WC_CHAIN: str` (`"xrpl:1"` if `IS_TESTNET` else `"xrpl:0"`), `config.walletconnect_enabled() -> bool`.

- [ ] **Step 1: Failing test**

```python
# tests/test_wc_config.py
import asyncio, json, os
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")
import lfg_service.app as app
from lfg_core import config


def test_defaults():
    assert config.WC_SURFACES == frozenset({"web", "telegram"})
    assert config.WC_CHAIN in ("xrpl:0", "xrpl:1")


def test_config_omits_block_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "")
    body = json.loads(asyncio.new_event_loop().run_until_complete(app.handle_config(None)).text)
    assert "walletconnect" not in body


def test_config_exposes_block_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "pid")
    body = json.loads(asyncio.new_event_loop().run_until_complete(app.handle_config(None)).text)
    assert body["walletconnect"] == {"project_id": "pid", "chain": config.WC_CHAIN, "surfaces": sorted(config.WC_SURFACES)}
```

- [ ] **Step 2: Run** → AttributeError.
- [ ] **Step 3: Implement** (`config.py`, after `WEB_ALLOWED_ORIGINS`):
```python
# #447 WalletConnect / Joey Wallet. Feature OFF unless a Reown project id is set.
REOWN_PROJECT_ID = os.getenv("REOWN_PROJECT_ID", "").strip()
WC_SURFACES = frozenset(
    s.strip() for s in os.getenv("WC_SURFACES", "web,telegram").split(",") if s.strip()
)
WC_CHAIN = "xrpl:1" if IS_TESTNET else "xrpl:0"


def walletconnect_enabled() -> bool:
    return bool(REOWN_PROJECT_ID)
```
`handle_config`: build the dict, then
```python
    if config.walletconnect_enabled():
        body["walletconnect"] = {"project_id": config.REOWN_PROJECT_ID, "chain": config.WC_CHAIN, "surfaces": sorted(config.WC_SURFACES)}
```
CLAUDE.md env block, add:
```
REOWN_PROJECT_ID=<reown-cloud-project-id>                    # optional (#447); WalletConnect/Joey sign-in + signing — unset = feature off, button hidden
WC_SURFACES=web,telegram                                      # optional (#447); surfaces that show the Joey button (add discord-activity after URL Mappings for relay.walletconnect.com + api.web3modal.org are verified)
```
- [ ] **Step 4: Run** → pass. **Step 5: Commit** — `git commit -am "feat(config): REOWN_PROJECT_ID / WC_SURFACES + /api/config walletconnect block (#447)"`

---

### Task 6: WalletConnect sign-in endpoints

**Files:**
- Modify: `lfg_service/app.py` (`handle_web_signin_start`, new `handle_web_signin_proof`, routes), `tests/test_web_signin_wc.py`

**Interfaces:**
- `POST /api/web/signin {provider:"walletconnect"}` → `{sign_id, nonce, source_tag, chain, expires_at, provider:"walletconnect"}`
- `POST /api/web/signin/proof {sign_id, tx_json}` → same body as the Xaman `signed` response + `provider`.
- Produces `app._wc_disabled_response()`, `app._nonce() -> str` (64 hex).

- [ ] **Step 1: Failing tests**

```python
# tests/test_web_signin_wc.py
import asyncio, json, os, time
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")
from xrpl.core import binarycodec, keypairs
from xrpl.wallet import Wallet
import lfg_service.app as app
from lfg_core import config, memos
from lfg_core.signing import proof, store
from lfg_service import identity


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _Req:
    def __init__(self, body=None, headers=None, match=None, remote="1.2.3.4"):
        self._body, self.headers, self.match_info, self.remote, self._store = body or {}, headers or {}, match or {}, remote, {}
    async def json(self):
        return self._body
    def __getitem__(self, k):
        return self._store[k]
    def __setitem__(self, k, v):
        self._store[k] = v


def _sign(tx, w):
    tx = dict(tx); tx["SigningPubKey"] = w.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(binarycodec.encode_for_signing(tx)), w.private_key)
    return tx


def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH_FN", lambda network=None: str(tmp_path / "a.db"))
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "ids.db")); identity.ensure_identities_table()
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "pid")
    app._web_signin_hits.clear()


def test_start_disabled_503(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); monkeypatch.setattr(config, "REOWN_PROJECT_ID", "")
    r = _run(app.handle_web_signin_start(_Req({"provider": "walletconnect"})))
    assert r.status == 503 and json.loads(r.text)["code"] == "wc_disabled"


def test_start_and_proof_bootstraps_session(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = _run(app.handle_web_signin_start(_Req({"provider": "walletconnect"})))
    s = json.loads(r.text)
    assert s["sign_id"].startswith("wc-") and len(s["nonce"]) == 64 and s["chain"] == config.WC_CHAIN
    w = Wallet.create()
    tx = _sign(proof.build_proof_txjson(w.classic_address, s["nonce"], memos.ACTION_SIGNIN), w)
    r2 = _run(app.handle_web_signin_proof(_Req({"sign_id": s["sign_id"], "tx_json": tx})))
    body = json.loads(r2.text)
    assert r2.status == 200 and body["state"] == "signed" and body["wallet"] == w.classic_address
    tok = app.verify_session_token(body["session_token"])
    assert tok["platform"] == "web" and tok["provider"] == "walletconnect" and tok["id"] == w.classic_address
    assert identity.resolve("web", w.classic_address) == w.classic_address
    # replay
    r3 = _run(app.handle_web_signin_proof(_Req({"sign_id": s["sign_id"], "tx_json": tx})))
    assert r3.status == 409 and json.loads(r3.text)["code"] == "proof_replayed"


def test_bad_proof_400_with_reason(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    s = json.loads(_run(app.handle_web_signin_start(_Req({"provider": "walletconnect"}))).text)
    w = Wallet.create()
    tx = _sign(proof.build_proof_txjson(w.classic_address, "ff" * 32, memos.ACTION_SIGNIN), w)
    r = _run(app.handle_web_signin_proof(_Req({"sign_id": s["sign_id"], "tx_json": tx})))
    assert r.status == 400 and json.loads(r.text) == {"error": "bad proof", "code": "bad_proof", "reason": "nonce"}
    assert store.get(s["sign_id"])["state"] == "pending"  # a bad attempt does not burn the nonce


def test_expired_410(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    s = json.loads(_run(app.handle_web_signin_start(_Req({"provider": "walletconnect"}))).text)
    monkeypatch.setattr(app.time, "time", lambda: time.time() + app.SIGNIN_TTL + 1)
    r = _run(app.handle_web_signin_proof(_Req({"sign_id": s["sign_id"], "tx_json": {}})))
    assert r.status == 410 and json.loads(r.text)["code"] == "proof_expired"


def test_unknown_sign_id_404(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = _run(app.handle_web_signin_proof(_Req({"sign_id": "wc-x", "tx_json": {}})))
    assert r.status == 404


def test_xaman_default_path_unchanged(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    async def fake(return_url=None):
        return {"uuid": "u-1", "xumm_url": "https://xumm.app/sign/u-1"}
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake)
    r = _run(app.handle_web_signin_start(_Req({})))
    assert json.loads(r.text) == {"uuid": "u-1", "signin_link": "https://xumm.app/sign/u-1"}
```

- [ ] **Step 2: Run** → AttributeError `handle_web_signin_proof`.
- [ ] **Step 3: Implement** (app.py, next to the web sign-in handlers):

```python
def _wc_disabled_response():
    return web.json_response({"error": "WalletConnect sign-in is not enabled", "code": "wc_disabled"}, status=503)


def _nonce() -> str:
    return secrets.token_hex(32)


def _web_session_response(wallet: str, provider: str) -> web.Response:
    """Shared tail of both web sign-in paths: identity link + session token."""
    handle = identity_store.handle_for_wallet(wallet)
    name = handle or f"{wallet[:6]}…{wallet[-4:]}"
    if not identity_store.link("web", wallet, name, wallet):
        return web.json_response({"error": "identity link failed"}, status=500)
    token = make_session_token({"id": wallet, "name": name, "platform": "web", "provider": provider})
    return web.json_response({"state": "signed", "wallet": wallet, "session_token": token,
                              "provider": provider, "user": {"id": wallet, "username": name}})
```
(`import secrets` at top.) In `handle_web_signin_start`, after the rate-limit check and before the XUMM branch:
```python
    body = await request.json() if request.can_read_body else {}
    if (body or {}).get("provider") == "walletconnect":
        if not config.walletconnect_enabled():
            return _wc_disabled_response()
        row = await asyncio.to_thread(
            sign_store.create, wallet="", purpose="signin", txjson=None, nonce=_nonce(),
            ip=_client_ip(request), ttl_seconds=SIGNIN_TTL,
        )
        return web.json_response({"provider": "walletconnect", "sign_id": row["id"], "nonce": row["nonce"],
                                  "source_tag": config.SOURCE_TAG, "chain": config.WC_CHAIN,
                                  "expires_at": row["expires_at"]})
```
(The existing `_Req` test helper has no `can_read_body`; use `body = await request.json()` wrapped in `try/except Exception: body = {}` instead of `can_read_body`. `from lfg_core.signing import store as sign_store` at top.)

```python
async def _verify_proof_request(request, *, purpose: str, action: str):
    """Shared by sign-in and link: returns (row, wallet) or an error Response."""
    body = await request.json()
    sign_id = str(body.get("sign_id") or "")
    row = await asyncio.to_thread(sign_store.get, sign_id)
    if not row or row["purpose"] != purpose:
        return web.json_response({"error": "not found"}, status=404)
    if row["state"] != "pending":
        return web.json_response({"error": "proof already used", "code": "proof_replayed"}, status=409)
    if row["expires_at"] < time.time():
        await asyncio.to_thread(sign_store.set_state, row["id"], "expired", expected_from="pending")
        return web.json_response({"error": "proof expired", "code": "proof_expired"}, status=410)
    try:
        wallet = signing_proof.verify_proof(body.get("tx_json"), nonce=row["nonce"], action=action)
    except signing_proof.ProofError as e:
        logging.warning(f"{purpose} proof {sign_id} rejected: {e.reason}")
        return web.json_response({"error": "bad proof", "code": "bad_proof", "reason": e.reason}, status=400)
    return row, wallet


async def handle_web_signin_proof(request):
    if not config.walletconnect_enabled():
        return _wc_disabled_response()
    res = await _verify_proof_request(request, purpose="signin", action=memos.ACTION_SIGNIN)
    if isinstance(res, web.Response):
        return res
    row, wallet = res
    if not await asyncio.to_thread(sign_store.set_state, row["id"], "consumed", expected_from="pending"):
        return web.json_response({"error": "proof already used", "code": "proof_replayed"}, status=409)
    return await asyncio.to_thread(_web_session_response, wallet, "walletconnect")
```
Route: `app.router.add_post("/api/web/signin/proof", handle_web_signin_proof)` **before** the `/api/web/signin/{payload_uuid}` GET (different method, but keep it adjacent). `from lfg_core.signing import proof as signing_proof`.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_web_signin_wc.py tests/test_web_signin_endpoint.py -q` → pass.
- [ ] **Step 5: Commit** — `git commit -am "feat(web): WalletConnect sign-in via signed pseudo-tx proof (#447)"`

---

### Task 7: `POST /api/sign/{id}/result` — on-ledger verification

**Files:**
- Create: `lfg_core/signing/result.py`
- Modify: `lfg_service/app.py` (handler + route)
- Test: `tests/test_sign_result.py`

**Interfaces:**
```python
# lfg_core/signing/result.py
AUTOFILL_FIELDS = frozenset({"Fee","Sequence","LastLedgerSequence","SigningPubKey","TxnSignature","hash","NetworkID","TicketSequence","AccountTxnID","Signers","date","ledger_index","inLedger","validated","meta"})
class Verdict:  ok: bool; code: str   # "signed" | "pending" | "not_found" | "mismatch" | "wrong_account"
def compare(expected: dict, onledger: dict) -> bool          # semantic equality minus AUTOFILL_FIELDS; `Flags` compared only if set in expected
async def verify_submitted(row: dict, tx_hash: str) -> Verdict   # uses xrpl_ops.get_tx
```
Endpoint: `POST /api/sign/{sign_id}/result` (authed) body `{hash}` | `{rejected:true}` | `{error:"…"}` → `{state}`; 202 while pending, 409 `tx_mismatch`, 403 `not_your_request`, 410 `expired`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_sign_result.py
import asyncio, json, os
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")
import pytest
import lfg_service.app as app
from lfg_core import xrpl_ops
from lfg_core.signing import result, store

W = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"
OTHER = "rrrrrrrrrrrrrrrrrrrrBZbvji"
H = "AB" * 32
TX = {"TransactionType": "Payment", "Account": W, "Destination": OTHER, "Amount": "1000", "SourceTag": 2606160021}


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _Req:
    def __init__(self, body, match, user):
        self._body, self.match_info, self.headers, self._store = body, match, {"Authorization": "Bearer x"}, {"user": user}
    async def json(self):
        return self._body
    def __getitem__(self, k):
        return self._store[k]
    def __setitem__(self, k, v):
        self._store[k] = v


def _env(tmp_path, monkeypatch, onledger):
    monkeypatch.setattr(store, "DB_PATH_FN", lambda network=None: str(tmp_path / "a.db"))
    async def fake_get_tx(h):
        return onledger
    monkeypatch.setattr(xrpl_ops, "get_tx", fake_get_tx)
    monkeypatch.setattr(app, "verify_session_token", lambda t: {"id": W, "name": "n", "platform": "web", "provider": "walletconnect"})
    return store.create(wallet=W, purpose="tx", txjson=TX, nonce=None, ip=None, ttl_seconds=900)


def test_compare_ignores_autofill_and_matches_semantics():
    assert result.compare(TX, {**TX, "Fee": "12", "Sequence": 9, "LastLedgerSequence": 1, "SigningPubKey": "ED", "TxnSignature": "AA", "hash": H, "Flags": 0})
    assert not result.compare(TX, {**TX, "Amount": "1"})
    assert not result.compare(TX, {**TX, "Destination": W})
    assert not result.compare({**TX, "Flags": 1}, {**TX, "Flags": 0})


def test_hash_verified_marks_signed(tmp_path, monkeypatch):
    row = _env(tmp_path, monkeypatch, {"validated": True, "hash": H, **TX, "Fee": "12", "Sequence": 3, "meta": {"TransactionResult": "tesSUCCESS"}})
    r = _run(app.handle_sign_result(_Req({"hash": H}, {"sign_id": row["id"]}, {"id": W})))
    assert r.status == 200 and json.loads(r.text)["state"] == "signed"
    assert store.get(row["id"])["txid"] == H


def test_unvalidated_is_202_and_stays_pending(tmp_path, monkeypatch):
    row = _env(tmp_path, monkeypatch, {"error": "txnNotFound"})
    r = _run(app.handle_sign_result(_Req({"hash": H}, {"sign_id": row["id"]}, {"id": W})))
    assert r.status == 202 and store.get(row["id"])["state"] == "pending"


def test_wrong_account_is_mismatch(tmp_path, monkeypatch):
    row = _env(tmp_path, monkeypatch, {"validated": True, **TX, "Account": OTHER, "meta": {}})
    r = _run(app.handle_sign_result(_Req({"hash": H}, {"sign_id": row["id"]}, {"id": W})))
    assert r.status == 409 and store.get(row["id"])["state"] == "mismatch"


def test_semantic_mismatch(tmp_path, monkeypatch):
    row = _env(tmp_path, monkeypatch, {"validated": True, **TX, "Amount": "1", "meta": {}})
    r = _run(app.handle_sign_result(_Req({"hash": H}, {"sign_id": row["id"]}, {"id": W})))
    assert r.status == 409 and json.loads(r.text)["code"] == "tx_mismatch"


def test_foreign_session_403(tmp_path, monkeypatch):
    row = _env(tmp_path, monkeypatch, {})
    monkeypatch.setattr(app, "verify_session_token", lambda t: {"id": OTHER, "name": "n", "platform": "web", "provider": "walletconnect"})
    r = _run(app.handle_sign_result(_Req({"hash": H}, {"sign_id": row["id"]}, {"id": OTHER})))
    assert r.status == 403


def test_rejected_and_error(tmp_path, monkeypatch):
    row = _env(tmp_path, monkeypatch, {})
    r = _run(app.handle_sign_result(_Req({"rejected": True}, {"sign_id": row["id"]}, {"id": W})))
    assert json.loads(r.text)["state"] == "rejected"
    row2 = store.create(wallet=W, purpose="tx", txjson=TX, nonce=None, ip=None, ttl_seconds=900)
    r = _run(app.handle_sign_result(_Req({"error": "boom"}, {"sign_id": row2["id"]}, {"id": W})))
    assert json.loads(r.text)["state"] == "failed"


def test_bad_hash_400(tmp_path, monkeypatch):
    row = _env(tmp_path, monkeypatch, {})
    r = _run(app.handle_sign_result(_Req({"hash": "zz"}, {"sign_id": row["id"]}, {"id": W})))
    assert r.status == 400
```

- [ ] **Step 2: Run** → ImportError / AttributeError.
- [ ] **Step 3: Implement**

```python
# lfg_core/signing/result.py
# On-ledger verification of a WalletConnect-submitted transaction (#447).
# The browser reports only a hash; everything that matters is re-read from
# the ledger and compared to the txjson WE built and parked in sign_requests.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lfg_core import xrpl_ops

AUTOFILL_FIELDS = frozenset(
    {"Fee", "Sequence", "LastLedgerSequence", "SigningPubKey", "TxnSignature", "hash", "NetworkID",
     "TicketSequence", "AccountTxnID", "Signers", "date", "ledger_index", "inLedger", "validated",
     "meta", "ctid", "tx_json", "close_time_iso", "ledger_hash"}
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    code: str  # signed | pending | not_found | mismatch | wrong_account
    result_code: str | None = None


def _norm(v: Any) -> Any:
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


def compare(expected: dict[str, Any], onledger: dict[str, Any]) -> bool:
    for k, v in expected.items():
        if k in AUTOFILL_FIELDS:
            continue
        if k == "Flags" and not v:
            continue
        if _norm(onledger.get(k)) != _norm(v):
            return False
    return True


async def verify_submitted(row: dict[str, Any], tx_hash: str) -> Verdict:
    tx = await xrpl_ops.get_tx(tx_hash)
    # rippled `tx` may nest the transaction under tx_json (api_version 2)
    body = dict(tx.get("tx_json") or tx)
    if not tx.get("validated"):
        return Verdict(False, "not_found" if tx.get("error") == "txnNotFound" else "pending")
    if body.get("Account") != row["wallet"]:
        return Verdict(False, "wrong_account")
    if not compare(row["txjson"] or {}, body):
        return Verdict(False, "mismatch")
    meta = tx.get("meta") or {}
    return Verdict(True, "signed", meta.get("TransactionResult"))
```

`lfg_service/app.py`:
```python
_HASH_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


@require_auth
async def handle_sign_result(request):
    """WalletConnect path (#447): the browser reports what Joey did. The
    server believes none of it — a hash is re-read from the ledger and
    compared to the txjson it parked in sign_requests."""
    sign_id = request.match_info["sign_id"]
    row = await asyncio.to_thread(sign_store.get, sign_id)
    if not row or row["purpose"] != "tx":
        return web.json_response({"error": "not found"}, status=404)
    if row["wallet"] != request["user"]["id"]:
        return web.json_response({"error": "not your request", "code": "not_your_request"}, status=403)
    if row["state"] != "pending":
        return web.json_response({"state": row["state"]})
    body = await request.json()
    if body.get("rejected"):
        await asyncio.to_thread(sign_store.set_state, sign_id, "rejected", expected_from="pending")
        return web.json_response({"state": "rejected"})
    if body.get("error"):
        await asyncio.to_thread(sign_store.set_state, sign_id, "failed", result_json={"error": str(body["error"])[:500]}, expected_from="pending")
        return web.json_response({"state": "failed"})
    tx_hash = str(body.get("hash") or "").upper()
    if not _HASH_RE.match(tx_hash):
        return web.json_response({"error": "bad hash", "code": "bad_hash"}, status=400)
    try:
        v = await signing_result.verify_submitted(row, tx_hash)
    except Exception as e:
        logging.warning(f"sign result {sign_id}: ledger lookup failed: {e}")
        return web.json_response({"state": "pending", "code": "tx_not_found"}, status=202)
    if v.ok:
        await asyncio.to_thread(sign_store.set_state, sign_id, "signed", txid=tx_hash,
                                result_json={"result": v.result_code}, expected_from="pending")
        return web.json_response({"state": "signed", "txid": tx_hash})
    if v.code in ("pending", "not_found"):
        if row["expires_at"] < time.time():
            await asyncio.to_thread(sign_store.set_state, sign_id, "expired", expected_from="pending")
            return web.json_response({"state": "expired", "code": "tx_not_found"}, status=410)
        return web.json_response({"state": "pending", "code": "tx_not_found"}, status=202)
    logging.warning(f"sign result {sign_id}: {v.code} for hash {tx_hash} (wallet {row['wallet']})")
    await asyncio.to_thread(sign_store.set_state, sign_id, "mismatch", txid=tx_hash,
                            result_json={"code": v.code}, expected_from="pending")
    return web.json_response({"state": "mismatch", "code": "tx_mismatch"}, status=409)
```
Route: `app.router.add_post("/api/sign/{sign_id}/result", handle_sign_result)`. `from lfg_core.signing import result as signing_result`.

- [ ] **Step 4: Run** → pass. **Step 5: Commit** — `git commit -am "feat(web): POST /api/sign/{id}/result verifies WalletConnect submissions on-ledger (#447)"`

---

### Task 8: `wallet_proof_links` + BFS edge + link endpoints

**Files:**
- Modify: `lfg_service/identity.py` (`ensure_identities_table`, new `link_wallets_by_proof`, `_bucket_bfs`), `lfg_service/app.py` (3 handlers + routes)
- Test: `tests/test_identity_proof_links.py`, `tests/test_wallet_link_endpoint.py`

**Interfaces:**
```python
identity.link_wallets_by_proof(wallet_a: str, wallet_b: str, proof_kind: str) -> bool   # INSERT OR IGNORE, lexically ordered pair
```
Endpoints (all `@require_wallet`):
- `POST /api/wallet/link {provider:"walletconnect"}` → `{sign_id, nonce, source_tag, chain, expires_at}`
- `POST /api/wallet/link/proof {sign_id, tx_json}` → `{state:"linked", wallet, bucket}`
- `POST /api/wallet/link {provider:"xaman"}` → `{uuid, signin_link}` (XUMM SignIn); `GET /api/wallet/link/{uuid}` → `{state:"pending"|"linked"|"expired", wallet?}`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_identity_proof_links.py
import sqlite3
from lfg_service import identity

A, B, C = "rAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "rBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", "rCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "ids.db")); identity.ensure_identities_table()


def test_link_is_ordered_and_idempotent(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert identity.link_wallets_by_proof(B, A, "wc-signed-tx")
    assert identity.link_wallets_by_proof(A, B, "wc-signed-tx")
    rows = sqlite3.connect(identity.DATABASE).execute("SELECT wallet_a, wallet_b, proof_kind FROM wallet_proof_links").fetchall()
    assert rows == [(A, B, "wc-signed-tx")]


def test_proof_link_merges_buckets_both_directions(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    identity.link("web", A, "a", A); identity.link("discord", "d1", "d", B)
    assert identity.bucket_for("web", A)["wallets"] == [A]
    identity.link_wallets_by_proof(A, B, "wc-signed-tx")
    assert identity.bucket_for("web", A)["wallets"] == [A, B]
    assert identity.bucket_for("discord", "d1")["identities"] == [{"platform": "discord", "platform_user_id": "d1"}, {"platform": "web", "platform_user_id": A}]


def test_chain_of_proof_links(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    identity.link("web", A, "a", A)
    identity.link_wallets_by_proof(A, B, "wc-signed-tx"); identity.link_wallets_by_proof(C, B, "xaman-signin")
    assert identity.bucket_for_wallet(C)["wallets"] == [A, B, C]


def test_self_link_refused(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert identity.link_wallets_by_proof(A, A, "wc-signed-tx") is False
```

```python
# tests/test_wallet_link_endpoint.py
import asyncio, json, os
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")
from xrpl.core import binarycodec, keypairs
from xrpl.wallet import Wallet
import lfg_service.app as app
from lfg_core import config, memos
from lfg_core.signing import proof, store
from lfg_service import identity

W = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _Req:
    def __init__(self, body=None, match=None):
        self._body, self.match_info, self.headers, self.remote, self._store = body or {}, match or {}, {"Authorization": "Bearer x"}, "1.1.1.1", {}
    async def json(self):
        return self._body
    def __getitem__(self, k):
        return self._store[k]
    def __setitem__(self, k, v):
        self._store[k] = v


def _sign(tx, w):
    tx = dict(tx); tx["SigningPubKey"] = w.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(binarycodec.encode_for_signing(tx)), w.private_key)
    return tx


def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH_FN", lambda network=None: str(tmp_path / "a.db"))
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "ids.db")); identity.ensure_identities_table()
    identity.link("web", W, "me", W)
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "pid")
    monkeypatch.setattr(app, "verify_session_token", lambda t: {"id": W, "name": "me", "platform": "web", "provider": "walletconnect"})
    app.web_link_payloads.clear()


def test_wc_link_roundtrip(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    s = json.loads(_run(app.handle_wallet_link_start(_Req({"provider": "walletconnect"}))).text)
    w = Wallet.create()
    tx = _sign(proof.build_proof_txjson(w.classic_address, s["nonce"], memos.ACTION_LINK), w)
    r = _run(app.handle_wallet_link_proof(_Req({"sign_id": s["sign_id"], "tx_json": tx})))
    body = json.loads(r.text)
    assert r.status == 200 and body["state"] == "linked" and body["wallet"] == w.classic_address
    assert set(body["bucket"]["wallets"]) == {W, w.classic_address}


def test_same_wallet_400(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    s = json.loads(_run(app.handle_wallet_link_start(_Req({"provider": "walletconnect"}))).text)
    w = Wallet.create()
    monkeypatch.setattr(app, "verify_session_token", lambda t: {"id": w.classic_address, "name": "me", "platform": "web", "provider": "walletconnect"})
    identity.link("web", w.classic_address, "x", w.classic_address)
    tx = _sign(proof.build_proof_txjson(w.classic_address, s["nonce"], memos.ACTION_LINK), w)
    r = _run(app.handle_wallet_link_proof(_Req({"sign_id": s["sign_id"], "tx_json": tx})))
    assert r.status == 400 and json.loads(r.text)["code"] == "same_wallet"


def test_signin_proof_cannot_be_used_as_link(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    s = json.loads(_run(app.handle_wallet_link_start(_Req({"provider": "walletconnect"}))).text)
    w = Wallet.create()
    tx = _sign(proof.build_proof_txjson(w.classic_address, s["nonce"], memos.ACTION_SIGNIN), w)
    r = _run(app.handle_wallet_link_proof(_Req({"sign_id": s["sign_id"], "tx_json": tx})))
    assert r.status == 400 and json.loads(r.text)["reason"] == "action"


def test_xaman_link_path(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    other = Wallet.create().classic_address
    async def fake_create(return_url=None):
        return {"uuid": "u-9", "xumm_url": "https://xumm.app/sign/u-9"}
    async def fake_status(uuid):
        return {"opened": True, "signed": True, "expired": False, "account": other, "txid": None, "user_token": None}
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    s = json.loads(_run(app.handle_wallet_link_start(_Req({"provider": "xaman"}))).text)
    assert s["uuid"] == "u-9"
    r = _run(app.handle_wallet_link_status(_Req(match={"payload_uuid": "u-9"})))
    body = json.loads(r.text)
    assert body["state"] == "linked" and body["wallet"] == other
    assert set(identity.bucket_for_wallet(W)["wallets"]) == {W, other}
```

- [ ] **Step 2: Run** → failures.
- [ ] **Step 3: Implement**

`identity.py` — in `ensure_identities_table`, after the `wallet_token_links` index:
```python
        # #447: explicit, user-proven wallet<->wallet edges (a signed
        # never-submitted pseudo-tx from the second wallet while signed in as
        # the first). Append-only; unlinking is an admin DELETE.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_proof_links (
                wallet_a   TEXT NOT NULL,
                wallet_b   TEXT NOT NULL,
                proof_kind TEXT NOT NULL,
                linked_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wallet_a, wallet_b)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_proof_links_b ON wallet_proof_links(wallet_b)")
```
New function:
```python
def link_wallets_by_proof(wallet_a: str, wallet_b: str, proof_kind: str) -> bool:
    if wallet_a == wallet_b:
        return False
    a, b = sorted((wallet_a, wallet_b))
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            "INSERT OR IGNORE INTO wallet_proof_links (wallet_a, wallet_b, proof_kind) VALUES (?, ?, ?)",
            (a, b, proof_kind),
        )
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"identity.link_wallets_by_proof failed: {e}")
        return False
    finally:
        conn.close()
```
`_bucket_bfs` — after the token-sibling loop inside `for w in new_wallets:`:
```python
            # wallets -> proof-linked wallets (#447), both directions
            for (w2,) in conn.execute(
                "SELECT wallet_b FROM wallet_proof_links WHERE wallet_a = ? "
                "UNION SELECT wallet_a FROM wallet_proof_links WHERE wallet_b = ?",
                (w, w),
            ):
                if w2 not in wallets:
                    wallets.add(w2)
                    frontier_wallets.append(w2)
```
Update the docstring ("over THREE edge types").

`app.py`:
```python
web_link_payloads: dict[str, dict[str, Any]] = {}  # xaman link SignIn uuid -> {wallet, created_at}


@require_wallet
async def handle_wallet_link_start(request):
    body = await request.json()
    provider = (body or {}).get("provider", "xaman")
    if provider == "walletconnect":
        if not config.walletconnect_enabled():
            return _wc_disabled_response()
        row = await asyncio.to_thread(sign_store.create, wallet=request["wallet"], purpose="link",
                                      txjson=None, nonce=_nonce(), ip=_client_ip(request), ttl_seconds=SIGNIN_TTL)
        return web.json_response({"provider": "walletconnect", "sign_id": row["id"], "nonce": row["nonce"],
                                  "source_tag": config.SOURCE_TAG, "chain": config.WC_CHAIN, "expires_at": row["expires_at"]})
    origin = request.headers.get("Origin", "")
    return_url = {"app": origin, "web": origin} if origin in config.WEB_ALLOWED_ORIGINS else None
    payload = await xumm_ops.create_signin_payload(return_url=return_url)
    if not payload:
        return _xumm_unavailable_response()
    web_link_payloads[payload["uuid"]] = {"wallet": request["wallet"], "created_at": time.time()}
    return web.json_response({"provider": "xaman", "uuid": payload["uuid"], "signin_link": payload["xumm_url"]})


def _finish_link(session_wallet: str, proven: str, proof_kind: str) -> web.Response:
    if proven == session_wallet:
        return web.json_response({"error": "that is already your signed-in wallet", "code": "same_wallet"}, status=400)
    if not identity_store.link_wallets_by_proof(session_wallet, proven, proof_kind):
        return web.json_response({"error": "link failed"}, status=500)
    return web.json_response({"state": "linked", "wallet": proven, "bucket": identity_store.bucket_for_wallet(session_wallet)})


@require_wallet
async def handle_wallet_link_proof(request):
    if not config.walletconnect_enabled():
        return _wc_disabled_response()
    res = await _verify_proof_request(request, purpose="link", action=memos.ACTION_LINK)
    if isinstance(res, web.Response):
        return res
    row, proven = res
    if row["wallet"] != request["wallet"]:
        return web.json_response({"error": "not your request", "code": "not_your_request"}, status=403)
    if not await asyncio.to_thread(sign_store.set_state, row["id"], "consumed", expected_from="pending"):
        return web.json_response({"error": "proof already used", "code": "proof_replayed"}, status=409)
    return await asyncio.to_thread(_finish_link, request["wallet"], proven, "wc-signed-tx")


@require_wallet
async def handle_wallet_link_status(request):
    uuid = request.match_info["payload_uuid"]
    rec = web_link_payloads.get(uuid)
    if not rec or rec["wallet"] != request["wallet"]:
        return web.json_response({"error": "not found"}, status=404)
    if rec["created_at"] + SIGNIN_TTL < time.time():
        del web_link_payloads[uuid]
        return web.json_response({"state": "expired"})
    s = await xumm_ops.get_payload_status(uuid)
    if not s:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    if s["signed"] and s.get("account") and is_valid_classic_address(s["account"]):
        del web_link_payloads[uuid]
        return await asyncio.to_thread(_finish_link, request["wallet"], s["account"], "xaman-signin")
    if s["expired"]:
        del web_link_payloads[uuid]
        return web.json_response({"state": "expired"})
    return web.json_response({"state": "opened" if s["opened"] else "pending"})
```
Routes:
```python
    app.router.add_post("/api/wallet/link", handle_wallet_link_start)
    app.router.add_post("/api/wallet/link/proof", handle_wallet_link_proof)
    app.router.add_get("/api/wallet/link/{payload_uuid}", handle_wallet_link_status)
```
(`require_wallet` short-circuits in `WEBAPP_DEV_MODE`; tests run with it off — confirm `config.WEBAPP_DEV_MODE` is False under `conftest.py`, else monkeypatch it.)

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_identity_proof_links.py tests/test_wallet_link_endpoint.py tests/test_identity*.py -q` → pass.
- [ ] **Step 5: Commit** — `git commit -am "feat(identity): wallet_proof_links edge + /api/wallet/link endpoints (#447)"`

---

### Task 9: Client — `wc.js` wrapper

**Files:**
- Create: `webapp/client/wc.js`
- Test: `webapp/client/wc_pure.js` (pure helpers) + `tests/test_wc_pure.py` (node-driven, same pattern as existing `*_pure.js` tests — check `tests/test_signdelivery_pure.py` for the harness).

**Interfaces (ES module exports):**
```js
// wc_pure.js (pure, testable)
export function accountFromSession(session)            // "xrpl:1:rXXX" → "rXXX" | null
export function proofTx({ wallet, nonce, sourceTag, action, memos }) // builds the AccountSet exactly like proof.build_proof_txjson (memos array supplied by server)
export function isWcSession(s)                          // s.sign_mode === 'walletconnect'
// wc.js
export async function wcConnect(cfg)                    // → { topic, wallet }  (opens the modal)
export async function wcRestore(cfg)                    // → { topic, wallet } | null (from localStorage 'lfg_wc_topic')
export async function wcSign(cfg, topic, txJson, { submit })  // → { tx_json, hash? }
export async function wcDisconnect(cfg, topic)
```

- [ ] **Step 1: Failing test** — `tests/test_wc_pure.py` runs node on `wc_pure.js`:
```python
# tests/test_wc_pure.py
import json, pathlib, shutil, subprocess
import pytest
NODE = shutil.which("node")
P = pathlib.Path("webapp/client/wc_pure.js").resolve()
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")

def _js(expr):
    out = subprocess.run([NODE, "--input-type=module", "-e", f"import * as m from '{P.as_uri()}'; console.log(JSON.stringify({expr}))"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)

def test_account_from_session():
    assert _js("m.accountFromSession({namespaces:{xrpl:{accounts:['xrpl:1:rABC']}}})") == "rABC"
    assert _js("m.accountFromSession({})") is None

def test_proof_tx_shape():
    tx = _js("m.proofTx({wallet:'rA', nonce:'00', sourceTag:2606160021, memos:[{Memo:{MemoType:'AA',MemoData:'BB'}}]})")
    assert tx == {"TransactionType": "AccountSet", "Account": "rA", "Fee": "0", "Sequence": 0, "LastLedgerSequence": 0, "SourceTag": 2606160021,
                  "Memos": [{"Memo": {"MemoType": "AA", "MemoData": "BB"}}, {"Memo": {"MemoType": "6C66672F6E6F6E6365", "MemoData": "3030"}}]}
```
Server side: make the sign-in/link start responses also return `memos` (the provenance memo array from `memos.build_memos_json(INITIATOR_USER, PLATFORM_WEBAPP, action)`) so the client never hand-rolls the enum — add `"memos": memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_SIGNIN)` (resp. `ACTION_LINK`) to both start responses in Tasks 6/8 and assert it in their tests.

- [ ] **Step 2: Run** → fails (file missing).
- [ ] **Step 3: Implement**

```js
// webapp/client/wc_pure.js — pure helpers for the WalletConnect path (#447). No DOM, no network.
const NONCE_MEMO_TYPE_HEX = '6C66672F6E6F6E6365'; // hex("lfg/nonce")

export function hexOf(s) {
  return Array.from(new TextEncoder().encode(s)).map((b) => b.toString(16).padStart(2, '0')).join('').toUpperCase();
}

export function accountFromSession(session) {
  const accounts = session?.namespaces?.xrpl?.accounts || [];
  const parts = (accounts[0] || '').split(':');
  return parts.length === 3 ? parts[2] : null;
}

export function proofTx({ wallet, nonce, sourceTag, memos }) {
  return {
    TransactionType: 'AccountSet',
    Account: wallet,
    Fee: '0',
    Sequence: 0,
    LastLedgerSequence: 0,
    SourceTag: sourceTag,
    Memos: [...memos, { Memo: { MemoType: NONCE_MEMO_TYPE_HEX, MemoData: hexOf(nonce) } }],
  };
}

export function isWcSession(s) {
  return !!s && s.sign_mode === 'walletconnect';
}
```

```js
// webapp/client/wc.js — WalletConnect v2 / Joey Wallet client (#447).
// Loaded lazily (dynamic import) so Xaman users never pay for the bundle.
import { accountFromSession } from './wc_pure.js?v=1';

const TOPIC_KEY = 'lfg_wc_topic';
let clientPromise = null;

async function client(cfg) {
  if (!clientPromise) {
    clientPromise = (async () => {
      const { SignClient, WalletConnectModal } = await import('./vendor/walletconnect.js');
      const sc = await SignClient.init({
        projectId: cfg.project_id,
        metadata: { name: "Let's Effing Go!", description: 'LFG NFT dress-up', url: location.origin, icons: [`${location.origin}/assets/icon-192.png`] },
      });
      const modal = new WalletConnectModal({ projectId: cfg.project_id, chains: [cfg.chain] });
      return { sc, modal };
    })();
  }
  return clientPromise;
}

function namespaces(cfg) {
  return { xrpl: { chains: [cfg.chain], methods: ['xrpl_signTransaction'], events: [] } };
}

export async function wcConnect(cfg) {
  const { sc, modal } = await client(cfg);
  const { uri, approval } = await sc.connect({ requiredNamespaces: namespaces(cfg) });
  if (uri) await modal.openModal({ uri });
  try {
    const session = await approval();
    const wallet = accountFromSession(session);
    if (!wallet) throw new Error('Joey returned no XRPL account');
    try { localStorage.setItem(TOPIC_KEY, session.topic); } catch (_) { /* private mode */ }
    return { topic: session.topic, wallet };
  } finally {
    modal.closeModal();
  }
}

export async function wcRestore(cfg) {
  let topic = null;
  try { topic = localStorage.getItem(TOPIC_KEY); } catch (_) { return null; }
  if (!topic) return null;
  const { sc } = await client(cfg);
  const session = sc.session.getAll().find((s) => s.topic === topic && s.expiry * 1000 > Date.now());
  if (!session) { try { localStorage.removeItem(TOPIC_KEY); } catch (_) { /* */ } return null; }
  const wallet = accountFromSession(session);
  return wallet ? { topic, wallet } : null;
}

export async function wcSign(cfg, topic, txJson, { submit }) {
  const { sc } = await client(cfg);
  const res = await sc.request({
    topic,
    chainId: cfg.chain,
    request: { method: 'xrpl_signTransaction', params: { tx_json: txJson, options: { autofill: submit, submit } } },
  });
  const signed = res?.tx_json || res;
  return { tx_json: signed, hash: signed?.hash || res?.hash || null };
}

export async function wcDisconnect(cfg, topic) {
  try {
    const { sc } = await client(cfg);
    await sc.disconnect({ topic, reason: { code: 6000, message: 'User signed out' } });
  } catch (_) { /* already gone */ }
  try { localStorage.removeItem(TOPIC_KEY); } catch (_) { /* */ }
}
```
Check the vendored bundle's actual export names (`grep -o "export{[^}]*}" webapp/client/vendor/walletconnect.js | head`) and the modal's constructor options before finalizing; adjust `openModal`/`closeModal` names if the bundle differs.

- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_wc_pure.py -q` → pass.
- [ ] **Step 5: Commit** — `git add webapp/client/wc.js webapp/client/wc_pure.js tests/test_wc_pure.py && git commit -m "feat(client): WalletConnect/Joey wrapper module (#447)"`

---

### Task 10: Client — Joey sign-in, restore, sign-out, link-wallet UI

**Files:**
- Modify: `webapp/client/index.html` (register panel: add `<button id="register-joey-btn" class="secondary" hidden>Connect with Joey Wallet</button>`; profile/home: `<button id="link-wallet-btn" class="link" hidden>Link another wallet</button>` + a small `link-wallet-panel` section cloned from the register panel with ids prefixed `link-`), `webapp/client/app.js`.
- Bump `index.html` `app.js?v=78` → `?v=79`.

- [ ] **Step 1: Wire config + button visibility** (in the `main()` `/api/config` block near :5145):
```js
    wcCfg = cfg.walletconnect || null;
    const surface = insideTelegram ? 'telegram' : insideDiscord ? 'discord-activity' : 'web';
    wcAllowed = !!wcCfg && (wcCfg.surfaces || []).includes(surface);
    el('register-joey-btn').hidden = !wcAllowed;
    el('link-wallet-btn').hidden = !sessionToken;
```
Declare `let wcCfg = null, wcAllowed = false, wcTopic = null;` near `WEB_SESSION_KEY`.

- [ ] **Step 2: Sign-in flow** (after `pollWebSignin`):
```js
async function startJoeySignin() {
  clearTimeout(signinPollTimer);
  showPanel('register-panel');
  renderSignin({ sub: 'Connecting to Joey Wallet…', spinner: true });
  try {
    const wc = await import('./wc.js?v=1');
    const { topic, wallet } = await wc.wcConnect(wcCfg);
    renderSignin({ sub: 'Approve the sign-in request in Joey Wallet…', spinner: true });
    const s = await api('/api/web/signin', { method: 'POST', body: JSON.stringify({ provider: 'walletconnect' }) });
    const { proofTx } = await import('./wc_pure.js?v=1');
    const tx = proofTx({ wallet, nonce: s.nonce, sourceTag: s.source_tag, memos: s.memos });
    const { tx_json } = await wc.wcSign(wcCfg, topic, tx, { submit: false });
    const done = await api('/api/web/signin/proof', { method: 'POST', body: JSON.stringify({ sign_id: s.sign_id, tx_json }) });
    sessionToken = done.session_token;
    wcTopic = topic;
    try { localStorage.setItem(WEB_SESSION_KEY, done.session_token); } catch (_) { /* private mode */ }
    me = { ...done.user, wallet: done.wallet, provider: 'walletconnect' };
    showMintHome();
  } catch (e) {
    showError(e.message);
    renderSignin({ sub: e.message.includes('reject') ? 'Cancelled in Joey Wallet.' : 'Could not sign in with Joey Wallet.', retry: true });
  }
}
```
`renderSignin` gains `el('register-joey-btn').hidden = !wcAllowed || spinner;`. Button: `el('register-joey-btn').onclick = startJoeySignin;` next to :5088.

- [ ] **Step 3: Restore + sign-out** — in `setupWeb()`, after `sessionToken = stored;` and before `/api/me`: nothing changes (token is enough). After a successful `/api/me` whose `provider === 'walletconnect'` (add `provider` to `/api/me`'s response from `request["user"]` — one-line server change in `handle_me`), call `wcTopic = (await (await import('./wc.js?v=1')).wcRestore(wcCfg))?.topic || null;`. In the existing sign-out handler (grep `localStorage.removeItem(WEB_SESSION_KEY)` at :149) add `if (wcTopic) (await import('./wc.js?v=1')).wcDisconnect(wcCfg, wcTopic);`.

- [ ] **Step 4: Link-wallet UI**
```js
async function startLinkWallet(provider) {
  showPanel('link-wallet-panel');
  const sub = el('link-sub');
  try {
    const s = await api('/api/wallet/link', { method: 'POST', body: JSON.stringify({ provider }) });
    if (provider === 'walletconnect') {
      sub.textContent = 'Connect the wallet you want to link in Joey, then approve the request…';
      const wc = await import('./wc.js?v=1');
      const { topic, wallet } = await wc.wcConnect(wcCfg);   // a NEW pairing: the other wallet
      const { proofTx } = await import('./wc_pure.js?v=1');
      const { tx_json } = await wc.wcSign(wcCfg, topic, proofTx({ wallet, nonce: s.nonce, sourceTag: s.source_tag, memos: s.memos }), { submit: false });
      const r = await api('/api/wallet/link/proof', { method: 'POST', body: JSON.stringify({ sign_id: s.sign_id, tx_json }) });
      sub.textContent = `Linked ${r.wallet.slice(0, 6)}… — ${r.bucket.wallets.length} wallets in your profile.`;
      await wc.wcDisconnect(wcCfg, topic); // do not overwrite the signed-in pairing topic
      return;
    }
    sub.textContent = 'Scan with the Xaman wallet you want to link.';
    applySignDelivery({ qrEl: el('link-qr'), linkBtn: el('link-link-btn'), toggleBtn: el('link-qr-toggle'), link: s.signin_link, qrData: s.signin_link });
    const tick = async () => {
      if (el('link-wallet-panel').hidden) return;
      const st = await api(`/api/wallet/link/${s.uuid}`).catch(() => ({ state: 'pending' }));
      if (st.state === 'linked') { sub.textContent = `Linked ${st.wallet.slice(0, 6)}…`; return; }
      if (st.state === 'expired') { sub.textContent = 'The request expired.'; return; }
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  } catch (e) {
    sub.textContent = e.code === 'same_wallet' ? "That's already your signed-in wallet." : e.message;
  }
}
el('link-wallet-btn').onclick = () => startLinkWallet(wcAllowed ? 'walletconnect' : 'xaman');
```
(Note: `wcConnect` stores the new topic in `lfg_wc_topic`; re-store the original with `localStorage.setItem('lfg_wc_topic', wcTopic)` after the link completes so restore keeps the signed-in pairing. Add `el('link-joey-btn')`/`el('link-xaman-btn')` when both are allowed, each calling `startLinkWallet` with its provider.)

- [ ] **Step 5: Verify** — `.venv/bin/pytest -q tests/test_wc_pure.py webapp -q`; `node --check webapp/client/app.js`; bump `index.html` to `app.js?v=79`. Manual: with `WEBAPP_DEV_MODE=1` the button renders when `REOWN_PROJECT_ID` is set.
- [ ] **Step 6: Commit** — `git commit -am "feat(client): Joey Wallet sign-in, restore, sign-out and link-wallet UI (#447)"`

---

### Task 11: Client — transaction signing on the WC path

**Files:**
- Modify: `webapp/client/app.js` — every site that renders a sign handle (`grep -n "qrData: s.xumm_url\|qrData: started.xumm_url\|extract_xumm_url\|list_xumm_url\|s.accept.xumm_url" webapp/client/app.js`, ~12 sites) + `applySignDelivery`.

**Interfaces:** `async function wcDrive(handle)` — given a session dict with `sign_mode === 'walletconnect'`, `uuid`, `txjson`, `expires_at`: signs+submits via Joey once (deduped per `uuid`), posts `/api/sign/{uuid}/result`, retries the post every 3 s on 202 until `expires_at`. Returns nothing; the existing pollers observe the flow's state change.

- [ ] **Step 1: Implement `wcDrive`** (near `applySignDelivery`):
```js
const wcDriven = new Set();
async function wcDrive(h) {
  if (!h || h.sign_mode !== 'walletconnect' || !h.uuid || wcDriven.has(h.uuid)) return;
  wcDriven.add(h.uuid);
  const wc = await import('./wc.js?v=1');
  const post = (body) => api(`/api/sign/${h.uuid}/result`, { method: 'POST', body: JSON.stringify(body) });
  let hash = null;
  try {
    ({ hash } = await wc.wcSign(wcCfg, wcTopic, h.txjson, { submit: true }));
  } catch (e) {
    await post(/reject|denied|cancel/i.test(e.message) ? { rejected: true } : { error: e.message }).catch(() => {});
    return;
  }
  if (!hash) { await post({ error: 'wallet returned no hash' }).catch(() => {}); return; }
  const until = (h.expires_at || (Date.now() / 1000 + 900)) * 1000;
  const tick = async () => {
    try {
      const r = await api(`/api/sign/${h.uuid}/result`, { method: 'POST', body: JSON.stringify({ hash }) });
      if (r.state !== 'pending') return;
    } catch (e) {
      if (e.status !== 202) return; // mismatch / expired: the poller will show it
    }
    if (Date.now() < until) setTimeout(tick, 3000);
  };
  tick();
}
```
`api()` must expose `e.status` and, for 202, resolve rather than throw — check how `api()` treats non-2xx (grep `async function api(`); 202 is 2xx so it resolves with `{state:'pending'}` — the `r.state !== 'pending'` branch handles it; the catch branch is for 409/410.

- [ ] **Step 2: Hook the render sites.** In `applySignDelivery`, add a first line: `if (arguments[0].handle && arguments[0].handle.sign_mode === 'walletconnect') { … }` — cleaner: add an optional `handle` param and at the top:
```js
  if (handle && handle.sign_mode === 'walletconnect') {
    if (qrEl) qrEl.hidden = true;
    if (linkBtn) linkBtn.hidden = true;
    if (toggleBtn) toggleBtn.hidden = true;
    wcDrive(handle);
    return { wc: true, linkPrimary: false, qrCollapsed: true, autoOpen: false };
  }
```
Then at each render site pass `handle: s` (mint :1227/:1269 pass the session `s`/`started`; the market status table at :4563–:4854 returns objects — add `handle: s` to each returned object and forward it in the shared renderer that consumes those objects; the trait-sell wizard passes the sub-handle `{sign_mode: s.extract_sign_mode, uuid: s.extract_uuid, txjson: s.extract_txjson}` — so the server's `TraitSellSession.to_dict()` must expose `extract_sign_mode/extract_uuid/extract_txjson` and `list_*` alike: add these to `market_flow.TraitSellSession.to_dict()` from the stored handle raws).
  Server side, every session `to_dict()` that exposes `xumm_url`/`push` must also expose `sign_mode`, `txjson`, `expires_at` from the handle raw (mint: `payment_sign_mode` … simpler: store `self.payment_handle = payload` and emit `"sign_mode": h.get("sign_mode"), "txjson": h.get("txjson"), "expires_at": h.get("expires_at")`). Add a test in `tests/test_wc_provider.py`:
```python
def test_mint_session_to_dict_exposes_wc_fields():
    s = mint_flow.MintSession(discord_id="u", wallet_address=W, platform="web", provider="walletconnect")
    s.payment_uuid, s.payment_sign_mode, s.payment_txjson, s.payment_expires_at = "wc-1", "walletconnect", {"a": 1}, 1.0
    d = s.to_dict()
    assert d["sign_mode"] == "walletconnect" and d["txjson"] == {"a": 1} and d["uuid"] == "wc-1"
```
  and mirror for `ListSession`/`BuySession`/`CancelSession`/`BidSession`/`BidAcceptSession`/`TraitSellSession`/`SwapSession`/`Burn2MintSession`/`BulkMintJob` (`grep -n '"xumm_url"' lfg_core/*_flow.py` lists every to_dict site).
  Sub text: where `signText(s.push, 'Scan to sign … in Xaman.')` is used, prefix with `s.sign_mode === 'walletconnect' ? 'Approve in Joey Wallet…' : …`.

- [ ] **Step 3: Verify** — `node --check webapp/client/app.js`; `.venv/bin/pytest -q`; bump `app.js?v=79` (already) and note in the PR that `wc.js?v=1` is new.
- [ ] **Step 4: Commit** — `git commit -am "feat(client): drive WalletConnect transaction signing through the session poll (#447)"`

---

### Task 12: Sweep, docs, smoke

**Files:**
- Modify: `lfg_service/app.py` (`_settlement_sweep_loop` or the #424 abandon sweep: call `sign_store.expire_stale()` each pass), `scripts/cancel_xumm_payloads.py` (skip `wc-` lines with a note), `CLAUDE.md` (a "WalletConnect / Joey (#447)" subsection under XUMM Flow: provider dispatch, proof shape, `/api/sign/{id}/result`, `wallet_proof_links`, known gaps #58/RegularKey), `README.md` repo-layout tree if `wc.js` must appear (run `scripts/check_repo_layout.py`), `docs/ACTIVITY_SETUP.md` (URL Mappings for `relay.walletconnect.com` + `api.web3modal.org`).

- [ ] **Step 1:** add `await asyncio.to_thread(sign_store.expire_stale)` to the periodic sweep, with a test asserting a stale pending row flips to `expired` after one sweep pass (monkeypatch the loop body function directly).
- [ ] **Step 2:** docs edits above.
- [ ] **Step 3:** full gate: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service && .venv/bin/pytest -q`.
- [ ] **Step 4:** Commit `docs + sweep`, push `feat/447-walletconnect`, open PR (non-draft) titled `feat: WalletConnect / Joey Wallet sign-in, wallet linking and tx signing (#447)`; wait for Greptile + CodeRabbit, close every finding on-thread.
- [ ] **Step 5: Smoke (staging, testnet `xrpl:1`, real Joey app)** — user-run; record results on #447:
  1. `REOWN_PROJECT_ID` set in `~/LFG-staging/.env`; `pm2 restart stg-activity --update-env`.
  2. build.letseffinggo.com (pointed at staging API) → "Connect with Joey Wallet" → sign-in → `/api/me` shows `provider: walletconnect`.
  3. Link a second wallet (Joey) → `sqlite3 lfg_nfts_testnet.db "select * from wallet_proof_links"` shows one row; profile bucket lists both.
  4. Mint (XRP path) → Joey prompt → `sign_requests` row `signed` with txid; mint completes.
  5. Market list + cancel on a character.
  6. Reject a request in Joey → session shows "Cancelled in wallet".
  7. Reload the page → still signed in, next sign request works without re-pairing.
  8. Mobile Safari/Chrome → Joey deep link opens from the modal (record outcome; if it fails, file a follow-up — desktop QR is the supported path).
  Then promote and repeat 2/4 on prod (`xrpl:0`).

---

## Self-review

- **Spec coverage:** §1 dispatch → Tasks 3–4; §2 proof + sign-in + link (WC and Xaman) → Tasks 2, 6, 8; §3 tx create/verify/status/cancel → Tasks 3, 7, 11; §4 client/config/surfaces/error codes → Tasks 5, 9, 10, 11; §5 tests → per task; Ops → Task 12. `#260` cancel script skip → Task 12. Known gaps documented in CLAUDE.md → Task 12.
- **Placeholders:** none; the two "check the bundle export names" notes in Task 9 are verification steps, not deferred work.
- **Type consistency:** `sign_store.create/get/set_state/expire_stale` signatures match across Tasks 1/3/6/7/8/12; `status_dict` keys match `xumm_ops.get_payload_status`'s; `_verify_proof_request` returns `(row, wallet)` or a Response in both Tasks 6 and 8; `ProofError.reason` strings match the parametrized tests; `wcSign` returns `{tx_json, hash}` as consumed in Tasks 10/11; `sign_mode/txjson/expires_at/uuid` keys shared between provider raw (Task 3), session `to_dict` (Task 11) and `wcDrive` (Task 11).
