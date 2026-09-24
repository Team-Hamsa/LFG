# Agent users (C of 3): the `agent` sign-in provider, client-signed dispatch, `platform=agent` memos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A bot holding its own key can use LFG exactly like a person does:
- it signs in with `provider: "agent"`;
- every transaction it needs to sign comes back as a client-signed sign request;
- every transaction its session causes is labelled `platform=agent` on-chain.

It gets no privileges beyond that.

**Architecture:**
- The agent arm reuses the WalletConnect proof flow. Its rows carry `provider="agent"`, and its proof memos say `platform=agent`.
- Dispatch keys on a `CLIENT_SIGNED_PROVIDERS` set.
- The provider is ambient per request (`signing.context`, set by `require_auth` and copied into every task a handler spawns). Memo labelling reads it through two helpers, `memos.platform_for()` and `memos.backend_platform()`. So no session object or signature has to carry a provider, except the two job types that survive a restart. Those persist it and restore the context when resumed.

**Tech Stack:** Python 3.10, aiohttp, sqlite3, xrpl-py, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-agent-users-design.md` §1 **and its Amendment 1** (the amendment wins where they differ). PR C of three. **It depends on PR B**, which adds `RedeemedProof` and the key rule. Branch C from `main` only after B merges. A branch stacked on B gets no bot review.

## Global Constraints

- `AGENT_SIGNIN_ENABLED` (env, default `0`) gates the agent arm only. It ships dark, and with the flag off the arm returns 503 `{"code": "agent_disabled"}`. It shares `_web_rate_limited` / `_web_proof_rate_limited`.
- Agent rows: `sign_requests.provider = "agent"`; the proof memos carry `platform=agent`.
- Memos must match the row's provider **exactly**: an agent row refuses a `webapp` proof, and vice versa. A NULL provider (pre-deploy rows, and every wallet-link row) means `walletconnect`/`webapp`.
- An agent proof issues an ordinary `platform="web"` session whose token has `provider: "agent"`, on the same `("web", wallet)` identity row.
- `CLIENT_SIGNED_PROVIDERS = frozenset({"walletconnect", "agent"})`. The cross-wallet rule is unchanged, and `SignIn` is never client-signed.
- The two Closet Market handlers refuse **every** client-signed provider, with provider-neutral copy.
- `memos.PLATFORM_AGENT = "agent"` joins the closed `_PLATFORMS` enum. Labels for an agent session:
  - every surface-platform memo site → `agent`;
  - backend-signed ops it starts → `agent`;
  - settlement → `backend`, always.
- Humans are unchanged: web equips, harvests, accepts and claims keep `backend`, and user-signed payloads keep their surface platform.
- The sponsored-burn memo is byte-identical to before.
- Only bulk-mint jobs and burn2mint sessions persist; they save `sign_provider` + `sign_wallet` and resume inside that signing context.
- Discovery contract: every client-signed flow's link field is `lfg-wc://<id>`, with `id` starting `wc-`.
- No new stores, no `conftest.py` pins. The `provider` column self-migrates.
- Gate: ruff, ruff-format, mypy, gitleaks, the whole pytest suite, check-repo-layout.
- **No Claude/AI attribution** anywhere. The PR opens ready. Both bots must pass, and every finding is closed on its thread.

---

### Task 1: `platform=agent` and the two ambient helpers

**Files:**
- Modify: `lfg_core/memos.py`: the platform constants and `_PLATFORMS` (~27–43); add the helpers after `platform_for_surface` (~149–154)
- Test: `tests/test_memos.py` (append)

**Interfaces:**
- Produces: `memos.PLATFORM_AGENT = "agent"`, `memos.platform_for(surface: str | None) -> str`, `memos.backend_platform() -> str`.

- [ ] **Step 1: Write the failing tests** (append; add `from lfg_core.signing import context` to the imports)

```python
def test_agent_is_a_memo_platform():
    assert memos.PLATFORM_AGENT == "agent"
    decoded = memos.decode_memos(
        memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_AGENT, memos.ACTION_SIGNIN)
    )
    assert decoded["platform"] == "agent"


def test_platform_for_labels_an_agent_session_on_every_surface():
    for surface in ("web", "discord", "telegram", None):
        assert memos.platform_for(surface) == memos.platform_for_surface(surface)
        with context.use("walletconnect", "rW"):
            assert memos.platform_for(surface) == memos.platform_for_surface(surface)
        with context.use("agent", "rW"):
            assert memos.platform_for(surface) == memos.PLATFORM_AGENT


def test_backend_platform_is_agent_only_for_an_agent_session():
    assert memos.backend_platform() == memos.PLATFORM_BACKEND
    with context.use("walletconnect", "rW"):
        assert memos.backend_platform() == memos.PLATFORM_BACKEND
    with context.use("agent", "rW"):
        assert memos.backend_platform() == memos.PLATFORM_AGENT
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_memos.py -q`. Expected: the 3 new tests FAIL with `AttributeError: ... 'PLATFORM_AGENT'`.

