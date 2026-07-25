# SourceTag-Sponsored Free Mint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a live-toggleable, 60-minute, 100-slot campaign that gives one payment-free NFT to each previously unseen SourceTag wallet and durably burns exactly 1 project LFGO per confirmed sponsored mint.

**Architecture:** A new SQLite-backed `sponsored_mint` domain module owns campaign, eligibility, claim, and burn-obligation state in the per-network app database, while the existing history database remains the SourceTag eligibility source. The service reserves sponsorship before exposing free UX, the existing mint pipeline gains a sponsored branch and durable callbacks, a restart-safe burn worker reconciles indeterminate submissions by memo, and Discord `/admin` controls service-token-protected endpoints.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, sqlite3/WAL, xrpl-py, discord.py, vanilla JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-07-24-sourcetag-sponsored-free-mint-design.md`

## Global Constraints

- Production behavior is mainnet-only and ships OFF.
- Eligibility means the normalized wallet is absent from validated `xrpl_txs.account` rows carrying `config.SOURCE_TAG`, absent from consumed claims, and absent from the project-wallet exclusion set.
- The free promise is shown only after a durable reservation; eligibility/store failure before reservation uses the unchanged paid path.
- Each activation expires after exactly 3,600 seconds and admits no more than 100 simultaneous reserved-or-consumed slots.
- Stop/expiry blocks new reservations but preserves admitted sessions.
- A claim becomes permanently consumed when its NFT is confirmed; downstream failures never permit a second free NFT.
- Each confirmed sponsored NFT creates exactly one durable obligation to burn `config.MINT_PRICE_LFGO` (currently `"1"`) from `config.SIGNING_ACCOUNT`.
- A burn never blocks the user's NFT; indeterminate burn submissions are reconciled by deterministic memo before retry and are never blindly resubmitted.
- User-signed `NFTokenAcceptOffer` carries `config.SOURCE_TAG`; backend signing wallets are excluded from unique-wallet metrics.
- `/admin` requires Discord Administrator permission both at command entry and on every component interaction.
- Admin endpoints require a valid Discord service token and reject every other surface.
- Existing paid single-mint and all bulk-mint behavior remains unchanged.
- No AI/Claude attribution in commit messages.

## File Structure

| Path | Responsibility |
|---|---|
| `lfg_core/sponsored_mint.py` | Create: schema, campaign state, eligibility, atomic reservations, claim transitions, metrics, audit, burn queue. |
| `lfg_core/sponsored_burn.py` | Create: restart-safe burn worker and indeterminate-result reconciliation. |
| `lfg_core/xrpl_ops.py` | Modify: submit and locate uniquely memoed project-funded LFGO burns without hiding result class. |
| `lfg_core/config.py` | Modify: campaign duration/cap and explicit excluded-wallet configuration. |
| `lfg_core/mint_flow.py` | Modify: sponsored session state, payment bypass, irreversible callbacks, resume-safe failure semantics. |
| `lfg_service/app.py` | Modify: reservation at mint start, admin endpoints, worker lifecycle, cancellation/recovery, API contracts. |
| `surfaces/_client/client.py` | Modify: typed service client methods for sponsored-campaign status/start/stop. |
| `surfaces/discord_bot/admin.py` | Modify: per-interaction permission gate and campaign controls/status embed. |
| `scripts/onchain_listener.py` | Modify: mark tagged sponsored acceptances from validated transactions. |
| `scripts/backfill_history.py` | Modify: preserve the same acceptance-marking behavior during replay/backfill. |
| `webapp/client/app.js` | Modify: sponsored pricing and no-payment UX. |
| `webapp/client/mint_pure.js` | Modify: pure sponsored-state presentation helper. |
| `tests/test_sponsored_mint_store.py` | Create: campaign/eligibility/claim/cap/concurrency tests. |
| `tests/test_sponsored_mint_flow.py` | Create: service and mint pipeline behavior/recovery tests. |
| `tests/test_sponsored_burn.py` | Create: obligation, retry, reconciliation, and no-double-burn tests. |
| `tests/test_sponsored_acceptance.py` | Create: listener acceptance and SourceTag metric tests. |
| `tests/test_sponsored_admin.py` | Create: endpoint, SDK, Discord permission, and audit tests. |
| `tests/test_mint_pure_js.py` | Modify: sponsored UI contract tests. |
| `scripts/audit_sponsored_mint_readiness.py` | Create: preflight archive, exclusion, balance, and pending-debt audit. |
| `docs/ops/sponsored-free-mint.md` | Create: staging rehearsal, production activation, monitoring, stop, recovery, and rollback runbook. |

---

### Task 1: Persistent campaign, eligibility, and claim store

**Files:**
- Create: `lfg_core/sponsored_mint.py`
- Modify: `lfg_core/config.py`
- Create: `tests/test_sponsored_mint_store.py`

**Interfaces:**
- Produces `CampaignStatus`, `ReservationResult`, `Claim`, and `BurnObligation` frozen dataclasses.
- Produces `ensure_schema(db_path: str) -> None`.
- Produces `start_campaign(db_path: str, *, network: str, actor: str, now: int | None = None) -> CampaignStatus`.
- Produces `stop_campaign(db_path: str, *, network: str, actor: str, now: int | None = None) -> CampaignStatus`.
- Produces `campaign_status(db_path: str, history_path: str, *, network: str, now: int | None = None) -> CampaignStatus`.
- Produces `reserve_if_eligible(db_path: str, history_path: str, *, network: str, wallet: str, session_id: str, now: int | None = None) -> ReservationResult`.
- Produces `release_reservation(db_path: str, *, network: str, wallet: str, session_id: str, reason: str, now: int | None = None) -> bool`.
- Produces `mark_minting(...)`, `record_minted_and_enqueue_burn(...)`, `record_offer(...)`, and `record_acceptance(...)`, all idempotent.

- [ ] **Step 1: Write campaign and eligibility failures**

Create fixture-backed tests that initialize `history_store.init_history_db`, call `ensure_schema`, and assert:

```python
def test_campaign_defaults_off_and_expires_at_3600_seconds(tmp_path):
    db, history = paths(tmp_path)
    sm.ensure_schema(db)
    assert sm.campaign_status(db, history, network="mainnet", now=100).state == "off"
    started = sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert started.enabled_until == 3700
    assert sm.campaign_status(db, history, network="mainnet", now=3699).state == "active"
    assert sm.campaign_status(db, history, network="mainnet", now=3700).state == "expired"


