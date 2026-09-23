# Agent users (B of 3): RegularKey-aware proofs and revocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A wallet-ownership proof signed by the account's current RegularKey signs in and links like a master-key proof. A session minted from one ends as soon as the owner removes or rotates that key on-ledger. A proof signed by a disabled master key is refused.

**Architecture:**
- A pure `KeyAuthority` value holds the account's RegularKey, master-disabled flag and lookup outcome. It is parsed from one validated-ledger `account_info` read (`xrpl_ops.key_authority`).
- `verify_proof(authority=...)` applies the spec's table. With `authority=None` it keeps today's master-only rule, so every existing caller is unchanged.
- `_redeem_proof` does the lookup before verifying, so the rule runs before `on_verified` writes anything.
- RegularKey tokens carry `key`/`signer` claims. `require_auth` and `/api/events/me` re-check them at most once a minute per wallet.

**Tech Stack:** Python 3.10, aiohttp, xrpl-py (`AccountInfo`, `derive_classic_address`), sqlite3, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-agent-users-design.md` §2 **and its Amendment 1** (the amendment wins where they differ). This is PR B of three. A is `/api/rarity/supply` (Team-Hamsa/LFG#602); C is the `agent` provider and memos.

## Global Constraints

- Key rule, for every proof (sign-in and wallet linking):

  | Signing key | Lookup succeeded | Lookup failed |
  |---|---|---|
  | derives to `Account` (master) | accept unless `lsfDisableMaster` → `master_disabled` | **accept** |
  | derives to the AccountRoot's `RegularKey` | accept | **refuse** `regular_key_unverified` (503, retryable) |
  | anything else | refuse `pubkey_account` | refuse `regular_key_unverified` (503; the key can't be checked) |

- `actNotFound` is a successful lookup: no RegularKey, master enabled.
- `regular_key_unverified` → HTTP 503 `{"code": "regular_key_unverified"}`. Every other refusal stays HTTP 400 `{"code": "bad_proof"}`, with the reason logged only.
- The lookup and key check run **before** `on_verified`, so a refused proof writes no `wallet_proof_links` row.
- Token claims for a RegularKey session: `key: "regular"`, `signer: <signing key's classic address>`. Master tokens are unchanged.
- Revocation is re-checked in `require_auth` (so every authenticated endpoint) and in `handle_events_me`, at most once per 60 s per wallet:
  - removed or rotated → denylist the token, 401 `key_revoked`;
  - lookup error → the last answer stands, whatever its age;
  - no answer at all → 503 `key_unverified`.
- Sign-in seeds the answer.
- Dev mode (`WEBAPP_DEV_MODE`) bypasses auth as today.
- No new stores, and no `conftest.py` pins.
- Gate: ruff, ruff-format, mypy, gitleaks, the whole pytest suite.
- **No Claude/AI attribution** anywhere. The PR opens ready. Both bots must pass, and every finding is closed on its thread.

---

### Task 1: `KeyAuthority` and the key rule in `verify_proof`

**Files:**
- Create: `lfg_core/signing/key_authority.py`
- Modify: `lfg_core/signing/proof.py`: imports (~17–27); the key check in `verify_proof` (~254–259, `if derived != account: raise ProofError("pubkey_account")`) and its signature (~160–167)
- Test: create `tests/test_key_authority.py`; modify `tests/test_signing_proof.py`

**Interfaces:**
- Produces:
  - `key_authority.KeyAuthority(regular_key: str | None, master_disabled: bool, lookup_ok: bool)` (frozen dataclass)
  - `key_authority.LOOKUP_FAILED`, `key_authority.NOT_FOUND`, `key_authority.LSF_DISABLE_MASTER = 0x00100000`
  - `key_authority.from_account_info(result: dict[str, Any]) -> KeyAuthority`
  - `proof.verify_proof(..., authority: KeyAuthority | None = None) -> str`
  - `proof.signing_key_address(tx_json: dict[str, Any]) -> str`

- [ ] **Step 1: Write the failing tests.** Create `tests/test_key_authority.py`:

```python
"""KeyAuthority parsing (agent users spec §2)."""

from lfg_core.signing.key_authority import (
    LOOKUP_FAILED,
    LSF_DISABLE_MASTER,
    KeyAuthority,
    from_account_info,
)


def test_decoded_account_flags_win_over_the_raw_bit():
    result = {
        "account_data": {"Flags": 0, "RegularKey": "rReg"},
        "account_flags": {"disableMasterKey": True},
    }
    assert from_account_info(result) == KeyAuthority("rReg", True, True)


def test_raw_flags_bit_when_account_flags_is_absent():
    assert from_account_info({"account_data": {"Flags": LSF_DISABLE_MASTER}}) == KeyAuthority(
        None, True, True
    )
    assert from_account_info({"account_data": {"Flags": 0}}) == KeyAuthority(None, False, True)


def test_no_flags_or_no_account_data_is_inconclusive():
    assert from_account_info({"account_data": {"RegularKey": "rReg"}}) == LOOKUP_FAILED
    assert from_account_info({}) == LOOKUP_FAILED
```

Append to `tests/test_signing_proof.py` (and add `from lfg_core.signing.key_authority import LOOKUP_FAILED, KeyAuthority` to its imports):

```python
ACCOUNT, REGULAR, FOREIGN = Wallet.create(), Wallet.create(), Wallet.create()
FOUND = KeyAuthority(regular_key=REGULAR.classic_address, master_disabled=False, lookup_ok=True)


def _signed_by(signer, account, nonce=NONCE):
    """A proof for `account` signed with `signer`'s key (a RegularKey when they differ)."""
    tx = _autofill(proof.build_proof_tx(account, nonce, memos.ACTION_SIGNIN))
    tx["SigningPubKey"] = signer.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), signer.private_key)
    return tx


def _verify(tx, authority):
    return proof.verify_proof(
        tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN, authority=authority
    )


def _refusal(tx, authority):
    with pytest.raises(proof.ProofError) as ei:
        _verify(tx, authority)
    return ei.value.reason


def test_master_key_is_accepted_when_the_ledger_allows_it():
    tx = _signed_by(ACCOUNT, ACCOUNT.classic_address)
    assert _verify(tx, FOUND) == ACCOUNT.classic_address


def test_disabled_master_key_is_refused():
    tx = _signed_by(ACCOUNT, ACCOUNT.classic_address)
    disabled = KeyAuthority(REGULAR.classic_address, master_disabled=True, lookup_ok=True)
    assert _refusal(tx, disabled) == "master_disabled"


def test_master_key_is_accepted_when_the_lookup_fails():
    tx = _signed_by(ACCOUNT, ACCOUNT.classic_address)
    assert _verify(tx, LOOKUP_FAILED) == ACCOUNT.classic_address


def test_regular_key_is_accepted():
    tx = _signed_by(REGULAR, ACCOUNT.classic_address)
    assert _verify(tx, FOUND) == ACCOUNT.classic_address


def test_regular_key_is_refused_when_the_lookup_fails():
    tx = _signed_by(REGULAR, ACCOUNT.classic_address)
    assert _refusal(tx, LOOKUP_FAILED) == "regular_key_unverified"


def test_foreign_key_is_refused():
    tx = _signed_by(FOREIGN, ACCOUNT.classic_address)
    assert _refusal(tx, FOUND) == "pubkey_account"


def test_signing_key_address_names_the_key_that_signed():
    assert proof.signing_key_address(_signed_by(REGULAR, ACCOUNT.classic_address)) == (
        REGULAR.classic_address
    )
```