- [ ] **Step 3: Implement.** In `lfg_core/memos.py`, after `PLATFORM_WEBAPP = "webapp"`:

```python
# A bot using LFG as an ordinary user through the `agent` sign-in provider
# (agent users spec §1): honest provenance, no privileges.
PLATFORM_AGENT = "agent"
```

Add `PLATFORM_AGENT,` to the `_PLATFORMS` frozenset. After `platform_for_surface`:

```python
def platform_for(surface: str | None) -> str:
    """`platform_for_surface`, except that a session signed in with the `agent`
    provider is labelled PLATFORM_AGENT on every surface (agent users spec §1).

    The provider is ambient: `signing.context`, set by require_auth for the
    request and copied into every task the handler spawns."""
    from lfg_core.signing import context  # lazy: the signing package imports memos

    if context.current_provider() == "agent":
        return PLATFORM_AGENT
    return platform_for_surface(surface)


def backend_platform() -> str:
    """The platform for a backend-signed op a session starts (an equip's modify,
    a claim payout): PLATFORM_AGENT for an agent session, else PLATFORM_BACKEND,
    so every human op keeps today's label."""
    from lfg_core.signing import context

    return PLATFORM_AGENT if context.current_provider() == "agent" else PLATFORM_BACKEND
```

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_memos.py tests/test_memos_transactions.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** — `git add lfg_core/memos.py tests/test_memos.py && git commit -m "feat(memos): platform=agent and the ambient platform_for / backend_platform helpers"`

---

### Task 2: `sign_requests.provider`

**Files:**
- Modify: `lfg_core/signing/store.py`: `ensure_table` (~28–75) and `create` (~82–110)
- Test: create `tests/test_sign_request_provider.py`

**Interfaces:**
- Produces: `store.create(..., provider: str | None = None)`. The row dict has `"provider"` (None on legacy/NULL rows).

- [ ] **Step 1: Write the failing tests**

```python
"""sign_requests.provider (agent users spec §1)."""

import sqlite3
import time

from lfg_core.signing import store


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "sign.db"))
    store.ensure_table()


def test_create_records_the_provider(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    row = store.create(wallet="", purpose="signin", txjson=None, nonce="n", ttl_seconds=60,
                       provider="agent")
    assert store.get(row["id"])["provider"] == "agent"
    legacy = store.create(wallet="", purpose="signin", txjson=None, nonce="m", ttl_seconds=60)
    assert store.get(legacy["id"])["provider"] is None


def test_the_column_self_migrates_on_an_existing_table(monkeypatch, tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sign_requests (id TEXT PRIMARY KEY, wallet TEXT NOT NULL, purpose TEXT"
        " NOT NULL, txjson TEXT, nonce TEXT, state TEXT NOT NULL DEFAULT 'pending', txid TEXT,"
        " result_json TEXT, ip TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO sign_requests (id, wallet, purpose, created_at, expires_at)"
        " VALUES ('wc-old', '', 'signin', 0, 9e9)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DATABASE", path)
    store.ensure_table()
    store.ensure_table()  # idempotent
    assert store.get("wc-old")["provider"] is None


def test_an_agent_request_past_its_ttl_is_expired_by_the_sweep(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    row = store.create(wallet="rW", purpose="tx", txjson={"Account": "rW"}, nonce=None,
                       ttl_seconds=-1, provider="agent")
    store.sweep_expired(time.time())
    assert store.get(row["id"])["state"] == "expired"
```