def test_known_tagged_and_project_wallets_are_not_eligible(tmp_path, monkeypatch):
    db, history = paths(tmp_path)
    insert_tagged(history, "rKnown")
    monkeypatch.setattr(config, "SPONSORED_MINT_EXCLUDED_WALLETS", ("rProject",))
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert not sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rKnown", session_id="s1", now=101
    ).sponsored
    assert not sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rProject", session_id="s2", now=101
    ).sponsored
```

- [ ] **Step 2: Run tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_sponsored_mint_store.py -v`

Expected: FAIL with `ImportError: cannot import name 'sponsored_mint'`.

- [ ] **Step 3: Implement schema and value types**

Add config values:

```python
SPONSORED_MINT_DURATION_SECONDS = int(os.getenv("SPONSORED_MINT_DURATION_SECONDS", "3600"))
SPONSORED_MINT_CAP = int(os.getenv("SPONSORED_MINT_CAP", "100"))
SPONSORED_MINT_EXCLUDED_WALLETS = tuple(
    value.strip()
    for value in os.getenv("SPONSORED_MINT_EXCLUDED_WALLETS", "").split(",")
    if value.strip()
)
```

In `lfg_core/sponsored_mint.py`, define the four tables from the design using explicit column lists, `BEGIN IMMEDIATE`, WAL, `busy_timeout=30000`, and dataclasses whose status values use literals from the spec. Build the exclusion set from `SPONSORED_MINT_EXCLUDED_WALLETS`, `SIGNING_ACCOUNT`, and `TOKEN_ISSUER_ADDRESS`.