In the same file, change the docstring of `test_pubkey_must_derive_the_account` to `"""Without a KeyAuthority (the master-key-only rule), a RegularKey-signed proof is refused."""`. The test body stays as it is.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_key_authority.py tests/test_signing_proof.py -q`
Expected: `tests/test_key_authority.py` errors at collection with `ModuleNotFoundError: No module named 'lfg_core.signing.key_authority'`, and the new `test_signing_proof.py` tests fail the same way.

- [ ] **Step 3: Implement.** Create `lfg_core/signing/key_authority.py`:

```python
"""Which keys may sign for an account, per its AccountRoot (agent users spec §2).

Pure: `xrpl_ops.key_authority` does the validated-ledger read and parses it with
`from_account_info`; `signing.proof.verify_proof` applies the rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: AccountRoot flag lsfDisableMaster: the master key may no longer sign.
LSF_DISABLE_MASTER = 0x00100000


@dataclass(frozen=True)
class KeyAuthority:
    """One `account_info` read of an account's signing keys.

    `lookup_ok` False means the read failed or was inconclusive, and the other
    two fields say nothing. An account the ledger doesn't know yet reads as found,
    with no RegularKey and its master key enabled (`NOT_FOUND`).
    """

    regular_key: str | None
    master_disabled: bool
    lookup_ok: bool


LOOKUP_FAILED = KeyAuthority(regular_key=None, master_disabled=False, lookup_ok=False)
NOT_FOUND = KeyAuthority(regular_key=None, master_disabled=False, lookup_ok=True)


def from_account_info(result: dict[str, Any]) -> KeyAuthority:
    """Parse a successful `account_info` result.

    The decoded `account_flags` object wins; older rippled builds omit it, so
    fall back to the raw `Flags` bit, as `xrpl_ops.disallows_incoming_nft_offers`
    does for its flag. No flags at all is inconclusive.
    """
    data = result.get("account_data")
    if not isinstance(data, dict):
        return LOOKUP_FAILED
    flags = result.get("account_flags")
    raw = data.get("Flags")
    if isinstance(flags, dict) and "disableMasterKey" in flags:
        disabled = bool(flags["disableMasterKey"])
    elif isinstance(raw, int):
        disabled = bool(raw & LSF_DISABLE_MASTER)
    else:
        return LOOKUP_FAILED
    regular = data.get("RegularKey")
    return KeyAuthority(
        regular_key=regular if isinstance(regular, str) and regular else None,
        master_disabled=disabled,
        lookup_ok=True,
    )
```

In `lfg_core/signing/proof.py`, add `from lfg_core.signing.key_authority import KeyAuthority` beside `from lfg_core.signing import provenance`. Add these two functions just above `verify_proof`:

```python
def signing_key_address(tx_json: dict[str, Any]) -> str:
    """The classic address of the key that signed a proof `verify_proof` accepted:
    the account itself for its master key, otherwise its RegularKey."""
    return derive_classic_address(str(tx_json["SigningPubKey"]).upper())


def _check_signing_key(signer: str, account: str, authority: KeyAuthority | None) -> None:
    """Agent users spec §2. `authority` None is the master-key-only rule."""
    if signer == account:
        if authority is not None and authority.lookup_ok and authority.master_disabled:
            raise ProofError("master_disabled")
        return
    if authority is None:
        raise ProofError("pubkey_account")
    if not authority.lookup_ok:
        raise ProofError("regular_key_unverified")
    if signer != authority.regular_key:
        raise ProofError("pubkey_account")
```

Change `verify_proof`'s signature to:

```python
def verify_proof(
    tx_json: Any,
    *,
    wallet_hint: str | None,
    nonce: str,
    action: str,
    max_last_ledger: int | None = None,
    authority: KeyAuthority | None = None,
) -> str:
    """Return the classic address proven by `tx_json`, or raise `ProofError`.

    `authority` is the account's keys as the validated ledger reports them
    (`xrpl_ops.key_authority`). Without it only the master key proves ownership.
    """
```

and replace

```python
    if derived != account:
        raise ProofError("pubkey_account")
```

with

```python
    _check_signing_key(derived, account, authority)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_key_authority.py tests/test_signing_proof.py -q`
Expected: all PASS, including the unchanged `test_pubkey_must_derive_the_account`.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/signing/key_authority.py lfg_core/signing/proof.py tests/test_key_authority.py tests/test_signing_proof.py
git commit -m "feat(signing): KeyAuthority and the RegularKey rule in verify_proof

authority=None keeps the master-key-only rule for every existing caller."
```

---

### Task 2: `xrpl_ops.key_authority`, the validated-ledger read

**Files:**
- Modify: `lfg_core/xrpl_ops.py`: imports (~62); add the function after `disallows_incoming_nft_offers` (~990)
- Test: `tests/test_key_authority.py` (append)

**Interfaces:**
- Consumes: `KeyAuthority`, `LOOKUP_FAILED`, `NOT_FOUND`, `from_account_info` (Task 1).
- Produces: `async xrpl_ops.key_authority(address: str) -> KeyAuthority`. It never raises.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_key_authority.py`, and add `import asyncio` and `from lfg_core import xrpl_ops` to its imports, plus `NOT_FOUND` to the key_authority import)

```python
class _Resp:
    def __init__(self, ok, result):
        self._ok = ok
        self.result = result

    def is_successful(self):
        return self._ok


def _stub_client(monkeypatch, response=None, raises=None):
    class _Client:
        async def request(self, _req):
            if raises is not None:
                raise raises
            return response

    monkeypatch.setattr(xrpl_ops, "async_rpc_client", _Client)


def _lookup(address="rAccount"):
    return asyncio.get_event_loop().run_until_complete(xrpl_ops.key_authority(address))


def test_found_account_is_parsed(monkeypatch):
    _stub_client(
        monkeypatch,
        _Resp(True, {"account_data": {"Flags": 0, "RegularKey": "rReg"}}),
    )
    assert _lookup() == KeyAuthority("rReg", False, True)


def test_unknown_account_is_a_definite_answer(monkeypatch):
    _stub_client(monkeypatch, _Resp(False, {"error": "actNotFound"}))
    assert _lookup() == NOT_FOUND


def test_other_errors_and_exceptions_are_lookup_failures(monkeypatch):
    _stub_client(monkeypatch, _Resp(False, {"error": "tooBusy"}))
    assert _lookup() == LOOKUP_FAILED
    _stub_client(monkeypatch, raises=OSError("down"))
    assert _lookup() == LOOKUP_FAILED
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_key_authority.py -q`
Expected: the three new tests FAIL with `AttributeError: module 'lfg_core.xrpl_ops' has no attribute 'key_authority'`.

- [ ] **Step 3: Implement.** In `lfg_core/xrpl_ops.py`, below `from lfg_core import config, memos, owner_lock, payment_ledger, xrpl_rpc`, add:

```python
from lfg_core.signing.key_authority import (
    LOOKUP_FAILED,
    NOT_FOUND,
    KeyAuthority,
    from_account_info,
)
```

After `disallows_incoming_nft_offers`, add:

```python
async def key_authority(address: str) -> KeyAuthority:
    """The validated ledger's word on which keys may sign for `address` (agent
    users spec §2).

    Never raises: a failed or inconclusive read is `LOOKUP_FAILED`, and each
    caller decides what that allows (a proof signed by the master key still
    passes; one signed by a RegularKey doesn't). `actNotFound` is a definite
    answer, not a failure: no RegularKey, master enabled.
    """
    try:
        client = async_rpc_client()
        response = await client.request(AccountInfo(account=address, ledger_index="validated"))
    except Exception as e:
        logging.warning(f"key_authority({address}) lookup failed: {e}")
        return LOOKUP_FAILED
    if response.is_successful():
        return from_account_info(response.result)
    if response.result.get("error") == "actNotFound":
        return NOT_FOUND
    logging.warning(f"key_authority({address}) inconclusive: {response.result.get('error')}")
    return LOOKUP_FAILED
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_key_authority.py tests/test_sponsored_preflight.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/xrpl_ops.py tests/test_key_authority.py
git commit -m "feat(xrpl_ops): key_authority — the validated ledger's RegularKey and lsfDisableMaster"
```

---

### Task 3: Proof redemption applies the rule; RegularKey tokens name their key

**Files:**
- Modify: `lfg_service/app.py`:
  - `make_session_token` (~727–740)
  - `_finish_web_signin` (~9832–9855)
  - `_redeem_proof` (~9898–9980)
  - `handle_web_signin_proof` (~9983–10004)
  - `handle_wallet_link_proof` (~10885–10925)
  - imports (~96–98)
- Test: `tests/test_wc_signin_endpoint.py`, `tests/test_wallet_link_endpoint.py`

**Interfaces:**
- Consumes: `xrpl_ops.key_authority` (Task 2); `verify_proof(authority=)`, `signing_key_address`, `LOOKUP_FAILED` (Task 1).
- Produces:
  - `app.RedeemedProof(wallet: str, signer: str)` (frozen dataclass). `_redeem_proof` returns `tuple[RedeemedProof, None] | tuple[None, web.Response]`.
  - `app._finish_web_signin(wallet: str, provider: str, signer: str | None = None)`.
  - Session-token claims `key: "regular"` and `signer` for RegularKey sessions.
  - PR C extends `RedeemedProof` with `provider`.

- [ ] **Step 1: Write the failing tests.** In **both** `tests/test_wc_signin_endpoint.py` and `tests/test_wallet_link_endpoint.py`:
  - add `from lfg_core.signing.key_authority import LOOKUP_FAILED, NOT_FOUND, KeyAuthority` to the imports;
  - add this autouse fixture after `_stub_proof_creation_ledger`. Without it, the proof endpoints would now read the ledger over the network:

```python
@pytest.fixture(autouse=True)
def ledger_keys(monkeypatch):
    """No network from the proof endpoints: the account's keys as the ledger
    reports them. Default: found, no RegularKey, master enabled."""
    state = {"authority": NOT_FOUND}

    async def _lookup(address):
        return state["authority"]

    monkeypatch.setattr(app.xrpl_ops, "key_authority", _lookup)
    return state


def _sign_as(account, signer, nonce, action):
    """A proof for `account` signed with `signer`'s key (a RegularKey when they differ)."""
    tx = proof.build_proof_tx(account, nonce, action)
    tx.update(Fee="12", Sequence=42, LastLedgerSequence=1500)
    tx["SigningPubKey"] = signer.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), signer.private_key)
    return tx