Before writing the third test, read `store.py` and `tests/test_sign_result_endpoint.py::test_sweep_expires_and_prunes` for the real sweep entry point. If it isn't `store.sweep_expired(now)`, use the function that test calls, with its real signature.

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_sign_request_provider.py -q`. Expected: FAIL (`create()` got an unexpected keyword argument `provider`).

- [ ] **Step 3: Implement.** In `ensure_table`, after the `created_ledger` try/except block, add the same pattern:

```python
        # Self-migrating (agent users spec §1): which client-signed provider a
        # sign-in row belongs to. NULL = walletconnect (rows pending across the
        # deploy, and every wallet-link row).
        try:
            conn.execute("ALTER TABLE sign_requests ADD COLUMN provider TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
```

In `create`, add `provider: str | None = None` after `created_ledger`, add `provider` to the INSERT column list and a `?` to VALUES, and append `provider` to the parameter tuple.

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_sign_request_provider.py tests/test_sign_result_endpoint.py tests/test_wc_signin_endpoint.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** — `git add lfg_core/signing/store.py tests/test_sign_request_provider.py && git commit -m "feat(signing): sign_requests.provider, self-migrating"`

---

### Task 3: The `agent` sign-in arm

**Files:**
- Modify:
  - `lfg_core/config.py`: next to `REOWN_PROJECT_ID` / `wc_enabled` (~719–731)
  - `lfg_core/signing/proof.py`: `build_proof_tx`, `verify_proof`
  - `lfg_service/app.py`: `handle_web_signin_start` (~9775), `RedeemedProof`, `_redeem_proof`, `handle_web_signin_proof`
- Test: create `tests/test_agent_signin_endpoint.py`; append to `tests/test_signing_proof.py`

**Interfaces:**
- Consumes (from PR B, #603): `RedeemedProof(wallet, signer, checked_at)`, `_redeem_proof -> tuple[RedeemedProof, None] | tuple[None, web.Response]`, `_finish_web_signin(wallet, provider, signer=None, checked_at=None)`, and the autouse `ledger_keys` stub pattern.
- Consumes (from Tasks 1–2): `memos.PLATFORM_AGENT`, `store.create(provider=)`.
- Produces:
  - `config.AGENT_SIGNIN_ENABLED: bool`
  - `proof.build_proof_tx(wallet, nonce, action, platform=memos.PLATFORM_WEBAPP)`
  - `proof.verify_proof(..., platform=memos.PLATFORM_WEBAPP)`
  - `proof.PROVIDER_PLATFORM: dict[str, str]`
  - `RedeemedProof.provider: str`

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_signing_proof.py`:

```python
def test_agent_proof_memos_say_agent_and_are_matched_exactly():
    w = Wallet.create()
    tx = _autofill(proof.build_proof_tx(w.classic_address, NONCE, memos.ACTION_SIGNIN,
                                        platform=memos.PLATFORM_AGENT))
    assert memos.decode_memos(tx["Memos"])["platform"] == "agent"
    tx["SigningPubKey"] = w.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), w.private_key)
    ok = proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN,
                            platform=memos.PLATFORM_AGENT)
    assert ok == w.classic_address
    with pytest.raises(proof.ProofError) as ei:  # an agent proof on a webapp row
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "memos"
```

Create `tests/test_agent_signin_endpoint.py`. Copy the module-level harness from `tests/test_wc_signin_endpoint.py` verbatim: the imports, `_run`, `_Req`, the autouse `_hermetic`, `_stub_proof_creation_ledger` and `ledger_keys` fixtures, and `_sign`. Change `_sign`'s signature to `_sign(wallet, nonce, platform=memos.PLATFORM_AGENT, action=memos.ACTION_SIGNIN)` and pass `platform=platform` to `proof.build_proof_tx`. Then add:

```python
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
    b = json.loads(_run(app.handle_web_signin_start(
        _Req(body={"provider": "walletconnect"}))).text)
    status, body = _redeem(b, _sign(Wallet.create(), b["nonce"]))
    assert status == 400 and body["code"] == "bad_proof"


def test_a_null_provider_row_redeems_as_walletconnect(monkeypatch):
    row = store.create(wallet="", purpose="signin", txjson=None, nonce="n" * 64,
                       ttl_seconds=300, created_ledger=1000)  # a pre-deploy row: no provider
    w = Wallet.create()
    status, body = _redeem({"sign_id": row["id"]},
                           _sign(w, "n" * 64, platform=memos.PLATFORM_WEBAPP))
    assert status == 200
    assert app.verify_session_token(body["session_token"])["provider"] == "walletconnect"


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
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_agent_signin_endpoint.py tests/test_signing_proof.py -q`. Expected: FAIL. The `platform=` kwarg is unknown, and `provider="agent"` falls through to the Xaman arm.

- [ ] **Step 3: Implement.**

(a) `lfg_core/config.py`, after `def wc_enabled()`:

```python
# Agent users (spec §1): the `agent` web sign-in provider — a bot holding its own
# key signs a proof like Joey does, and its transactions are labelled
# platform=agent. Ships dark; turned on per stack. Read at import.
AGENT_SIGNIN_ENABLED = env_flag("AGENT_SIGNIN_ENABLED", "0")
```

(b) `lfg_core/signing/proof.py`:
- Add `platform: str = memos.PLATFORM_WEBAPP` as the last parameter of `build_proof_tx` (after `action`) and as the last keyword-only parameter of `verify_proof`.
- Replace `memos.PLATFORM_WEBAPP` with `platform` in both memo blocks (`build_proof_tx` ~132 and the `expected` block in `verify_proof` ~230).
- Add at module level:

```python
#: The proof memo platform for each client-signed sign-in provider. A row with
#: no provider (pre-deploy, and every wallet-link row) is walletconnect.
PROVIDER_PLATFORM = {"walletconnect": memos.PLATFORM_WEBAPP, "agent": memos.PLATFORM_AGENT}
```

(c) `lfg_service/app.py`, `handle_web_signin_start`:
- Before the existing walletconnect gate, add:

```python
    if provider == "agent" and not config.AGENT_SIGNIN_ENABLED:
        return web.json_response(
            {"error": "agent sign-in is not enabled", "code": "agent_disabled"}, status=503
        )
```

- Change `if provider == "walletconnect":` (the arm that creates the row) to `if provider in ("walletconnect", "agent"):`.
- In that arm, pass `provider=provider` to `sign_request_store.create`, pass `platform=signing_proof.PROVIDER_PLATFORM[provider]` as `build_proof_tx`'s fourth argument, and return `"provider": provider` instead of the literal.
- Add to the docstring: `With provider="agent" (agent users spec §1) the same flow runs for a bot, gated on AGENT_SIGNIN_ENABLED, with platform=agent proof memos.`

(d) `RedeemedProof` gains a fourth field, `provider: str  # the row's client-signed provider ("walletconnect" for NULL rows)`. In `_redeem_proof`, before the `verify_proof` call:

```python
    provider = row.get("provider") or "walletconnect"
```

Pass `platform=signing_proof.PROVIDER_PLATFORM[provider]` to `verify_proof`, and return `RedeemedProof(wallet, signing_proof.signing_key_address(tx_json), checked_at, provider)`.

(e) `handle_web_signin_proof`: `return await _finish_web_signin(proven.wallet, proven.provider, signer=proven.signer, checked_at=proven.checked_at)`.

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_agent_signin_endpoint.py tests/test_wc_signin_endpoint.py tests/test_wallet_link_endpoint.py tests/test_signing_proof.py tests/test_regular_key_sessions.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** — `git add lfg_core/config.py lfg_core/signing/proof.py lfg_service/app.py tests/test_agent_signin_endpoint.py tests/test_signing_proof.py && git commit -m "feat(signin): the agent provider — a WalletConnect-shaped proof arm with platform=agent memos, gated on AGENT_SIGNIN_ENABLED"`

---

### Task 4: Client-signed dispatch covers `agent`

**Files:**
- Modify: `lfg_core/xumm_ops.py` (`should_use_walletconnect` ~229, its caller ~266); `lfg_service/app.py` (`_wallet_unsupported_response` ~5801 and the two checks ~6362, ~6521)
- Test: `tests/test_wc_provider.py` (append); `tests/test_closet_market_api_signing.py` (update ~422–445)

**Interfaces:**
- Produces: `xumm_ops.CLIENT_SIGNED_PROVIDERS: frozenset[str]` and `xumm_ops.should_use_client_signing(txjson) -> bool`. It replaces `should_use_walletconnect`; update its one caller and any test that names it.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_wc_provider.py`:

```python
def test_an_agent_session_is_client_signed_too():
    with context.use("agent", W):
        h = _run(
            xumm_ops._create_xumm_payload(
                {
                    "TransactionType": "TrustSet",
                    "Account": W,
                    "LimitAmount": {"currency": "USD", "issuer": OTHER, "value": "1"},
                },
                memos_json=_memos(),
            )
        )
    assert h["uuid"].startswith("wc-") and h["xumm_url"] == f"lfg-wc://{h['uuid']}"
    assert store.get(h["uuid"])["wallet"] == W


def test_an_agent_sessions_foreign_account_payload_falls_back_to_xaman(monkeypatch):
    calls = []

    async def fake_post(payload):
        calls.append(payload)
        return {"qr_url": "q", "xumm_url": "x", "uuid": "2" * 36, "pushed": False}

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    with context.use("agent", W):
        h = _run(
            xumm_ops._create_xumm_payload(
                {"TransactionType": "TrustSet", "Account": OTHER}, memos_json=_memos()
            )
        )
    assert calls and h["uuid"] == "2" * 36
```

In `tests/test_closet_market_api_signing.py`, replace the two refusal tests (~422–445) with provider-parametrized versions:

```python
REFUSAL = {"code": "wallet_unsupported", "error": "Closet Market orders need a Xaman sign-in for now."}


@pytest.mark.parametrize("provider", ["walletconnect", "agent"])
def test_closet_bid_refused_for_client_signed_sessions(closet_env, provider):
    body = {"slot": "Head", "value": "Tiara", "price_brix": "10"}
    with signing_context.use(provider, ME):
        resp = _run(server.handle_closet_bid_create(_req("POST", "/", body)))
    assert resp.status == 409 and _json(resp) == REFUSAL


@pytest.mark.parametrize("provider", ["walletconnect", "agent"])
def test_closet_buy_refused_for_client_signed_sessions(closet_env, provider):
    with signing_context.use(provider, BIDDER):
        resp = _run(
            server.handle_closet_ask_buy(_req("POST", "/", match_info={"order_id": "bogus"}))
        )
    assert resp.status == 409 and _json(resp) == REFUSAL
```

Keep the comment block above them, and update its "isn't available on WalletConnect (Joey) yet" to "isn't available to client-signed sessions (Joey, agents) yet".

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_wc_provider.py tests/test_closet_market_api_signing.py -q`. Expected: the agent cases FAIL (dispatch goes to Xaman; the Closet Market isn't refused).

- [ ] **Step 3: Implement.** In `lfg_core/xumm_ops.py`, replace `should_use_walletconnect` with:

```python
#: Providers whose sessions sign their own transactions (#447, agent users §1):
#: a payload for the session's own account becomes a sign request, not XUMM.
CLIENT_SIGNED_PROVIDERS = frozenset({"walletconnect", "agent"})


def should_use_client_signing(txjson: dict[str, Any]) -> bool:
    """Ambient dispatch (#447, agent users §1): client-signed only when the
    session's provider signs for itself AND the tx is signable by the session's
    account (the cross-wallet rule) AND it is a real transaction (SignIn is
    Xaman's pseudo-tx)."""
    from lfg_core.signing import context

    if context.current_provider() not in CLIENT_SIGNED_PROVIDERS:
        return False
    if txjson.get("TransactionType") == "SignIn":
        return False
    wallet = context.current_wallet()
    if wallet and txjson.get("Account") == wallet:
        return True
    logging.info(
        f"sign request for {txjson.get('Account')} downgraded to xaman (session wallet {wallet})"
    )
    return False
```

and change its caller to `if should_use_client_signing(txjson):`. Keep `get_provider("walletconnect")`: agent rows use the same transport. In `lfg_service/app.py`:
- change both `if signing_context.current_provider() == "walletconnect":` to `if signing_context.current_provider() in xumm_ops.CLIENT_SIGNED_PROVIDERS:`;
- change `_wallet_unsupported_response`'s message to `"Closet Market orders need a Xaman sign-in for now."`;
- in its docstring, "isn't available on WalletConnect (Joey) yet" becomes "isn't available to client-signed sessions (Joey, agents) yet".

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_wc_provider.py tests/test_closet_market_api_signing.py tests/test_signing_context.py -q`, and `grep -rn "should_use_walletconnect" lfg_core lfg_service tests webapp` (expect no hits). Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat(signing): client-signed dispatch covers the agent provider; Closet Market refuses every client-signed session"`

---

### Task 5: Every surface-platform memo site labels an agent session

**Files:**
- Modify: every `memos.platform_for_surface(` call outside `lfg_core/memos.py`. There are 30 as of `0415a63a`:
  - `lfg_service/app.py` 13
  - `lfg_core/swap_flow.py` 6
  - `lfg_core/mint_flow.py` 5
  - `lfg_core/bulk_mint_flow.py` 2
  - `lfg_core/shop_flow.py` 2
  - `lfg_core/market_flow.py` 1
  - `lfg_core/burn2mint_flow.py` 1
- Test: create `tests/test_memo_platform_sites.py`

**Interfaces:**
- Consumes: `memos.platform_for` (Task 1).

- [ ] **Step 1: Write the failing guard test**

```python
"""Agent users §1: a memo platform derived from a surface must go through
memos.platform_for, so an agent session is labelled platform=agent everywhere."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_code_outside_memos_calls_platform_for_surface():
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for base in ("lfg_core", "lfg_service", "webapp", "scripts")
        for p in (ROOT / base).rglob("*.py")
        if p.name != "memos.py"
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(r"\bplatform_for_surface\(", line)
    ]
    assert offenders == []
```

- [ ] **Step 2:** Run it. Expected: FAIL, listing the 30 sites.

- [ ] **Step 3: Implement.** At every listed site, replace `memos.platform_for_surface(` with `memos.platform_for(`. Nothing else changes: same argument, same keyword. Ten of these sites are backend-signed (`mint_nft`, `create_nft_offer` / `offer_delivery.ensure_offer`, `modify_nft`, `burn_nft`, `prepare_sponsored_mint`, the shop's `mint_fn` / `offer_fn`). They already stamp the surface platform, so they switch the same way (Amendment 1). The sponsored `NFTokenMint`'s recovery is by hash, not memo.

- [ ] **Step 4:** Run the guard, then `scripts/run-tests -n auto --dist loadfile -q`. Expected: PASS. With no agent context, `platform_for` returns exactly what `platform_for_surface` did.

- [ ] **Step 5: Commit** — `git commit -am "feat(memos): every surface-platform memo site labels an agent session platform=agent"`

---

### Task 6: Backend-signed ops and BRIX claims an agent session starts

**Files:**
- Modify: `scripts/_economy_deps.py` (`_offer_or_skip`, `_accept_or_skip`, `_closet_modify`, `build_economy_deps` ~280–340); `webapp/economy_api.py` (`build_settlement_deps` ~205); `lfg_core/xrpl_ops.py` (`send_brix_claim` ~3076, memo ~3128); `lfg_service/app.py` (`_claim_one_wallet`'s `send_brix_claim` call ~2169)
- Test: `tests/test_economy_deps_trait.py` (append); `tests/test_brix_claim_flow.py` (append); every test stub of `send_brix_claim` (`grep -rn '"send_brix_claim"' tests webapp`)

**Interfaces:**
- Consumes: `memos.backend_platform()`, `memos.PLATFORM_AGENT` (Task 1).
- Produces:
  - `build_economy_deps(conn, user_token=None, owner=None, platform: str | None = None)`, where None means `memos.backend_platform()` at construction;
  - `send_brix_claim(..., platform: str = memos.PLATFORM_BACKEND)`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_economy_deps_trait.py` (it already has `_run` and the `sys.path` setup that makes `import _economy_deps` work):

```python
def _record_platforms(monkeypatch):
    """Fake every op build_economy_deps wires; return the list of `platform=` values they get."""
    import types

    from lfg_core import xrpl_ops, xumm_ops

    seen: list = []

    def recorder(result):
        async def fake(*args, **kw):
            seen.append(kw.get("platform"))
            return result

        return fake

    monkeypatch.setattr(xrpl_ops, "mint_nft", recorder("NFTID"))
    monkeypatch.setattr(xrpl_ops, "burn_nft", recorder("BURNHASH"))
    monkeypatch.setattr(xrpl_ops, "create_nft_offer", recorder("OFFER"))
    # _closet_modify reads tx_hash / ledger_index / transaction_index off the result
    monkeypatch.setattr(
        xrpl_ops,
        "modify_nft",
        recorder(types.SimpleNamespace(tx_hash="H", ledger_index=5, transaction_index=0)),
    )
    monkeypatch.setattr(xumm_ops, "create_accept_offer_payload", recorder({"uuid": "u"}))
    return seen


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("agent", "agent"), ("xaman", "backend"), ("walletconnect", "backend")],
)
def test_every_op_an_economy_session_builds_carries_its_platform(monkeypatch, provider, expected):
    import sqlite3

    import _economy_deps as deps

    from lfg_core import economy_store
    from lfg_core.signing import context

    seen = _record_platforms(monkeypatch)
    conn = sqlite3.connect(":memory:")
    economy_store.init_economy_schema(conn)
    with context.use(provider, "rOWNER"):
        d = deps.build_economy_deps(conn, owner="rOWNER")
    # Called OUTSIDE the context: the platform is fixed when the deps are built.
    for call in (
        d.closet_mint_fn("u"), d.char_mint_fn("u"), d.trait_mint_fn("u"),
        d.char_modify_fn("N", "rOWNER", "u"), d.closet_modify_fn("N", "rOWNER", "u"),
        d.char_burn_fn("N", "rOWNER"), d.trait_burn_fn("N", "rOWNER"),
        d.closet_offer_fn("N", "rOWNER"), d.char_offer_fn("N", "rOWNER"),
        d.closet_accept_fn("OFFER"), d.char_accept_fn("OFFER"),
    ):
        _run(call)
    assert seen == [expected] * 11


def test_settlement_deps_stay_backend_inside_an_agent_request(monkeypatch):
    import sqlite3

    from lfg_core import economy_store
    from lfg_core.signing import context
    from webapp import economy_api

    seen = _record_platforms(monkeypatch)
    conn = sqlite3.connect(":memory:")
    economy_store.init_economy_schema(conn)
    with context.use("agent", "rOWNER"):
        d = economy_api.build_settlement_deps(conn)
    _run(d.char_modify_fn("N", "rOWNER", "u"))
    assert seen == ["backend"]
```

Add `import pytest` to that file's imports if it's missing. Append to `tests/test_brix_claim_flow.py` (add `from lfg_core import memos` to its imports if missing):

```python
def test_send_brix_claim_labels_the_payment_with_its_platform(capture_payment):
    asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42, platform=memos.PLATFORM_AGENT))
    assert memos.decode_memos(capture_payment.tx.to_xrpl()["Memos"])["platform"] == "agent"
    asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=43))
    assert memos.decode_memos(capture_payment.tx.to_xrpl()["Memos"])["platform"] == "backend"