- [ ] **Step 4: Implement campaign calculation and atomic reservation**

`reserve_if_eligible` must:

```python
if network != "mainnet":
    return ReservationResult(False, "wrong_network", None)
if not archive_is_usable(history_path):
    return ReservationResult(False, "eligibility_unavailable", None)
if is_tagged_wallet(history_path, wallet) or wallet in excluded_wallets():
    return ReservationResult(False, "ineligible", None)
```

Then open the app DB, `BEGIN IMMEDIATE`, recompute effective campaign state, count claims in `reserved`, `minting`, `minted`, `offered`, or `accepted`, and insert/reacquire only if the count is below the persisted cap. Treat a consumed claim as permanently ineligible. Return reason codes `campaign_off`, `campaign_expired`, `at_capacity`, `already_consumed`, or `reserved`.

- [ ] **Step 5: Add concurrency and lifecycle tests**

Add tests for 110 concurrent thread-pool reservations admitting exactly 100; duplicate wallet requests admitting one; manual stop; expiry; released pre-mint claims reopening capacity; `minting` not released without explicit proof; and a consumed claim remaining unavailable after offer failure.

- [ ] **Step 6: Run store tests**

Run: `.venv/bin/python -m pytest tests/test_sponsored_mint_store.py -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add lfg_core/config.py lfg_core/sponsored_mint.py tests/test_sponsored_mint_store.py
git commit -m "feat: add sponsored mint campaign store"
```

---

### Task 2: Service admin API and client contract

**Files:**
- Modify: `lfg_service/app.py`
- Modify: `surfaces/_client/client.py`
- Create: `tests/test_sponsored_admin.py`

**Interfaces:**
- Consumes Task 1 campaign functions.
- Produces `GET /api/admin/sponsored-mint/status`.
- Produces `POST /api/admin/sponsored-mint/start` and `/stop`.
- Produces client methods `sponsored_mint_status()`, `sponsored_mint_start()`, and `sponsored_mint_stop()`.

- [ ] **Step 1: Write endpoint authorization and lifecycle tests**

Test every handler with missing, Telegram, and Discord tokens:

```python
def test_start_requires_discord_service_token(client, service_headers):
    assert client.post("/api/admin/sponsored-mint/start").status == 401
    assert client.post(
        "/api/admin/sponsored-mint/start", headers=service_headers["telegram"]
    ).status == 403
    response = client.post(
        "/api/admin/sponsored-mint/start",
        headers=service_headers["discord"],
        json={"actor": "admin:42"},
    )
    assert response.status == 200
    assert response.json()["state"] == "active"
```

Also assert duplicate start returns the same active campaign, repeated stop is idempotent, actor is required, status exposes countdown/cap/reserved/minted/accepted/burn counts/unique count/balance, and routes are registered before the static mount.

- [ ] **Step 2: Run tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_sponsored_admin.py -v`

Expected: FAIL because the routes and SDK methods do not exist.

- [ ] **Step 3: Add service handlers**

Implement a shared guard:

```python
def _require_discord_surface(request):
    if request["surface"] != "discord":
        return web.json_response({"error": "forbidden", "code": "wrong_surface"}, status=403)
    return None
```

Decorate all three handlers with `@require_service_token`, pass `db_path.app_db_path(config.XRPL_NETWORK)` and `history_store.history_db_path(config.XRPL_NETWORK)`, require non-empty `actor` for mutations, and serialize dataclasses with `dataclasses.asdict`. Fetch project LFGO balance with `xrpl_ops.get_trustline_balance(config.SIGNING_ACCOUNT, ...)`; expose `null` on RPC failure without failing campaign control.

- [ ] **Step 4: Add SDK methods**

Implement direct service-token calls:

```python
async def sponsored_mint_status(self) -> dict[str, Any]:
    return await self._request("GET", "/api/admin/sponsored-mint/status", token=self._service_token)