```

Append to `tests/test_wc_signin_endpoint.py`:

```python
def _redeem(b, tx):
    r = _run(app.handle_web_signin_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    return r.status, json.loads(r.text)


def test_regular_key_proof_signs_in_and_the_token_names_the_key(monkeypatch, ledger_keys):
    b = _start(monkeypatch)
    account, regular = Wallet.create(), Wallet.create()
    ledger_keys["authority"] = KeyAuthority(regular.classic_address, False, True)
    status, body = _redeem(b, _sign_as(account.classic_address, regular, b["nonce"],
                                       memos.ACTION_SIGNIN))
    assert status == 200 and body["wallet"] == account.classic_address
    decoded = app.verify_session_token(body["session_token"])
    assert decoded["id"] == account.classic_address
    assert decoded["key"] == "regular" and decoded["signer"] == regular.classic_address


def test_master_key_token_carries_no_key_claims(monkeypatch):
    b = _start(monkeypatch)
    w = Wallet.create()
    status, body = _redeem(b, _sign(w, b["nonce"]))
    decoded = app.verify_session_token(body["session_token"])
    assert status == 200 and "key" not in decoded and "signer" not in decoded


def test_foreign_key_proof_is_refused(monkeypatch):
    b = _start(monkeypatch)
    account, foreign = Wallet.create(), Wallet.create()
    status, body = _redeem(b, _sign_as(account.classic_address, foreign, b["nonce"],
                                       memos.ACTION_SIGNIN))
    assert status == 400 and body["code"] == "bad_proof"
    assert store.get(b["sign_id"])["state"] == "pending"


def test_disabled_master_key_is_refused(monkeypatch, ledger_keys):
    b = _start(monkeypatch)
    ledger_keys["authority"] = KeyAuthority(None, True, True)
    status, body = _redeem(b, _sign(Wallet.create(), b["nonce"]))
    assert status == 400 and body["code"] == "bad_proof"


def test_unverifiable_regular_key_is_a_retryable_503(monkeypatch, ledger_keys):
    b = _start(monkeypatch)
    account, regular = Wallet.create(), Wallet.create()
    ledger_keys["authority"] = LOOKUP_FAILED
    status, body = _redeem(b, _sign_as(account.classic_address, regular, b["nonce"],
                                       memos.ACTION_SIGNIN))
    assert status == 503 and body["code"] == "regular_key_unverified"
    assert store.get(b["sign_id"])["state"] == "pending"  # the agent can retry


def test_master_key_is_accepted_when_the_lookup_fails(monkeypatch, ledger_keys):
    b = _start(monkeypatch)
    ledger_keys["authority"] = LOOKUP_FAILED
    status, _ = _redeem(b, _sign(Wallet.create(), b["nonce"]))
    assert status == 200
```

Append to `tests/test_wallet_link_endpoint.py` (and add `import sqlite3` to its imports):

```python
def _proof_links():
    with sqlite3.connect(identity_store.DATABASE) as conn:
        return conn.execute("SELECT COUNT(*) FROM wallet_proof_links").fetchone()[0]


def test_foreign_key_link_proof_writes_no_edge(monkeypatch):
    b = _start_wc(monkeypatch)
    other, foreign = Wallet.create(), Wallet.create()
    tx = _sign_as(other.classic_address, foreign, b["nonce"], memos.ACTION_LINK)
    r = _run(app.handle_wallet_link_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 400 and _body(r)["code"] == "bad_proof"
    assert _proof_links() == 0


def test_regular_key_link_proof_links_the_account(monkeypatch, ledger_keys):
    b = _start_wc(monkeypatch)
    other, regular = Wallet.create(), Wallet.create()
    ledger_keys["authority"] = KeyAuthority(regular.classic_address, False, True)
    tx = _sign_as(other.classic_address, regular, b["nonce"], memos.ACTION_LINK)
    r = _run(app.handle_wallet_link_proof(_Req(body={"sign_id": b["sign_id"], "tx_json": tx})))
    assert r.status == 200 and _body(r)["wallet"] == other.classic_address
    assert _proof_links() == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_wc_signin_endpoint.py tests/test_wallet_link_endpoint.py -q`
Expected: the new RegularKey tests FAIL (400 instead of 200, no `key` claim, 400 instead of 503). The existing tests still PASS.

- [ ] **Step 3: Implement.** In `lfg_service/app.py`:

(a) Add `from lfg_core.signing.key_authority import LOOKUP_FAILED` beside `from lfg_core.signing import proof as signing_proof`.

(b) In `make_session_token`, right before `body = base64.urlsafe_b64encode(...)`:

```python
    # A RegularKey session (agent users spec §2) carries the key that signed, so
    # require_auth can end it when the owner removes or rotates that key.
    payload.update({k: user[k] for k in ("key", "signer") if k in user})
```

(c) Replace `_finish_web_signin`'s signature and token line:

```python
async def _finish_web_signin(
    wallet: str, provider: str, signer: str | None = None
) -> web.Response:
```

```python
    user = {"id": wallet, "name": name, "platform": "web", "provider": provider}
    if signer is not None and signer != wallet:
        user.update(key="regular", signer=signer)
    token = make_session_token(user)
```

Add this line to its docstring: `` `signer` is the proof's signing key; a RegularKey marks the token (spec §2). ``

(d) Above `_redeem_proof`, add:

```python
@dataclass(frozen=True)
class RedeemedProof:
    """What a redeemed ownership proof established."""

    wallet: str  # the account proven
    signer: str  # the key that signed: == wallet for the master key, else its RegularKey
```

(e) In `_redeem_proof`:
- Change the return annotation to `tuple[RedeemedProof, None] | tuple[None, web.Response]`.
- In the docstring, change "Returns `(wallet, None)` on success" to "Returns `(RedeemedProof, None)` on success".
- Directly after the size-cap check (`if not isinstance(tx_json, dict) or len(json.dumps(tx_json)) > WC_PROOF_MAX_BYTES:` … `return`), add:

```python
    # Spec §2: one validated-ledger read of the account's keys, BEFORE verify_proof
    # and so before on_verified, so a proof the key rule refuses never writes an edge.
    account = tx_json.get("Account")
    authority = (
        await xrpl_ops.key_authority(account)
        if isinstance(account, str) and is_valid_classic_address(account)
        else LOOKUP_FAILED
    )
```

Pass `authority=authority,` as the last keyword argument of the `signing_proof.verify_proof` call. In the `except signing_proof.ProofError as e:` block, after the `logging.warning(...)` line and before the 400 return, add:

```python
        if e.reason == "regular_key_unverified":
            return None, web.json_response(
                {
                    "error": "could not check the signing key on-ledger; try again",
                    "code": "regular_key_unverified",
                },
                status=503,
            )
```

Change the final `return wallet, None` to:

```python
    return RedeemedProof(wallet, signing_proof.signing_key_address(tx_json)), None
```

(f) In `handle_web_signin_proof`, replace the tail from `wallet, refusal = await _redeem_proof(` through the end with:

```python
    redeemed, refusal = await _redeem_proof(
        str(body.get("sign_id") or ""),
        body.get("tx_json"),
        purpose="signin",
        action=memos.ACTION_SIGNIN,
    )
    if refusal is not None:
        return refusal
    proven = cast(RedeemedProof, redeemed)
    return await _finish_web_signin(proven.wallet, "walletconnect", signer=proven.signer)
```

(g) In `handle_wallet_link_proof`, rename `wallet, refusal = await _redeem_proof(` to `redeemed, refusal = await _redeem_proof(`, and change the last line to:

```python
    return await _linked_response(session_wallet, cast(RedeemedProof, redeemed).wallet)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_wc_signin_endpoint.py tests/test_wallet_link_endpoint.py tests/test_funder_at_login.py tests/test_signing_context.py tests/test_signing_proof.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_wc_signin_endpoint.py tests/test_wallet_link_endpoint.py
git commit -m "feat(signin): accept RegularKey proofs; refuse a disabled master key

_redeem_proof reads the account's keys from the validated ledger before
verifying, so a refused proof never writes a wallet_proof_links edge. An
unverifiable RegularKey is a retryable 503; a RegularKey session's token
carries key/signer claims."
```

---

### Task 4: Revocation, which ends a RegularKey session once the key is gone

**Files:**
- Modify: `lfg_service/app.py`:
  - add the revocation block just above `def require_auth`;
  - `require_auth` (~849–866);
  - `handle_events_me` (~517–524);
  - `_finish_web_signin` (seed the answer).
- Test: create `tests/test_regular_key_sessions.py`

**Interfaces:**
- Consumes: `xrpl_ops.key_authority` (Task 2); the `key`/`signer` claims and `_finish_web_signin(..., signer=)` (Task 3); the existing `revoke_session_token(token) -> bool`.
- Produces:
  - `app.REGULAR_KEY_RECHECK_SECONDS = 60.0`
  - `app._regular_keys: dict[str, tuple[float, str | None]]`
  - `app._note_regular_key(wallet, regular_key)`
  - `async app._regular_key_still_set(wallet, signer) -> bool | None`
  - `async app._regular_key_refusal(token, payload) -> web.Response | None`

- [ ] **Step 1: Write the failing tests** (`tests/test_regular_key_sessions.py`)

```python
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
    yield
    app._revoked_sessions.clear()
    app._regular_keys.clear()


@pytest.fixture
def ledger(monkeypatch):
    state = {"authority": KeyAuthority(SIGNER, False, True), "calls": 0}

    async def _lookup(address):
        state["calls"] += 1
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_regular_key_sessions.py -q`
Expected: FAIL with `AttributeError: module 'lfg_service.app' has no attribute '_regular_keys'`.

- [ ] **Step 3: Implement.** In `lfg_service/app.py`, just above `def require_auth(handler):`:

```python
# --- RegularKey sessions (agent users spec §2, Amendment 1) -----------------
# A token minted from a RegularKey proof carries key="regular" and the signing
# key's address. Every authenticated request re-checks, at most once a minute
# per wallet, that the account's RegularKey is still that signer. If the owner
# removed or rotated it, the token is denylisted and the request refused. On a
# lookup error the last answer stands, whatever its age; with no answer at all
# (a restart since sign-in) the request fails closed with a retryable 503.
REGULAR_KEY_RECHECK_SECONDS = 60.0
_regular_keys: dict[str, tuple[float, str | None]] = {}  # wallet -> (checked, RegularKey)


def _note_regular_key(wallet: str, regular_key: str | None) -> None:
    _regular_keys[wallet] = (time.monotonic(), regular_key)


async def _regular_key_still_set(wallet: str, signer: str) -> bool | None:
    """Is `signer` still `wallet`'s RegularKey? None: no answer to go on."""
    now = time.monotonic()
    cached = _regular_keys.get(wallet)
    if cached is not None and now - cached[0] < REGULAR_KEY_RECHECK_SECONDS:
        return cached[1] == signer
    authority = await xrpl_ops.key_authority(wallet)
    if authority.lookup_ok:
        _regular_keys[wallet] = (now, authority.regular_key)
        return authority.regular_key == signer
    return None if cached is None else cached[1] == signer


async def _regular_key_refusal(token: str, payload: dict[str, Any]) -> web.Response | None:
    """None if this session may proceed, else the refusal. Master-key tokens (no
    "key" claim) pass untouched."""
    if payload.get("key") != "regular":
        return None
    still = await _regular_key_still_set(str(payload["id"]), str(payload.get("signer", "")))
    if still is None:
        return web.json_response(
            {
                "error": "could not check your signing key on-ledger; try again",
                "code": "key_unverified",
            },
            status=503,
        )
    if not still:
        revoke_session_token(token)
        return web.json_response(
            {"error": "signing key revoked", "code": "key_revoked"}, status=401
        )
    return None
```

In `require_auth`, right after the `if not user: return web.json_response({"error": "unauthorized"}, status=401)` lines, add:

```python
        refusal = await _regular_key_refusal(auth[7:], user)
        if refusal is not None:
            return refusal
```

In `handle_events_me`, right after its `if not payload: return ... 401` lines, add:

```python
    refusal = await _regular_key_refusal(request.query.get("token", ""), payload)
    if refusal is not None:
        return refusal
```

In `_finish_web_signin`, inside the `if signer is not None and signer != wallet:` block added in Task 3, add as its second line:

```python
        _note_regular_key(wallet, signer)  # the proof just verified it on-ledger
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_regular_key_sessions.py tests/test_signing_context.py tests/test_logout_disconnect.py tests/test_event_endpoints.py tests/test_wc_signin_endpoint.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_regular_key_sessions.py
git commit -m "feat(auth): end RegularKey sessions when the key is removed or rotated

require_auth and /api/events/me re-check the account's RegularKey at most
once a minute per wallet; a removed or rotated key denylists the token (401
key_revoked). A lookup error keeps the last answer; with none, 503."
```

---

### Task 5: Docs

**Files:**
- Modify: `CLAUDE.md`: the WalletConnect paragraph, starting at "**RegularKey is\n   NOT accepted**" (~1744–1747)
- Modify: `docs/superpowers/specs/2026-08-27-walletconnect-joey-signin-design.md`: lines ~109–110 and ~188–190

- [ ] **Step 1: CLAUDE.md.** Replace the sentence "**RegularKey is NOT accepted**: `verify_proof` derives the address from `SigningPubKey` and requires it to equal `Account`, so only the master key can prove ownership." (it wraps across lines 1744–1747) with:

```markdown
**RegularKey proofs** (agent users spec §2): `_redeem_proof` reads the
   account's keys from the validated ledger (`xrpl_ops.key_authority`) before
   `verify_proof`. The master key proves ownership unless `lsfDisableMaster` is
   set; the AccountRoot's current RegularKey proves it too. On a failed lookup
   the master key is still accepted and a RegularKey is refused with a
   retryable 503 `regular_key_unverified`; any other key is `bad_proof`. The
   rule runs before `on_verified`, so a refused link proof writes no edge. A
   RegularKey session's token carries `key: "regular"` and `signer`;
   `require_auth` (and `/api/events/me`) re-check at most once a minute per
   wallet that the RegularKey is still that signer, denylist the token with a
   401 `key_revoked` when it isn't, keep the last answer on a lookup error, and
   fail closed with a 503 `key_unverified` when there is none (a restart since
   sign-in).
```

Keep the indentation of the surrounding list item.

- [ ] **Step 2: The #447 spec.** After line ~110 ("RegularKey-signed proofs are NOT accepted in v1 (documented limitation).") and after the "Known gaps" RegularKey bullet (~188–190), add:

```markdown
  > **Superseded 2026-09-23:** RegularKey-signed proofs are accepted, and
  > RegularKey sessions end when the key is removed. See
  > `2026-09-22-agent-users-design.md` §2.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-08-27-walletconnect-joey-signin-design.md
git commit -m "docs: RegularKey proofs and session revocation (agent users §2)"
```

---

### Task 6: Gate, PR, review

- [ ] **Step 1: Whole gate**

Run: `scripts/run-tests -n auto --dist loadfile -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service`
Expected: all green.

- [ ] **Step 2: Push, and open the PR ready**

```bash
git push -u origin feat/agent-users-regular-key
gh pr create --repo Team-Hamsa/LFG --base main --head feat/agent-users-regular-key \
  --title "feat(signin): RegularKey-aware ownership proofs + session revocation (agent users B/3)" \
  --body "<summary of Tasks 1-5, the key-rule table, the revocation rule, and 'Part B of three; A is #602'>"
```

- [ ] **Step 3: Review.** Wait for Greptile (check the `Greptile Review` check-run summary; a clean pass leaves no comments) and CodeRabbit. Close every finding on its own thread with the fixing commit or the reason it's declined. Re-trigger with `@greptile-apps please re-review` / `@coderabbitai review`.