```

Append to `tests/test_brix_endpoints.py`:

```python
@pytest.mark.parametrize(("provider", "expected"), [("agent", "agent"), ("xaman", "backend")])
def test_a_claim_payout_carries_the_session_platform(drip, monkeypatch, provider, expected):
    from lfg_core.signing import context

    seen = []

    async def paid(destination, value, claim_id, max_last_ledger_seq=None, platform=None):
        seen.append(platform)
        return xrpl_ops.ClaimPayment("confirmed", "TXHASH", 999)

    monkeypatch.setattr(xrpl_ops, "send_brix_claim", paid)
    _accrue(drip)
    with context.use(provider, WALLET):
        data = _body(_run(server.handle_brix_claim(_Req())))
    assert data["state"] == "confirmed" and seen == [expected]
```

- [ ] **Step 2:** Run the three test files. Expected: FAIL (every platform is `backend`; the unknown kwarg raises TypeError in the fakes).

- [ ] **Step 3: Implement.**
- `_offer_or_skip(nft_id, owner, platform=memos.PLATFORM_BACKEND)` passes `platform=platform` to `create_nft_offer`.
- `_accept_or_skip(offer_id, user_token=None, owner=None, platform=memos.PLATFORM_BACKEND)` passes `platform=platform` to `create_accept_offer_payload`.
- `_closet_modify(nft_id, owner, url, platform=memos.PLATFORM_BACKEND)` passes `platform=platform` to `modify_nft`.
- In `build_economy_deps`, add the `platform` parameter. First line of the body:

```python
    # Agent users §1: every op these deps sign or build is labelled with the
    # session's platform, fixed now, in the request that starts the session.
    platform = platform or memos.backend_platform()