async def sponsored_mint_start(self, actor: str) -> dict[str, Any]:
    return await self._request(
        "POST", "/api/admin/sponsored-mint/start", token=self._service_token,
        json={"actor": actor},
    )
```

Add the symmetric stop method.

- [ ] **Step 5: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/test_sponsored_admin.py tests/test_sdk_x_admin.py tests/test_x_admin_toggle.py -v`

Expected: all PASS.

```bash
git add lfg_service/app.py surfaces/_client/client.py tests/test_sponsored_admin.py
git commit -m "feat: expose sponsored mint admin API"
```

---

### Task 3: Discord admin controls and per-interaction authorization

**Files:**
- Modify: `surfaces/discord_bot/admin.py`
- Modify: `tests/test_sponsored_admin.py`
- Modify: `tests/test_discord_admin_x_toggle.py`

**Interfaces:**
- Consumes Task 2 SDK methods.
- Produces `AdminView.interaction_check(interaction) -> bool`.
- Produces `_sponsored_status_embed(status: dict[str, Any]) -> Embed`.

- [ ] **Step 1: Write interaction-gate and button tests**

Assert a non-administrator cannot press any button even if they obtain an active view, receives an ephemeral denial, and does not call the service. Assert an administrator can start, stop, and refresh, the actor sent is `discord:<user.id>`, and the status embed includes countdown, admitted/100, accepted/tagged, unique/300, balance, burned/pending, and last operator.