```

  Pass `platform=platform` to every `mint_nft` / `modify_nft` / `burn_nft` lambda. Wire:

```python
        closet_offer_fn=lambda nft_id, owner: _offer_or_skip(nft_id, owner, platform),
        closet_accept_fn=lambda offer_id: _accept_or_skip(offer_id, user_token, owner, platform),
        closet_modify_fn=lambda nft_id, owner, url: _closet_modify(nft_id, owner, url, platform),
        char_offer_fn=lambda nft_id, owner: _offer_or_skip(nft_id, owner, platform),
        char_accept_fn=lambda offer_id: _accept_or_skip(offer_id, user_token, owner, platform),
```

  Add one line to the docstring: `` `platform` (default: the session's, via memos.backend_platform) labels every op. ``
- `build_settlement_deps`: `return _economy_deps.build_economy_deps(conn, platform=memos.PLATFORM_BACKEND)`, with the comment `# settlement is service-triggered: always backend, even inside an agent's buy-status request`. Import `memos` if the module lacks it.
- `send_brix_claim`: add `platform: str = memos.PLATFORM_BACKEND` after `max_last_ledger_seq`, and use it in place of `memos.PLATFORM_BACKEND` in its memo build.
- `_claim_one_wallet`: pass `platform=memos.backend_platform()` to `send_brix_claim`. The claim-all task runs `_claim_one_wallet` inside the context it inherited from the request that started it.
- Update every `send_brix_claim` test fake to accept `platform=None`.

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_economy_deps_trait.py tests/test_brix_claim_flow.py tests/test_brix_endpoints.py tests/test_brix_claim_all.py webapp/test_economy_api.py tests/test_memos_transactions.py -q`. Expected: PASS, including the sponsored-burn memo snapshot.

- [ ] **Step 5: Commit** — `git commit -am "feat(memos): backend-signed economy ops and BRIX claims an agent session starts say platform=agent; settlement stays backend"`

---

### Task 7: Resumed jobs keep their signing context

**Files:**
- Modify:
  - `lfg_core/bulk_mint_flow.py`: `BulkMintJob.__init__` (~174), `serialize`, `from_serialized`
  - `lfg_core/burn2mint_flow.py`: `Burn2MintSession.__init__` (~86), `serialize`, `from_serialized`, and where it builds the `BulkMintJob` (~485)
  - `lfg_service/app.py`: `resume_bulk_jobs` (~8672, the `job.task = asyncio.create_task(...)` at ~8736) and `_launch_burn_mint_job` (~7873–7884)
- Test: create `tests/test_resume_signing_context.py`

**Interfaces:**
- Produces:
  - `BulkMintJob.sign_provider: str`, `BulkMintJob.sign_wallet: str | None`, and the same two on `Burn2MintSession`. Both are keyword arguments defaulting to None, meaning "read `signing.context` now". Records without them load as `"xaman"` / `None`.
  - `app._launch_bulk_task(job) -> None`, which starts `run_bulk_mint_job(job)` inside `signing_context.use(job.sign_provider, job.sign_wallet)`.

- [ ] **Step 1: Write the failing tests**

```python
"""A resumed bulk mint or burn2mint keeps the signing mode it started with
(agent users §1, Amendment 1): a client-signed session's payloads stay client-signed."""

import asyncio

from lfg_core import bulk_mint_flow, burn2mint_flow
from lfg_core.signing import context
import lfg_service.app as app

W = "rWALLETWALLETWALLETWALLETWALLET1"


def test_bulk_job_records_and_restores_its_signing_context():
    with context.use("agent", W):
        job = bulk_mint_flow.BulkMintJob("u", W, 2, platform="web")
    d = job.serialize()
    assert (d["sign_provider"], d["sign_wallet"]) == ("agent", W)
    again = bulk_mint_flow.BulkMintJob.from_serialized(d)
    assert (again.sign_provider, again.sign_wallet) == ("agent", W)
    d.pop("sign_provider"); d.pop("sign_wallet")  # a record from before this change
    legacy = bulk_mint_flow.BulkMintJob.from_serialized(d)
    assert (legacy.sign_provider, legacy.sign_wallet) == ("xaman", None)


def test_burn2mint_session_records_and_restores_its_signing_context():
    with context.use("agent", W):
        s = burn2mint_flow.Burn2MintSession("u", W, ["N1"], platform="web")
    again = burn2mint_flow.Burn2MintSession.from_serialized(s.serialize())
    assert (again.sign_provider, again.sign_wallet) == ("agent", W)


def test_a_resumed_bulk_job_runs_inside_its_signing_context(monkeypatch):
    seen = {}

    async def fake_run(job):
        seen["ctx"] = (context.current_provider(), context.current_wallet())

    monkeypatch.setattr(bulk_mint_flow, "run_bulk_mint_job", fake_run)
    with context.use("agent", W):
        job = bulk_mint_flow.BulkMintJob("u", W, 1, platform="web")

    async def main():  # outside any request context, like startup
        app._launch_bulk_task(job)
        await job.task

    asyncio.new_event_loop().run_until_complete(main())
    assert seen["ctx"] == ("agent", W)