- [ ] **Step 2: Run the focused tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_sponsored_admin.py tests/test_discord_admin_x_toggle.py -v`

Expected: FAIL because `AdminView.interaction_check` and sponsored controls are absent.

- [ ] **Step 3: Add the gate and controls**

Implement:

```python
async def interaction_check(self, interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    if not perms or not perms.administrator:
        if interaction.response.is_done():
            await interaction.followup.send("Administrator permission required.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "Administrator permission required.", ephemeral=True
            )
        logging.warning("Rejected /admin interaction from %s", interaction.user)
        return False
    return True
```

Add Start/Stop/Refresh buttons that defer ephemerally, call the SDK, update labels/disabled state from authoritative status, and log the action through `log_admin_action`. Keep the command-level `@has_permissions(administrator=True)` decorator.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/test_sponsored_admin.py tests/test_discord_admin_x_toggle.py tests/test_discord_buttons.py -v`

Expected: all PASS.

```bash
git add surfaces/discord_bot/admin.py tests/test_sponsored_admin.py tests/test_discord_admin_x_toggle.py
git commit -m "feat: control sponsored mints from Discord admin"
```

---

### Task 4: Sponsored reservation and payment-free mint execution

**Files:**
- Modify: `lfg_core/mint_flow.py`
- Modify: `lfg_service/app.py`
- Create: `tests/test_sponsored_mint_flow.py`
- Modify: `tests/test_mint_cancel.py`

**Interfaces:**
- Consumes Task 1 reservation and claim transitions.
- Extends `MintSession(..., sponsored: bool = False)` with `sponsorship_reason`, `claim_wallet`, and serialized `sponsored`.
- Produces `run_mint_session(session, *, on_sponsored_mint=None, on_sponsored_offer=None)`.

- [ ] **Step 1: Write service admission tests**

Test that active/unseen wallets return `sponsored: true`, `pay_with: "SPONSORED"`, no payment link/UUID, and start the mint task. Test known wallets, inactive campaigns, history failures, and reservation-store failures use the exact paid preparation path. Test bulk mint never consults sponsorship.

- [ ] **Step 2: Write pipeline boundary tests**

Use `mint_one_unit` fakes to assert:

```python
async def test_sponsored_session_never_waits_for_payment(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "wait_for_payment", forbidden)
    monkeypatch.setattr(xrpl_ops, "buy_and_burn", forbidden)
    await mint_flow.run_mint_session(sponsored_session(), on_sponsored_mint=record_mint)
    assert session.state == mint_flow.OFFER_READY
```

Assert `mark_minting` happens before `mint_one_unit`; NFT confirmation consumes the claim and enqueues a burn before offer completion; offer failure retains consumed claim/NFT ID; cancellation before mint releases the claim; stop/expiry after reservation does not alter execution.

- [ ] **Step 3: Run tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_sponsored_mint_flow.py tests/test_mint_cancel.py -v`

Expected: FAIL because sessions have no sponsored branch.

- [ ] **Step 4: Extend `MintSession` and serialization**

Add fields:

```python
self.sponsored = sponsored
self.sponsorship_reason = "source_tag_newcomer" if sponsored else None
self.claim_wallet = wallet_address if sponsored else None
```

For sponsored sessions set `pay_with="SPONSORED"` and `pay_amount="0"`. `prepare_payment`, `regenerate_payment`, `_payment_params`, and `ensure_payment_fallback` must reject or no-op appropriately for sponsored sessions. Include `sponsored` and `sponsorship_reason` in `to_dict`.

- [ ] **Step 5: Branch `run_mint_session`**

For paid sessions retain the current payment/buyback block byte-for-byte. For sponsored sessions set `GENERATING` without calling payment code, transition the claim to `minting`, and use `_on_mint` to call `record_minted_and_enqueue_burn` before settling headroom. After `mint_one_unit` returns an offer ID, call `record_offer`. If the mint callback persistence fails after the NFT lands, set a recovery-visible error and do not release/reacquire the claim.

- [ ] **Step 6: Reserve in `handle_mint_start`**

After session creation/headroom reservation but before paid payload creation, call `reserve_if_eligible` in `asyncio.to_thread`. If sponsored, set the session fields and launch the normal wrapper immediately. If not sponsored for any reason, continue through existing `prepare_payment`. On cancellation/terminalization before mint, invoke `release_reservation`; never release if `nft_id` exists or store state is `minting` with unverified submission.

- [ ] **Step 7: Run regression tests and commit**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_sponsored_mint_flow.py tests/test_mint_cancel.py \
  tests/test_mint_one_unit.py tests/test_service_mint_platform.py \
  tests/test_headroom.py webapp/test_smoke.py -v
```

Expected: all PASS.

```bash
git add lfg_core/mint_flow.py lfg_service/app.py tests/test_sponsored_mint_flow.py tests/test_mint_cancel.py
git commit -m "feat: add payment-free sponsored mint path"
```

---

### Task 5: Durable LFGO burn worker and reconciliation

**Files:**
- Create: `lfg_core/sponsored_burn.py`
- Modify: `lfg_core/xrpl_ops.py`
- Modify: `lfg_core/memos.py`
- Modify: `lfg_service/app.py`
- Create: `tests/test_sponsored_burn.py`
- Modify: `tests/test_xrpl_source_tag.py`
- Modify: `tests/test_memos_transactions.py`

**Interfaces:**
- Produces `xrpl_ops.submit_sponsored_burn(memo_id: str) -> BurnSubmission`.
- Produces `xrpl_ops.find_sponsored_burn(memo_id: str) -> str | None`.
- Produces `sponsored_burn.process_one(db_path: str, *, submit, reconcile, now: int | None = None) -> bool`.
- Produces `sponsored_burn.run_worker(db_path: str, stop: asyncio.Event) -> None`.

- [ ] **Step 1: Write obligation and result-class tests**

Assert one confirmed claim creates one obligation with deterministic memo `fm-<claim-id-prefix>`, repeat callbacks create none, successful submission stores one hash, deterministic failure stays pending with backoff, and transport/timeout uncertainty becomes `indeterminate`.

- [ ] **Step 2: Write reconciliation tests**

Assert an indeterminate obligation first calls `find_sponsored_burn`; if found it becomes burned without submit; if absent after a completed account-history scan it returns to pending and submits on a later worker pass; if reconciliation itself fails it stays indeterminate. Assert concurrent workers lease only one row.

- [ ] **Step 3: Run tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_sponsored_burn.py -v`

Expected: FAIL because the worker and XRPL interfaces do not exist.

- [ ] **Step 4: Add provenance action and explicit XRPL outcomes**

Add `ACTION_SPONSORED_MINT_BURN = "sponsored-mint-burn"` to the memo enum. Define:

```python
@dataclass(frozen=True)
class BurnSubmission:
    state: Literal["validated", "failed", "indeterminate"]
    tx_hash: str | None
    error: str | None
```

`submit_sponsored_burn` sends exactly `config.MINT_PRICE_LFGO` from `SIGNING_ACCOUNT` to `TOKEN_ISSUER_ADDRESS`, sets `SOURCE_TAG`, and stamps `campaign=memo_id`. Unlike `buy_and_burn`, it preserves whether `submit_and_wait` returned a validated engine failure or raised after possible submission.

- [ ] **Step 5: Implement history reconciliation**

`find_sponsored_burn` requests validated `account_tx` history for `SIGNING_ACCOUNT`, decodes provenance memos with existing memo helpers, and returns the transaction hash only when all fields match: successful `Payment`, correct issuer/currency/value/destination, SourceTag, backend initiator, sponsored-burn action, and exact campaign memo. Return a separate complete/not-complete result so RPC failure is never interpreted as proof of absence.

- [ ] **Step 6: Implement worker leasing and lifecycle**

Use a short SQLite lease (`submitting`, `lease_until`) so crashes make work reclaimable. Process pending obligations after `next_attempt_at`; reconcile `indeterminate` first; use bounded exponential backoff; write every state transition before returning. Start one worker from aiohttp startup and set/await its stop event during cleanup.

- [ ] **Step 7: Run tests and commit**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_sponsored_burn.py tests/test_xrpl_source_tag.py \
  tests/test_memos_transactions.py tests/test_signing_account.py -v
```

Expected: all PASS.

```bash
git add lfg_core/sponsored_burn.py lfg_core/xrpl_ops.py lfg_core/memos.py \
  lfg_service/app.py tests/test_sponsored_burn.py tests/test_xrpl_source_tag.py \
  tests/test_memos_transactions.py
git commit -m "feat: reconcile sponsored LFGO burns"
```

---

### Task 6: Validated acceptance tracking and SourceTag metrics

**Files:**
- Modify: `scripts/onchain_listener.py`
- Modify: `scripts/backfill_history.py`
- Modify: `lfg_core/sponsored_mint.py`
- Create: `tests/test_sponsored_acceptance.py`
- Modify: `tests/test_onchain_listener.py`
- Modify: `tests/test_backfill_onchain.py`

**Interfaces:**
- Consumes `record_acceptance(db_path, network, wallet, tx_hash, now)`.
- Produces `observe_sponsored_acceptance(tx: dict, meta: dict, *, network: str) -> bool`.

- [ ] **Step 1: Write validated-listener tests**

Assert only a validated `tesSUCCESS` `NFTokenAcceptOffer` with LFG SourceTag and an account matching an `offered`/`minted` consumed claim is recorded. Wrong tag, failed engine result, backend wallet, different network, duplicate transaction, and unrelated acceptance are ignored. Assert the admin unique count comes from distinct archive accounts excluding project wallets.

- [ ] **Step 2: Run tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_sponsored_acceptance.py -v`

Expected: FAIL because the observer does not exist.

- [ ] **Step 3: Add the observer**

After `history_store.insert_tx` succeeds, call the pure observer for qualifying accept transactions. The observer derives the submitting `Account`, verifies `SourceTag == config.SOURCE_TAG`, checks `meta.TransactionResult == "tesSUCCESS"`, and idempotently stores `accept_tx_hash`/`tagged_at`. Apply the identical hook in replay/backfill so rebuilding history also repairs claims.

- [ ] **Step 4: Add admin metric assertions**

Extend Task 2 status tests so `unique_wallets` queries `COUNT(DISTINCT account)` using the same exclusion set as eligibility, while `accepted` counts claims with a validated recorded transaction. Ensure project burn/mint transactions never increase the unique count.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_sponsored_acceptance.py tests/test_onchain_listener.py \
  tests/test_backfill_onchain.py tests/test_history_store.py -v
```

Expected: all PASS.

```bash
git add scripts/onchain_listener.py scripts/backfill_history.py \
  lfg_core/sponsored_mint.py tests/test_sponsored_acceptance.py \
  tests/test_onchain_listener.py tests/test_backfill_onchain.py
git commit -m "feat: track tagged sponsored mint acceptances"
```

---

### Task 7: Sponsored web UX

**Files:**
- Modify: `webapp/client/mint_pure.js`
- Modify: `webapp/client/app.js`
- Modify: `tests/test_mint_pure_js.py`

**Interfaces:**
- Consumes session JSON fields `sponsored`, `sponsorship_reason`, `pay_with`, and `pay_amount`.
- Produces `sponsoredMintCopy(session) -> {title, body, action}` or `null`.

- [ ] **Step 1: Write pure UI tests**

Assert sponsored sessions render “Sponsored mint”, “No XRP or LFGO payment”, and an instruction that the user must accept the NFT in Xaman. Assert no payment QR, payment spinner, regenerate-payment button, or payment timeout language appears. Assert paid LFGO/XRP snapshots remain byte-for-byte unchanged.

- [ ] **Step 2: Run tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_mint_pure_js.py -v`

Expected: FAIL because sponsored presentation is not implemented.

- [ ] **Step 3: Implement the pure helper and rendering branch**

Add:

```javascript
export function sponsoredMintCopy(session) {
  if (!session?.sponsored) return null;
  return {
    title: "Sponsored mint",
    body: "No XRP or LFGO payment. We’ll mint your NFT, then you’ll accept it in Xaman.",
    action: "Mint my free NFT",
  };
}
```

At mint-start and polling render points, branch on `session.sponsored` before payment UI. Preserve generating/minting/offer-ready states and existing accept QR/deeplink behavior.

- [ ] **Step 4: Run UI and smoke tests**

Run: `.venv/bin/python -m pytest tests/test_mint_pure_js.py webapp/test_smoke.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/mint_pure.js webapp/client/app.js tests/test_mint_pure_js.py
git commit -m "feat: show sponsored mint experience"
```

---

### Task 8: Restart recovery, readiness audit, and operator runbook

**Files:**
- Modify: `lfg_core/sponsored_mint.py`
- Modify: `lfg_service/app.py`
- Modify: `tests/test_sponsored_mint_flow.py`
- Create: `scripts/audit_sponsored_mint_readiness.py`
- Create: `docs/ops/sponsored-free-mint.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces `recover_incomplete_claims(db_path: str, history_path: str, *, network: str) -> RecoveryReport`.
- Produces readiness script exit `0` only when archive, exclusions, listener freshness, balance, and debt checks pass.

- [ ] **Step 1: Write restart recovery tests**

Seed claims in `reserved`, `minting`, `minted`, and `offered` states, simulate restart, and assert: reversible stale reservations release; `minting` remains held until on-chain reconciliation proves no mint; minted claims retain debt and expose offer recovery; expired campaigns remain off; worker leases become reclaimable after expiry.

- [ ] **Step 2: Run tests and verify red**

Run: `.venv/bin/python -m pytest tests/test_sponsored_mint_flow.py tests/test_sponsored_burn.py -v`

Expected: FAIL on missing recovery entry point.

- [ ] **Step 3: Implement startup recovery**

Run schema initialization and `recover_incomplete_claims` during aiohttp startup before accepting mint requests. Reconcile minting claims using stored transaction/NFT evidence and existing LFG/on-chain indexes; never release merely because the process restarted. Queue missing offer recovery through the existing pending-offer mechanisms and leave consumed claims consumed.

- [ ] **Step 4: Add readiness audit**

The script accepts `--network`, `--app-db`, and `--history-db`; prints a concise PASS/FAIL line for schema, active campaign state, latest archived ledger time, unique count, exclusion membership, signing-wallet LFGO balance, pending/indeterminate burn debt, and incomplete claims. `--json` emits the same report for automation. It must not mutate campaign state or submit transactions.

- [ ] **Step 5: Write the exact runbook**

Document:

```text
1. Run full tests and readiness audit on staging.
2. Start a shortened testnet campaign via Discord /admin.
3. Exercise known-wallet paid flow and unseen-wallet sponsored flow.
4. Confirm mint, locked offer, tagged acceptance, unique increment, and one burn.
5. Restart service with an in-flight reservation and pending burn; verify recovery.
6. Merge to main, deploy staging, rerun audit.
7. Promote with scripts/promote.sh only after staging evidence is green.
8. On production, verify feature OFF, then perform one controlled campaign/mint.
9. Stop from /admin for rollback; paid mint remains live and debt worker continues.
```

Include SQL/read-only commands for claims and burn debt, log patterns, and the rule that code rollback never deletes the tables.

- [ ] **Step 6: Run recovery tests and audit help**

Run:

```bash
.venv/bin/python -m pytest tests/test_sponsored_mint_flow.py tests/test_sponsored_burn.py -v
.venv/bin/python scripts/audit_sponsored_mint_readiness.py --help
```

Expected: tests PASS; help exits 0 and lists all arguments.

- [ ] **Step 7: Commit**

```bash
git add lfg_core/sponsored_mint.py lfg_service/app.py \
  tests/test_sponsored_mint_flow.py scripts/audit_sponsored_mint_readiness.py \
  docs/ops/sponsored-free-mint.md CLAUDE.md
git commit -m "docs: add sponsored mint recovery runbook"
```

---

### Task 9: Full verification and release candidate

**Files:**
- Modify only files needed to correct failures found by verification.

**Interfaces:**
- Consumes every prior task.
- Produces a review-ready branch with no unresolved automated review comments.

- [ ] **Step 1: Run format, lint, type, and full test gates**

Run the repository-prescribed commands from `pyproject.toml`/CI:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy lfg_core lfg_service surfaces
.venv/bin/python -m pytest -q
```

Expected: every command exits 0.

- [ ] **Step 2: Run invariant-focused tests again**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_sponsored_mint_store.py tests/test_sponsored_admin.py \
  tests/test_sponsored_mint_flow.py tests/test_sponsored_burn.py \
  tests/test_sponsored_acceptance.py tests/test_xrpl_source_tag.py \
  tests/test_mint_cancel.py tests/test_headroom.py webapp/test_smoke.py -v
```

Expected: all PASS.

- [ ] **Step 3: Perform manual security review**

Verify from the diff that:

- every admin route has both service-token and Discord-surface enforcement;
- `AdminView.interaction_check` gates every component;
- no sponsorship is returned before reservation commits;
- every post-mint path preserves the consumed claim;
- no burn retry can bypass memo reconciliation;
- no mainnet secret or wallet seed appears in logs, API JSON, tests, or docs.

- [ ] **Step 4: Commit verification fixes**

If verification required changes:

```bash
git add <only-the-files-corrected>
git commit -m "fix: close sponsored mint verification gaps"
```

If no changes were required, do not create an empty commit.

- [ ] **Step 5: Request automated review and close threads**

Push the branch, open a PR against `main`, wait for CI plus Greptile and CodeRabbit, address every actionable comment, reply with the evidence, and resolve the thread. “Green” alone is not release-ready.

- [ ] **Step 6: Stage and promote**

After merge to `main`, deploy staging and execute `docs/ops/sponsored-free-mint.md`. Promote to `deploy` only after the controlled staging flow succeeds. Verify production campaign state is OFF before considering the deployment complete.