```

If `BulkMintJob.__init__` or `serialize` needs more state than the tests above supply (e.g. `from_serialized` reads keys `serialize` only sets later), build the job the way `tests/test_bulk_mint_flow.py` does, and keep the assertions.

- [ ] **Step 2:** Run it. Expected: FAIL (`AttributeError: sign_provider`).

- [ ] **Step 3: Implement.** In both constructors, add keyword args `sign_provider: str | None = None, sign_wallet: str | None = None`, and set:

```python
        # Agent users §1: the signing mode the session started in, so a resumed
        # job keeps dispatching the way it did (a client-signed session's
        # payloads stay client-signed). Captured from the request's context.
        from lfg_core.signing import context as signing_context

        self.sign_provider = sign_provider or signing_context.current_provider()
        self.sign_wallet = sign_wallet if sign_provider else signing_context.current_wallet()
```

`serialize` adds `"sign_provider": self.sign_provider, "sign_wallet": self.sign_wallet`. `from_serialized` passes `sign_provider=d.get("sign_provider", "xaman"), sign_wallet=d.get("sign_wallet")`. Where `burn2mint_flow` constructs its `BulkMintJob` (~485), pass `sign_provider=session.sign_provider, sign_wallet=session.sign_wallet`. In `lfg_service/app.py` add:

```python
def _launch_bulk_task(job: Any) -> None:
    """Start (or resume) a bulk job's run inside the signing context it was
    created in (agent users §1): a task copies the context at creation."""
    with signing_context.use(job.sign_provider, job.sign_wallet):
        job.task = asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))
```

and replace `job.task = asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))` in `resume_bulk_jobs` and `_launch_burn_mint_job` with `_launch_bulk_task(job)`. The start handler's own launch runs inside the request context already and stays as it is.

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_resume_signing_context.py tests/test_bulk_mint_flow.py tests/test_bulk_mint_service.py tests/test_session_resume.py -q` and `grep -rln burn2mint tests | xargs .venv/bin/python -m pytest -q`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "fix(resume): bulk-mint jobs and burn2mint sessions resume in the signing context they started in"`

---

### Task 8: The discovery contract, for agents

**Files:**
- Create: `tests/test_client_signed_discovery.py`. It was written and verified against `walletconnect` on 2026-09-23 on the local branch `scratch/agent-users-discovery`: `git show scratch/agent-users-discovery:tests/test_client_signed_discovery.py > tests/test_client_signed_discovery.py`.

- [ ] **Step 1:** Bring the file in and run it: all its `walletconnect` cases PASS.
- [ ] **Step 2:** Change its `PROVIDERS = ["walletconnect"]` to `PROVIDERS = ["walletconnect", "agent"]`. Where each test reads the stored sign request, add an assertion that its txjson memos decode to `platform == memos.platform_for_surface("web")` for walletconnect and `"agent"` for agent (this is the spec's "an agent session's user-signed payload carries `platform=agent`", end to end).
- [ ] **Step 3:** Run it. Expected: PASS for both providers (Tasks 4–5 make the agent cases pass).
- [ ] **Step 4: Commit** — `git add tests/test_client_signed_discovery.py && git commit -m "test: the lfg-wc:// discovery contract, per client-signed flow, for Joey and agents"`

---

### Task 9: Docs

- [ ] **CLAUDE.md**:
  - add `agent` to the memo platform enum (~625–626): `` | `agent` (a bot using the `agent` sign-in provider — agent users spec §1) ``;
  - replace the stale sweep sentence (~1735–1737, "(now started when `ECONOMY_ENABLED or config.wc_enabled()`)") with "(always started; `sweep_sign_requests` runs every pass)";
  - add the agent arm to the WalletConnect sign-in paragraph: `provider: "agent"` (gated on `AGENT_SIGNIN_ENABLED`), agent rows, and `platform=agent` labelling for every transaction an agent session causes;
  - in the env block, after `WC_SURFACES=…` (~167): `` AGENT_SIGNIN_ENABLED=0                                          # optional (agent users §1); the "agent" web sign-in provider for bots holding their own key — 0 = off ``
- [ ] **`docs/ops/env.staging.example`**: add `AGENT_SIGNIN_ENABLED=1` beside the other feature flags (~6–9), with a comment: the fly's testnet rehearsal.
- [ ] Commit — `git commit -am "docs: the agent sign-in provider and platform=agent (agent users §1)"`

---

### Task 10: Gate, PR, review

- [ ] After PR B merges, rebase onto `main`. Run `scripts/run-tests -n auto --dist loadfile -q`, `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service`: all green.
- [ ] `git push -u origin feat/agent-users-agent-provider`, then open the PR ready (not draft), titled `feat(signin): the agent provider + platform=agent memos (agent users C/3)`. The body summarizes Tasks 1–9 and links the spec and plan.
- [ ] Wait for Greptile (its check-run summary) and CodeRabbit. Close every finding on its thread with the fixing commit or the reason it's declined.
- [ ] Rollout (operator): staging `.env` `AGENT_SIGNIN_ENABLED=1` + `pm2 restart stg-activity --update-env`; prod only when the fly goes live.
