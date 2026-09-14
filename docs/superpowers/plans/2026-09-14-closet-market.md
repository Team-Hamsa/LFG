# Closet Market Implementation Plan (#443, with #496 folded in)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let holders trade loose Closet traits through an off-ledger order book. Asks are free DB rows. Bids lock BRIX in an XRPL TokenEscrow. Fills settle through the app wallet: funds, then the asset move, then the forward payment, then the Closet mirror. The trait marketplace shows the book: best bid on each card, a place-bid form, and "bids on my traits" with Fill or Deposit & fill.

**Architecture:**
- `lfg_core/closet_market_store.py` (sync sqlite, same `onchain_<net>.db` as `closet_assets`) owns orders, fills, encumbrance, auto-cross and the one-transaction asset move.
- `lfg_core/closet_market_flow.py` (async, dependency-injected like `economy_flow`/`shop_flow`) drives the rest: the bid escrow lifecycle and the fill settlement state machine. Every backend tx is submitted with a pinned `LastLedgerSequence` and resolved by memo lookup, following the `recover_brix_claims` rule.
- `lfg_service/app.py` exposes `/api/closet/*` and runs the machine inline plus from the existing 2-minute `_settlement_sweep_loop`.
- The vanilla-JS client gets a pure helper module and wiring in `app.js`.

**Tech Stack:** Python 3.10, aiohttp, sqlite3, xrpl-py 5.0.0 (`EscrowCreate/Finish/Cancel`, `LedgerEntry`), `cryptography.Fernet`, Xaman payloads via `xumm_ops._create_xumm_payload`, vanilla ES modules tested under Node via pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-closet-market-design.md` (design approved 2026-08-25). Additions from #496 are in https://github.com/Team-Hamsa/LFG/issues/443#issuecomment-5671943552.

## Global Constraints

- **SourceTag:** every XRPL transaction and Xaman payload carries `SourceTag = 2606160021`. Backend txs set `source_tag=config.SOURCE_TAG`; user payloads get it from `_create_xumm_payload`. Never omit it.
- **Provenance memos:** every tx carries provenance memos (`memos.build_memo_models` backend / `memos.build_memos_json` user). New actions: `closet-bid`, `closet-buy`, `closet-fill`, `closet-forward`, `closet-refund`.
- **Signer pin (#314):** signer == session wallet on every user payload (`Account` pinned in txjson), and the signed account is re-checked on read-back.
- **Unknown is not failure:** an unknown tx outcome is never treated as failure. It resolves only by finding the memo-tagged tx (confirmed) or by the validated ledger passing the tx's `LastLedgerSequence` with no tx found (failed).
- **Supply-neutral:** no `supply_changes` rows. `audit_trait_economy` is unchanged.
- **Feature flags:**
  - `CLOSET_MARKET_ENABLED=0` by default.
  - New orders require `ECONOMY_ENABLED` + `CLOSET_MARKET_ENABLED` + `CLOSET_MARKET_ENC_KEY`.
  - Cancel, status and settlement keep running with only `ECONOMY_ENABLED` + `CLOSET_MARKET_ENC_KEY`, so turning the flag off never strands escrowed BRIX (same posture as `SHOP_ENABLED`).
- `CLOSET_MARKET_FEE_BPS` defaults to `700`, allowed range `0 ≤ bps < 10000`. `CLOSET_BID_TTL_SECONDS` defaults to `604800`.
- **BRIX amounts** are strings normalised by `market_ops.validate_brix_value` (>0, ≤6 dp, ≤1e15). Decimal math only. Fee = `price × bps / 10000` rounded DOWN to 6 dp.
- **BRIX pair:** always `config.BRIX_CURRENCY_HEX` / `config.BRIX_ISSUER`, never `TOKEN_*` (LFGO).
- **App wallet** is `config.SIGNING_ACCOUNT`, signed with `Wallet.from_seed(config.SEED)` (mainnet: regkey for `rLfgoMint…`).
- **Economy network:** all Closet Market tables resolve via `config.ECONOMY_NETWORK`.
- **Tests** never read frozen `config.X` constants for defaults (use `config.env_flag("X", config.X_DEFAULT)`). They monkeypatch config attributes instead. New test files need no env preamble (root `conftest.py`).
- **Client cache-busters:** `?v=` bumps travel with the PR. `app.js?v=` is pinned exactly in `tests/test_harvest_pure_js.py` and `tests/test_app_js_deeplink.py`. The CSS version lives in the filename (`style.vNN.css`). Check `main` for the latest numbers right before bumping (parallel branches collide).
- **Pre-push gate:** ruff, mypy, gitleaks and pytest block pushes. Never `--no-verify`. In a worktree, symlink the venv first (`ln -s /home/hamsa/LFG/.venv .venv`), or the gate silently skips.

## Spec deltas (decided while planning — each is implemented below)

1. **Wrong issuer account in the spec's "Ledger prerequisites".** Verified on mainnet 2026-09-14:
   - The BRIX issuer is `rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px` (`config._default_brix_issuer` on mainnet; prod `.env` does not override it), not `rLfgoMint…`.
   - Its master key is disabled. Its RegularKey is `rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ` (= `BRIX_DISTRIBUTOR_ADDRESS`), so `AccountSet SetFlag=17` is signed with `BRIX_DISTRIBUTOR_SEED`, with `Account=rLfgoBriX…`.
   - `lsfAllowTrustLineLocking` is still NOT set.
2. **App wallet headroom.** `rLfgoMint…` holds a BRIX line with limit **258,055**, balance 45,509 (2026-09-14). `EscrowFinish` into it fails once `balance + amount > limit`, so the ops step raises the limit to `BRIX_TRUSTLINE_LIMIT` (1e9).
3. **Testnet app wallet == BRIX issuer.** XRPL docs: "issuers can't create escrows with their own issued tokens, [but] they can serve as recipients … works the same way as a direct payment." Escrow → issuer burns and forwards from the issuer mint, so it is net-neutral per fill and the fee is effectively burned. The audit's balance check is skipped when `SIGNING_ACCOUNT == BRIX_ISSUER`.
4. **Distinct memo tags per phase.**
   - Backend txs carry `lfg:closet_<phase>:<id>` with phase ∈ `finish, forward, overshoot, refund, cancel, cancel_finish, cancel_refund`, so recovery lookups are unambiguous.
   - The buyer's Payment is identified by `InvoiceID = sha256(fill_id)`, not a memo, because user payload memos are the schema-stamped provenance set.
5. **Memo action names are prefixed** `closet-*` so they don't collide with #283's `bid`. There is no `ask` action: asks never touch the ledger.
6. **Endpoint changes.** One fill status endpoint, `GET /api/closet/fill/{fill_id}`, replaces `GET /api/closet/ask/{id}/buy/{fill_id}`. Added `GET /api/closet/keys` (bid picker: every known `(slot, value)` from `trait_rarity`).
7. **Extra states.**
   - Orders gain `matched` (a fill owns it) and `cancelling`; fills gain `failed`.
   - Escrow-funded fills pre-check "buyer Closet active & seller holds the unit" BEFORE `EscrowFinish`, so an undeliverable match never costs a finish plus refund.
8. **DB-first asset move needs two guards the spec didn't state:**
   - (a) The listener (`nft_listener._apply_closet`) and `scripts/backfill_economy._reconcile_closet` must NOT rebuild `closet_assets` for an owner with an unmirrored fill. Otherwise pre-fill Closet metadata resurrects the moved unit (duplication).
   - (b) Step 2 holds BOTH owners' `owner_lock`s (sorted), because Equip/Assemble/Extract full-overwrite `closet_assets` from a snapshot read under that lock.
9. **Ask buys need no BRIX trustline.** The BRIX lands in the app wallet; non-holders pay XRP via `SendMax` on the same Payment (`detect_payment_path`).
10. **Encumbrance** also counts holder fills of a bid (no ask row) while they're pre-move.
11. **Closet metadata `orders` block** is written by every `sync_closet` from the DB and omitted when empty. Creating or cancelling an ask does not trigger a modify.
12. **Bid cancellation** is allowed only from `open` (a `pending_escrow` bid just lets its payload expire).
13. **#496 additions:**
    - Grouped trait cards carry `book` (best bid/ask).
    - Place bid from the card detail and from a Wanted tab.
    - `orders/mine` returns `bids_on_my_traits` with `source: closet|token`. A token holder gets "Deposit & fill": the existing `/api/deposit` flow, then the fill.
    - Status only says done once the ledger-backed state says so.

## File Structure

| File | Responsibility |
|---|---|
| `lfg_core/config.py` (modify) | flags, fee/TTL/key/margin knobs, `closet_market_enabled()`, `closet_market_settleable()` |
| `lfg_core/memos.py` (modify) | five `closet-*` actions |
| `lfg_core/crypto_condition.py` (create) | PREIMAGE-SHA-256 condition/fulfillment DER encoding + Fernet seal/unseal |
| `lfg_core/closet_market_store.py` (create) | schema, asks, bids, fills, encumbrance, book, auto-cross, asset move, sweep queries, tags |
| `lfg_core/economy_store.py` (modify) | `init_economy_schema` also ensures the Closet Market schema |
| `lfg_core/economy_flow.py` / `webapp/economy_api.py` (modify) | encumbrance gates in Equip / Assemble / Extract |
| `lfg_core/closet_token.py` (modify) | `orders` block in Closet metadata |
| `lfg_core/nft_listener.py`, `scripts/backfill_economy.py` (modify) | unmirrored-fill rebuild guard |
| `lfg_core/xrpl_ops.py` (modify) | `TxOutcome`, `TxNotSubmitted`, `app_brix_payment`, `escrow_finish`, `escrow_cancel`, `get_escrow`, `find_app_txs_by_memo`, `tx_entry_hash`, `tx_entry_result` |
| `lfg_core/xumm_ops.py` (modify) | `create_closet_bid_payload`, `create_closet_buy_payload` |
| `lfg_core/closet_market_flow.py` (create) | `ClosetMarketDeps`, `advance_bid`, `settle_fill` |
| `lfg_service/app.py` (modify) | gates, deps builder, mirror fn, `/api/closet/*`, views, sweep, browse `book` annotation, `/api/config` |
| `scripts/audit_closet_market.py` (create) | nightly conservation/escrow audit |
| `scripts/closet_market_setup.py` (create) | issuer flag + app trustline limit (check / apply) |
| `scripts/closet_market_e2e.py` (create) | testnet end-to-end against the real ledger |
| `ecosystem.prod.config.js`, `ecosystem.staging.config.js` (modify) | audit cron |
| `docs/ops/closet-market.md` (create), `CLAUDE.md` (modify) | ops runbook + env docs |
| `webapp/client/closet_market_pure.js` (create) | pure labels / money math for the UI |
| `webapp/client/app.js`, `index.html`, `style.vNN.css` (modify) | Wanted tab, Mine sections, bid form, card badge, flows |
| `tests/test_closet_market_*.py` (create) | one test file per task, as named in each task |

---

### Task 1: Config flags, memo actions, `/api/config`

**Files:**
- Modify: `lfg_core/config.py` (after the `SHOP_*` block, ~line 455)
- Modify: `lfg_core/memos.py` (action constants ~line 63-115 and `_ACTIONS`)
- Modify: `lfg_service/app.py` `handle_config` (~line 8466)
- Test: `tests/test_closet_market_config.py`

**Interfaces:**
- Produces:
  - `config.CLOSET_MARKET_ENABLED_DEFAULT: str`, `config.CLOSET_MARKET_ENABLED: bool`
  - `config.CLOSET_MARKET_FEE_BPS: int`, `config.CLOSET_BID_TTL_SECONDS: int`
  - `config.CLOSET_MARKET_ENC_KEY: str`, `config.CLOSET_MARKET_LEDGER_MARGIN: int`
  - `config.validate_closet_market_fee_bps(bps: int) -> None`
  - `config.closet_market_enabled() -> bool`, `config.closet_market_settleable() -> bool`
  - `memos.ACTION_CLOSET_BID`, `ACTION_CLOSET_BUY`, `ACTION_CLOSET_FILL`, `ACTION_CLOSET_FORWARD`, `ACTION_CLOSET_REFUND`
  - `/api/config` keys `closet_market_enabled: bool`, `closet_market_fee_bps: int`, `closet_bid_ttl_seconds: int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_config.py
import asyncio
import json

import pytest
from aiohttp.test_utils import make_mocked_request

from lfg_core import config, memos
from lfg_service import app as server


def test_closet_market_ships_off():
    assert config.env_flag("CLOSET_MARKET_ENABLED", config.CLOSET_MARKET_ENABLED_DEFAULT) is False


def test_enabled_needs_economy_flag_and_key(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "")
    assert config.closet_market_enabled() is False
    assert config.closet_market_settleable() is False
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "k")
    assert config.closet_market_enabled() is True
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", False)
    assert config.closet_market_enabled() is False
    assert config.closet_market_settleable() is True  # kill switch keeps settlement alive
    monkeypatch.setattr(config, "ECONOMY_ENABLED", False)
    assert config.closet_market_settleable() is False


@pytest.mark.parametrize("bps", [-1, 10000, 25000])
def test_fee_bps_out_of_range_rejected(bps):
    with pytest.raises(ValueError):
        config.validate_closet_market_fee_bps(bps)


@pytest.mark.parametrize("bps", [0, 700, 9999])
def test_fee_bps_in_range_ok(bps):
    config.validate_closet_market_fee_bps(bps)


@pytest.mark.parametrize(
    "action", ["closet-bid", "closet-buy", "closet-fill", "closet-forward", "closet-refund"]
)
def test_closet_memo_actions_are_in_the_closed_enum(action):
    assert memos.build_memos_json(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, action)
    assert memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, action)


def test_config_endpoint_exposes_closet_market(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "k")
    monkeypatch.setattr(config, "CLOSET_MARKET_FEE_BPS", 250)
    resp = asyncio.new_event_loop().run_until_complete(
        server.handle_config(make_mocked_request("GET", "/api/config"))
    )
    body = json.loads(resp.body)
    assert body["closet_market_enabled"] is True
    assert body["closet_market_fee_bps"] == 250
    assert body["closet_bid_ttl_seconds"] == config.CLOSET_BID_TTL_SECONDS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_config.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.config' has no attribute 'CLOSET_MARKET_ENABLED_DEFAULT'`

- [ ] **Step 3: Implement config**

Add to `lfg_core/config.py` directly after the `SHOP_OFFER_TTL_SECONDS` line:

```python
# Closet Market (#443): off-ledger trait asks + TokenEscrow-backed bids.
# CLOSET_MARKET_ENABLED gates NEW orders only; cancel/status/settlement run
# whenever closet_market_settleable() so flipping the flag off never strands
# BRIX locked in an escrow (same posture as SHOP_ENABLED).
CLOSET_MARKET_ENABLED_DEFAULT = "0"
CLOSET_MARKET_ENABLED = env_flag("CLOSET_MARKET_ENABLED", CLOSET_MARKET_ENABLED_DEFAULT)
CLOSET_MARKET_FEE_BPS = int(os.getenv("CLOSET_MARKET_FEE_BPS", "700"))
CLOSET_BID_TTL_SECONDS = int(os.getenv("CLOSET_BID_TTL_SECONDS", "604800"))
# Fernet key encrypting each bid's escrow fulfillment at rest. Without it the
# backend could never finish (or refund) an escrow, so both gates require it.
CLOSET_MARKET_ENC_KEY = os.getenv("CLOSET_MARKET_ENC_KEY", "")
# LastLedgerSequence headroom on backend Closet Market txs — what makes an
# unknown outcome decidable (same role as BRIX_CLAIM_LEDGER_MARGIN).
CLOSET_MARKET_LEDGER_MARGIN = int(os.getenv("CLOSET_MARKET_LEDGER_MARGIN", "40"))


def validate_closet_market_fee_bps(bps: int) -> None:
    if not 0 <= bps < 10000:
        raise ValueError(f"CLOSET_MARKET_FEE_BPS must be in [0, 10000), got {bps}")


validate_closet_market_fee_bps(CLOSET_MARKET_FEE_BPS)


def closet_market_settleable() -> bool:
    """Cancel/status/settlement may run (escrows can be finished/cancelled)."""
    return bool(ECONOMY_ENABLED and CLOSET_MARKET_ENC_KEY)


def closet_market_enabled() -> bool:
    """New asks/bids/buys/fills may be placed."""
    return bool(closet_market_settleable() and CLOSET_MARKET_ENABLED)
```

- [ ] **Step 4: Implement memo actions**

In `lfg_core/memos.py`, after `ACTION_LINK = "link"`:

```python
# Closet Market (#443). Prefixed so they never collide with #283's native
# NFTokenOffer `bid`. Asks have no action: they never touch the ledger.
ACTION_CLOSET_BID = "closet-bid"  # user EscrowCreate locking a bid's BRIX
ACTION_CLOSET_BUY = "closet-buy"  # user Payment taking an ask
ACTION_CLOSET_FILL = "closet-fill"  # backend EscrowFinish funding a fill
ACTION_CLOSET_FORWARD = "closet-forward"  # backend Payment app -> seller
ACTION_CLOSET_REFUND = "closet-refund"  # backend refund Payment / EscrowCancel
```

and add all five names to the `_ACTIONS = frozenset({...})` set.

- [ ] **Step 5: Expose the flag**

In `lfg_service/app.py` `handle_config`, add next to `"shop_enabled"`:

```python
            "closet_market_enabled": config.closet_market_enabled(),
            "closet_market_fee_bps": config.CLOSET_MARKET_FEE_BPS,
            "closet_bid_ttl_seconds": config.CLOSET_BID_TTL_SECONDS,
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_config.py tests/test_memos.py -v`
Expected: PASS (`tests/test_memos.py` enumerates actions in a parametrize list, not an exact set, so it needs no change).

- [ ] **Step 7: Commit**

```bash
git add lfg_core/config.py lfg_core/memos.py lfg_service/app.py tests/test_closet_market_config.py
git commit -m "feat(closet-market): config flags, memo actions, /api/config exposure (#443)"
```

---

### Task 2: PREIMAGE-SHA-256 crypto-conditions + sealed fulfillments

**Files:**
- Create: `lfg_core/crypto_condition.py`
- Test: `tests/test_crypto_condition.py`

**Interfaces:**
- Consumes: `config.CLOSET_MARKET_ENC_KEY`
- Produces:
  - `new_preimage() -> bytes` (32 random bytes)
  - `condition_hex(preimage: bytes) -> str` and `fulfillment_hex(preimage: bytes) -> str` (upper-case hex)
  - `seal(fulfillment: str) -> str` and `unseal(blob: str) -> str`
  - `class ConditionKeyError(RuntimeError)`

No crypto-conditions library is installed. The DER encoding is small and fixed: a condition is `A0 len [80 20 sha256] [81 len cost]`, and a fulfillment is `A0 len [80 len preimage]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crypto_condition.py
import hashlib

import pytest
from cryptography.fernet import Fernet
from xrpl.models.transactions import EscrowFinish

from lfg_core import config
from lfg_core import crypto_condition as cc

# The canonical empty-preimage vector from the XRPL escrow docs.
EMPTY_CONDITION = "A0258020E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855810100"
EMPTY_FULFILLMENT = "A0028000"


def test_empty_preimage_matches_published_vector():
    assert cc.condition_hex(b"") == EMPTY_CONDITION
    assert cc.fulfillment_hex(b"") == EMPTY_FULFILLMENT


def test_32_byte_preimage_shape():
    pre = bytes(range(32))
    digest = hashlib.sha256(pre).hexdigest().upper()
    assert cc.condition_hex(pre) == "A0258020" + digest + "810120"
    assert cc.fulfillment_hex(pre) == "A0228020" + pre.hex().upper()


def test_new_preimage_is_32_random_bytes():
    a, b = cc.new_preimage(), cc.new_preimage()
    assert len(a) == 32 and a != b


def test_escrow_finish_model_accepts_the_pair():
    pre = cc.new_preimage()
    EscrowFinish(
        account="rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        owner="rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        offer_sequence=7,
        condition=cc.condition_hex(pre),
        fulfillment=cc.fulfillment_hex(pre),
    ).validate()


def test_seal_roundtrip(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())
    blob = cc.seal("A0228020" + "00" * 32)
    assert "A022" not in blob
    assert cc.unseal(blob) == "A0228020" + "00" * 32


def test_unseal_without_key_raises(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "")
    with pytest.raises(cc.ConditionKeyError):
        cc.unseal("x")


def test_unseal_with_wrong_key_raises(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())
    blob = cc.seal("AA")
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(cc.ConditionKeyError):
        cc.unseal(blob)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_crypto_condition.py -v`
Expected: FAIL with `ImportError: cannot import name 'crypto_condition'`

- [ ] **Step 3: Implement**

```python
# lfg_core/crypto_condition.py
"""PREIMAGE-SHA-256 crypto-conditions for Closet Market bid escrows (#443).

An XRPL escrow created with a Condition can only be finished by a tx carrying
the matching Fulfillment. The backend generates the preimage per bid, hands
the Condition to the bidder's EscrowCreate, and keeps the Fulfillment sealed
(Fernet, CLOSET_MARKET_ENC_KEY) so only it can finish the escrow.

Encoding (RFC draft-thomas-crypto-conditions, DER, short-form lengths only):
  condition   = A0 len { 80 20 sha256(preimage)  81 len cost }   cost = len(preimage)
  fulfillment = A0 len { 80 len preimage }
"""

from __future__ import annotations

import binascii
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

from lfg_core import config

PREIMAGE_BYTES = 32


class ConditionKeyError(RuntimeError):
    """CLOSET_MARKET_ENC_KEY is missing or cannot open this blob."""


def new_preimage() -> bytes:
    return secrets.token_bytes(PREIMAGE_BYTES)


def _der_len(n: int) -> bytes:
    if n >= 128:
        raise ValueError("long-form DER lengths are not needed for PREIMAGE-SHA-256")
    return bytes([n])


def _uint(n: int) -> bytes:
    raw = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    return b"\x00" + raw if raw[0] & 0x80 else raw


def condition_hex(preimage: bytes) -> str:
    cost = _uint(len(preimage))
    body = b"\x80\x20" + hashlib.sha256(preimage).digest() + b"\x81" + _der_len(len(cost)) + cost
    return (b"\xa0" + _der_len(len(body)) + body).hex().upper()


def fulfillment_hex(preimage: bytes) -> str:
    body = b"\x80" + _der_len(len(preimage)) + preimage
    return (b"\xa0" + _der_len(len(body)) + body).hex().upper()


def _fernet() -> Fernet:
    key = config.CLOSET_MARKET_ENC_KEY
    if not key:
        raise ConditionKeyError("CLOSET_MARKET_ENC_KEY is not configured")
    try:
        return Fernet(key.encode())
    except (ValueError, binascii.Error) as exc:
        raise ConditionKeyError("CLOSET_MARKET_ENC_KEY is not a valid Fernet key") from exc


def seal(fulfillment: str) -> str:
    return _fernet().encrypt(fulfillment.encode()).decode()


def unseal(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, ValueError, TypeError, binascii.Error) as exc:
        raise ConditionKeyError("stored fulfillment cannot be decrypted") from exc
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_crypto_condition.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/crypto_condition.py tests/test_crypto_condition.py
git commit -m "feat(closet-market): PREIMAGE-SHA-256 condition encoding + sealed fulfillments (#443)"
```

---

### Task 3: Store I — schema, asks, encumbrance, book

**Files:**
- Create: `lfg_core/closet_market_store.py`
- Modify: `lfg_core/economy_store.py` `init_economy_schema` (~line 188)
- Test: `tests/test_closet_market_store_asks.py`

**Interfaces:**
- Consumes: `closet_assets(owner, slot, value, count)` and `closet_tokens(owner, status)` from `economy_store`
- Produces (all sync, `conn: sqlite3.Connection`):
  - Constants:
    - Sides: `SIDE_ASK`, `SIDE_BID`.
    - Order states: `PENDING_ESCROW`, `OPEN`, `MATCHED`, `CANCELLING`, `FILLED`, `CANCELLED`, `EXPIRED`.
    - Fill states: `FUNDS_PENDING`, `FUNDED`, `ASSET_MOVED`, `PAID`, `MIRRORED`, `REFUND_PENDING`, `REFUNDED`, `INDETERMINATE`, `FAILED`.
    - Fund sources: `FUNDS_PAYMENT`, `FUNDS_ESCROW`.
    - Other: `TERMINAL_FILL_STATES`, `HASH_UNKNOWN`.
  - `class OrderError(ValueError)` with `.code`
  - `ensure_schema(conn) -> None`, `new_id() -> str`
  - `fmt_brix(d: Decimal) -> str`, `fee_for(price_brix: str, bps: int) -> str`
  - `invoice_id(fill_id: str) -> str`, `memo_tag(phase: str, ident: str) -> str`
  - `get_order(conn, order_id) -> dict | None`, `get_fill(conn, fill_id) -> dict | None`
  - `update_order(conn, order_id, **fields) -> None`, `update_fill(conn, fill_id, **fields) -> None`
  - `encumbrance(conn, owner) -> dict[tuple[str, str], int]`, `holding_count(conn, owner, slot, value) -> int`, `available_count(conn, owner, slot, value) -> int`
  - `closet_active(conn, owner) -> bool`
  - `create_ask(conn, *, owner, slot, value, price_brix, platform, now=None) -> dict`, `cancel_ask(conn, order_id, owner) -> dict`
  - `book_summary(conn, slot=None, value=None) -> list[dict]`, `book_levels(conn, slot, value) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_store_asks.py
import sqlite3
from decimal import Decimal

import pytest

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es

SELLER, BUYER = "rSeller", "rBuyer"


def _conn():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)  # must also create the closet market tables
    for owner in (SELLER, BUYER):
        es.set_closet_token(c, owner, f"CLOSET-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 2)], [])
    return c


def test_init_economy_schema_creates_tables():
    c = _conn()
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"closet_orders", "closet_fills"} <= names


def test_active_constant_matches_closet_token():
    assert cms._ACTIVE == ct.ACTIVE


def test_create_ask_encumbers_without_touching_count():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="12.5", platform="discord")
    assert ask["state"] == cms.OPEN and ask["side"] == cms.SIDE_ASK
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 2
    assert cms.available_count(c, SELLER, "Head", "Crown") == 1
    assert cms.encumbrance(c, SELLER) == {("Head", "Crown"): 1}


def test_cannot_ask_more_than_held():
    c = _conn()
    for _ in range(2):
        cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="1", platform=None)
    with pytest.raises(cms.OrderError) as e:
        cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="1", platform=None)
    assert e.value.code == "not_available"


def test_ask_requires_active_closet():
    c = _conn()
    es.set_closet_status(c, SELLER, ct.PENDING_ACCEPT)
    with pytest.raises(cms.OrderError) as e:
        cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="1", platform=None)
    assert e.value.code == "closet_required"


def test_cancel_ask_frees_the_unit_and_is_owner_only():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="3", platform=None)
    with pytest.raises(cms.OrderError) as e:
        cms.cancel_ask(c, ask["id"], BUYER)
    assert e.value.code == "not_found"
    assert cms.cancel_ask(c, ask["id"], SELLER)["state"] == cms.CANCELLED
    assert cms.available_count(c, SELLER, "Head", "Crown") == 2
    with pytest.raises(cms.OrderError) as e:
        cms.cancel_ask(c, ask["id"], SELLER)
    assert e.value.code == "not_open"


def test_book_summary_and_levels_sort_by_decimal_not_text():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="9", platform=None, now=1)
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="10", platform=None, now=2)
    [row] = cms.book_summary(c)
    assert row == {
        "slot": "Head", "value": "Crown",
        "best_ask_brix": "9", "ask_count": 2, "best_bid_brix": None, "bid_count": 0,
    }
    levels = cms.book_levels(c, "Head", "Crown")
    assert [a["price_brix"] for a in levels["asks"]] == ["9", "10"]
    assert levels["bids"] == []


def test_fee_rounds_down_to_micro_brix():
    assert cms.fee_for("1", 700) == "0.07"
    assert cms.fee_for("0.000001", 700) == "0"
    assert cms.fee_for("12.345678", 250) == "0.308641"
    assert cms.fmt_brix(Decimal("5.000000")) == "5"


def test_memo_tags_and_invoice_id():
    assert cms.memo_tag("forward", "abc") == "lfg:closet_forward:abc"
    with pytest.raises(ValueError):
        cms.memo_tag("nope", "abc")
    inv = cms.invoice_id("abc")
    assert len(inv) == 64 and inv == inv.upper()


def test_update_order_rejects_unknown_fields_and_states():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="3", platform=None)
    with pytest.raises(ValueError):
        cms.update_order(c, ask["id"], owner="rEvil")
    with pytest.raises(ValueError):
        cms.update_order(c, ask["id"], state="bogus")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_store_asks.py -v`
Expected: FAIL with `ImportError: cannot import name 'closet_market_store'`

- [ ] **Step 3: Implement the store core**

```python
# lfg_core/closet_market_store.py
"""Closet Market order book (#443): off-ledger asks, escrow-backed bids, fills.

Lives in the per-network onchain_<net>.db beside closet_assets, so a fill's
asset move and both order closes are ONE sqlite transaction. The DB is
authoritative for orders and fills; the Closet NFToken's `lfg_closet.orders`
block is a display mirror written by the next sync_closet.

Must not import economy_store / closet_token (economy_store imports this
module to ensure the schema) — closet_assets / closet_tokens are touched with
plain SQL here.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import ROUND_DOWN, Decimal
from typing import Any

_ACTIVE = "active"  # == closet_token.ACTIVE (asserted in tests)

SIDE_ASK = "ask"
SIDE_BID = "bid"

PENDING_ESCROW = "pending_escrow"
OPEN = "open"
MATCHED = "matched"
CANCELLING = "cancelling"
FILLED = "filled"
CANCELLED = "cancelled"
EXPIRED = "expired"
ORDER_STATES = frozenset({PENDING_ESCROW, OPEN, MATCHED, CANCELLING, FILLED, CANCELLED, EXPIRED})

FUNDS_PENDING = "funds_pending"
FUNDED = "funded"
ASSET_MOVED = "asset_moved"
PAID = "paid"
MIRRORED = "mirrored"
REFUND_PENDING = "refund_pending"
REFUNDED = "refunded"
INDETERMINATE = "indeterminate"
FAILED = "failed"
FILL_STATES = frozenset(
    {FUNDS_PENDING, FUNDED, ASSET_MOVED, PAID, MIRRORED, REFUND_PENDING, REFUNDED, INDETERMINATE, FAILED}
)
TERMINAL_FILL_STATES = frozenset({MIRRORED, REFUNDED, FAILED})

FUNDS_PAYMENT = "payment"
FUNDS_ESCROW = "escrow"

# Stored when a tx is confirmed on-ledger but its hash could not be read.
# Never None: a None hash means "not done yet" to the state machine.
HASH_UNKNOWN = "confirmed-hash-unavailable"

_TAG_PHASES = frozenset(
    {"finish", "forward", "overshoot", "refund", "cancel", "cancel_finish", "cancel_refund"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS closet_orders (
    id               TEXT PRIMARY KEY,
    side             TEXT NOT NULL CHECK (side IN ('ask', 'bid')),
    owner            TEXT NOT NULL,
    slot             TEXT NOT NULL,
    value            TEXT NOT NULL,
    price_brix       TEXT NOT NULL,
    state            TEXT NOT NULL,
    created_ts       INTEGER NOT NULL,
    updated_ts       INTEGER NOT NULL,
    platform         TEXT,
    payload_uuid     TEXT,
    xumm_url         TEXT,
    qr_url           TEXT,
    push             TEXT,
    signed_txid      TEXT,
    escrow_tx_hash   TEXT,
    escrow_owner_seq INTEGER,
    condition        TEXT,
    fulfillment_enc  TEXT,
    cancel_after     INTEGER,
    cancel_reason    TEXT,
    cancel_finish_hash TEXT,
    cancel_refund_hash TEXT,
    pending_phase    TEXT,
    pending_lls      INTEGER,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_closet_orders_book ON closet_orders (state, side, slot, value);
CREATE INDEX IF NOT EXISTS idx_closet_orders_owner ON closet_orders (owner, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_one_live_bid ON closet_orders (owner, slot, value)
    WHERE side = 'bid' AND state IN ('pending_escrow', 'open', 'matched', 'cancelling');
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_escrow_hash ON closet_orders (escrow_tx_hash)
    WHERE escrow_tx_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS closet_fills (
    id                 TEXT PRIMARY KEY,
    ask_order_id       TEXT,
    bid_order_id       TEXT,
    funds_source       TEXT NOT NULL CHECK (funds_source IN ('payment', 'escrow')),
    seller             TEXT NOT NULL,
    buyer              TEXT NOT NULL,
    slot               TEXT NOT NULL,
    value              TEXT NOT NULL,
    price_brix         TEXT NOT NULL,
    fee_brix           TEXT NOT NULL,
    overshoot_brix     TEXT NOT NULL DEFAULT '0',
    state              TEXT NOT NULL,
    platform           TEXT,
    payload_uuid       TEXT,
    xumm_url           TEXT,
    qr_url             TEXT,
    push               TEXT,
    signed_txid        TEXT,
    payment_tx_hash    TEXT,
    escrow_finish_hash TEXT,
    forward_tx_hash    TEXT,
    overshoot_tx_hash  TEXT,
    refund_tx_hash     TEXT,
    refund_to          TEXT,
    refund_brix        TEXT,
    pending_phase      TEXT,
    pending_lls        INTEGER,
    seller_mirrored    INTEGER NOT NULL DEFAULT 0,
    buyer_mirrored     INTEGER NOT NULL DEFAULT 0,
    attempts           INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    created_ts         INTEGER NOT NULL,
    updated_ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_closet_fills_state ON closet_fills (state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_fill_payment ON closet_fills (payment_tx_hash)
    WHERE payment_tx_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_one_fill_per_bid ON closet_fills (bid_order_id)
    WHERE bid_order_id IS NOT NULL AND state NOT IN ('refunded', 'failed');
"""

_ORDER_MUTABLE = frozenset(
    {
        "state", "payload_uuid", "xumm_url", "qr_url", "push", "signed_txid",
        "escrow_tx_hash", "escrow_owner_seq", "cancel_reason", "cancel_finish_hash",
        "cancel_refund_hash", "pending_phase", "pending_lls", "error",
    }
)
_FILL_MUTABLE = frozenset(
    {
        "state", "payload_uuid", "xumm_url", "qr_url", "push", "signed_txid",
        "payment_tx_hash", "escrow_finish_hash", "forward_tx_hash", "overshoot_tx_hash",
        "refund_tx_hash", "refund_to", "refund_brix", "pending_phase", "pending_lls",
        "seller_mirrored", "buyer_mirrored", "attempts", "error",
    }
)


class OrderError(ValueError):
    """A refused order operation. `code` is the API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _now() -> int:
    return int(time.time())


def new_id() -> str:
    return uuid.uuid4().hex


def fmt_brix(d: Decimal) -> str:
    q = d.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def fee_for(price_brix: str, bps: int) -> str:
    return fmt_brix(Decimal(price_brix) * bps / Decimal(10000))


def invoice_id(fill_id: str) -> str:
    """Payment.InvoiceID identifying a buyer's payment for one fill."""
    return hashlib.sha256(fill_id.encode()).hexdigest().upper()


def memo_tag(phase: str, ident: str) -> str:
    if phase not in _TAG_PHASES:
        raise ValueError(f"unknown closet market memo phase: {phase!r}")
    return f"lfg:closet_{phase}:{ident}"


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """One serialized write transaction (BEGIN IMMEDIATE takes the write lock
    up front, so a check-then-write can't interleave with another writer)."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row, strict=True))


def _many(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def get_order(conn: sqlite3.Connection, order_id: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM closet_orders WHERE id = ?", (order_id,))


def get_fill(conn: sqlite3.Connection, fill_id: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM closet_fills WHERE id = ?", (fill_id,))


def _update(table: str, allowed: frozenset[str], states: frozenset[str], conn: sqlite3.Connection,
            ident: str, fields: dict[str, Any]) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"{table}: fields not updatable: {sorted(bad)}")
    if "state" in fields and fields["state"] not in states:
        raise ValueError(f"{table}: unknown state {fields['state']!r}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE {table} SET {sets}, updated_ts = ? WHERE id = ?",  # noqa: S608 - keys allowlisted
        (*fields.values(), _now(), ident),
    )
    conn.commit()


def update_order(conn: sqlite3.Connection, order_id: str, **fields: Any) -> None:
    _update("closet_orders", _ORDER_MUTABLE, ORDER_STATES, conn, order_id, fields)


def update_fill(conn: sqlite3.Connection, fill_id: str, **fields: Any) -> None:
    _update("closet_fills", _FILL_MUTABLE, FILL_STATES, conn, fill_id, fields)


def closet_active(conn: sqlite3.Connection, owner: str) -> bool:
    row = conn.execute("SELECT status FROM closet_tokens WHERE owner = ?", (owner,)).fetchone()
    return row is not None and row[0] == _ACTIVE


def holding_count(conn: sqlite3.Connection, owner: str, slot: str, value: str) -> int:
    row = conn.execute(
        "SELECT count FROM closet_assets WHERE owner = ? AND slot = ? AND value = ?",
        (owner, slot, value),
    ).fetchone()
    return int(row[0]) if row else 0


def encumbrance(conn: sqlite3.Connection, owner: str) -> dict[tuple[str, str], int]:
    """Units of each key promised to the market: open/matched asks, plus
    holder fills of a bid (no ask row) that haven't moved the asset yet."""
    out: dict[tuple[str, str], int] = {}
    for slot, value, n in conn.execute(
        "SELECT slot, value, COUNT(*) FROM closet_orders WHERE owner = ? AND side = 'ask' "
        "AND state IN ('open', 'matched') GROUP BY slot, value",
        (owner,),
    ):
        out[(slot, value)] = int(n)
    for slot, value, n in conn.execute(
        "SELECT slot, value, COUNT(*) FROM closet_fills WHERE seller = ? AND ask_order_id IS NULL "
        "AND (state IN ('funds_pending', 'funded') OR (state = 'indeterminate' AND pending_phase = 'finish')) "
        "GROUP BY slot, value",
        (owner,),
    ):
        out[(slot, value)] = out.get((slot, value), 0) + int(n)
    return out


def available_count(conn: sqlite3.Connection, owner: str, slot: str, value: str) -> int:
    return holding_count(conn, owner, slot, value) - encumbrance(conn, owner).get((slot, value), 0)


def create_ask(conn: sqlite3.Connection, *, owner: str, slot: str, value: str, price_brix: str,
               platform: str | None, now: int | None = None) -> dict[str, Any]:
    ts = now if now is not None else _now()
    oid = new_id()
    with _immediate(conn):
        if not closet_active(conn, owner):
            raise OrderError("closet_required", "Create and claim your Closet first.")
        if available_count(conn, owner, slot, value) < 1:
            raise OrderError("not_available", f"no unlisted '{value}' ({slot}) in your Closet")
        conn.execute(
            "INSERT INTO closet_orders (id, side, owner, slot, value, price_brix, state, created_ts, "
            "updated_ts, platform) VALUES (?, 'ask', ?, ?, ?, ?, 'open', ?, ?, ?)",
            (oid, owner, slot, value, price_brix, ts, ts, platform),
        )
    order = get_order(conn, oid)
    assert order is not None
    return order


def cancel_ask(conn: sqlite3.Connection, order_id: str, owner: str) -> dict[str, Any]:
    with _immediate(conn):
        order = get_order(conn, order_id)
        if order is None or order["side"] != SIDE_ASK or order["owner"] != owner:
            raise OrderError("not_found", "ask not found")
        if order["state"] != OPEN:
            raise OrderError("not_open", f"this ask is {order['state']}")
        conn.execute(
            "UPDATE closet_orders SET state = 'cancelled', updated_ts = ? WHERE id = ?",
            (_now(), order_id),
        )
    cancelled = get_order(conn, order_id)
    assert cancelled is not None
    return cancelled


def book_summary(conn: sqlite3.Connection, slot: str | None = None,
                 value: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT side, slot, value, price_brix FROM closet_orders WHERE state = 'open'"
    params: list[Any] = []
    if slot is not None:
        sql += " AND slot = ?"
        params.append(slot)
    if value is not None:
        sql += " AND value = ?"
        params.append(value)
    keys: dict[tuple[str, str], dict[str, Any]] = {}
    for side, s, v, price in conn.execute(sql, params):
        row = keys.setdefault(
            (s, v),
            {"slot": s, "value": v, "best_ask_brix": None, "ask_count": 0,
             "best_bid_brix": None, "bid_count": 0},
        )
        if side == SIDE_ASK:
            row["ask_count"] += 1
            if row["best_ask_brix"] is None or Decimal(price) < Decimal(row["best_ask_brix"]):
                row["best_ask_brix"] = price
        else:
            row["bid_count"] += 1
            if row["best_bid_brix"] is None or Decimal(price) > Decimal(row["best_bid_brix"]):
                row["best_bid_brix"] = price
    return sorted(keys.values(), key=lambda r: (r["slot"], r["value"]))


def book_levels(conn: sqlite3.Connection, slot: str, value: str) -> dict[str, Any]:
    rows = _many(
        conn,
        "SELECT id, side, owner, price_brix, created_ts FROM closet_orders "
        "WHERE state = 'open' AND slot = ? AND value = ?",
        (slot, value),
    )
    asks = sorted((r for r in rows if r["side"] == SIDE_ASK),
                  key=lambda r: (Decimal(r["price_brix"]), r["created_ts"], r["id"]))
    bids = sorted((r for r in rows if r["side"] == SIDE_BID),
                  key=lambda r: (-Decimal(r["price_brix"]), r["created_ts"], r["id"]))

    def pub(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "owner": r["owner"], "price_brix": r["price_brix"]}

    return {"slot": slot, "value": value, "asks": [pub(r) for r in asks], "bids": [pub(r) for r in bids]}
```

- [ ] **Step 4: Ensure the schema from `init_economy_schema`**

In `lfg_core/economy_store.py`, add `from lfg_core import closet_market_store` to the imports. Then add as the last line of `init_economy_schema`:

```python
    closet_market_store.ensure_schema(conn)  # #443: orders/fills share this DB
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_store_asks.py tests/test_economy_store.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add lfg_core/closet_market_store.py lfg_core/economy_store.py tests/test_closet_market_store_asks.py
git commit -m "feat(closet-market): order store schema, asks, encumbrance, book (#443)"
```

---

### Task 4: Store II — bids, fills, auto-cross, asset move, sweep queries

**Files:**
- Modify: `lfg_core/closet_market_store.py`
- Test: `tests/test_closet_market_store_fills.py`

**Interfaces:**
- Consumes: Task 3's helpers
- Produces:
  - Bid lifecycle:
    - `has_live_bid(conn, owner, slot, value) -> bool`
    - `create_pending_bid(conn, *, owner, slot, value, price_brix, platform, condition, fulfillment_enc, cancel_after, payload_uuid, xumm_url, qr_url, push, now=None) -> dict`
    - `mark_bid_open(conn, order_id, *, escrow_tx_hash, escrow_owner_seq) -> dict`
    - `begin_cancel_bid(conn, order_id, *, owner: str | None, reason: str) -> dict`
  - Matching and fills:
    - `cross_incoming(conn, order_id, *, fee_bps, now=None) -> dict | None` (the created fill)
    - `fill_bid(conn, bid_id, filler, *, fee_bps, platform, now=None) -> dict`
    - `create_take_fill(conn, ask_id, buyer, *, fee_bps, platform, now=None) -> dict`
    - `claim_ask_for_payment(conn, fill_id, payment_tx_hash) -> str`
    - `fill_blocker(conn, fill_id) -> str | None`
    - `abort_unfunded_fill(conn, fill_id, reason, *, bid_state=None) -> None`
    - `move_asset(conn, fill_id, *, now=None) -> str`
    - `fill_in_amount(fill: dict) -> str`
    - `mark_side_mirrored(conn, fill_id, side: str) -> None`
  - Owner views:
    - `has_unmirrored_fill(conn, owner) -> bool`
    - `open_orders_for_meta(conn, owner) -> list[dict] | None`
    - `orders_for_owner(conn, owner, fills_limit=20) -> dict`
    - `bids_on_holdings(conn, owner) -> list[dict]`
  - Sweep queries: `orders_needing_attention(conn) -> list[str]`, `fills_needing_attention(conn) -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_store_fills.py
import sqlite3

import pytest

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es

SELLER, BUYER, OTHER = "rSeller", "rBuyer", "rOther"


def _conn(seller_count=1):
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER, OTHER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", seller_count)], [])
    return c


def _open_bid(c, owner=BUYER, price="10", now=100):
    bid = cms.create_pending_bid(
        c, owner=owner, slot="Head", value="Crown", price_brix=price, platform="web",
        condition="A0", fulfillment_enc="sealed", cancel_after=999999, payload_uuid="U",
        xumm_url="x", qr_url="q", push=None, now=now,
    )
    return cms.mark_bid_open(c, bid["id"], escrow_tx_hash=f"H{bid['id']}", escrow_owner_seq=5)


def test_one_live_bid_per_key():
    c = _conn()
    _open_bid(c)
    assert cms.has_live_bid(c, BUYER, "Head", "Crown")
    with pytest.raises(cms.OrderError) as e:
        _open_bid(c)
    assert e.value.code == "bid_exists"


def test_incoming_ask_crosses_resting_bid_at_bid_price():
    c = _conn()
    bid = _open_bid(c, price="10")
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    fill = cms.cross_incoming(c, ask["id"], fee_bps=700)
    assert fill["price_brix"] == "10" and fill["overshoot_brix"] == "0"
    assert fill["fee_brix"] == "0.7" and fill["funds_source"] == cms.FUNDS_ESCROW
    assert (fill["seller"], fill["buyer"], fill["state"]) == (SELLER, BUYER, cms.FUNDS_PENDING)
    assert cms.get_order(c, bid["id"])["state"] == cms.MATCHED
    assert cms.get_order(c, ask["id"])["state"] == cms.MATCHED


def test_incoming_bid_crosses_resting_ask_at_ask_price_with_overshoot():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    bid = _open_bid(c, price="10")
    fill = cms.cross_incoming(c, bid["id"], fee_bps=0)
    assert (fill["price_brix"], fill["overshoot_brix"], fill["fee_brix"]) == ("8", "2", "0")
    assert cms.fill_in_amount(fill) == "10"


def test_best_counter_is_best_price_then_fifo_and_never_self():
    c = _conn(seller_count=3)
    es.set_closet_contents(c, BUYER, [("Head", "Crown", 1)], [])
    cms.create_ask(c, owner=BUYER, slot="Head", value="Crown", price_brix="1", platform=None, now=1)
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="7", platform=None, now=2)
    first7 = cms.book_levels(c, "Head", "Crown")["asks"][1]["id"]
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="7", platform=None, now=3)
    bid = _open_bid(c, owner=BUYER, price="9")
    fill = cms.cross_incoming(c, bid["id"], fee_bps=0)
    assert fill["ask_order_id"] == first7  # own ask at 1 skipped; FIFO among the 7s


def test_no_cross_when_prices_do_not_meet():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="11", platform=None)
    bid = _open_bid(c, price="10")
    assert cms.cross_incoming(c, bid["id"], fee_bps=0) is None


def test_holder_fill_encumbers_and_refuses_self_and_unheld():
    c = _conn()
    bid = _open_bid(c)
    with pytest.raises(cms.OrderError) as e:
        cms.fill_bid(c, bid["id"], BUYER, fee_bps=0, platform=None)
    assert e.value.code == "self_cross"
    with pytest.raises(cms.OrderError) as e:
        cms.fill_bid(c, bid["id"], OTHER, fee_bps=0, platform=None)
    assert e.value.code == "not_available"
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    assert fill["ask_order_id"] is None
    assert cms.available_count(c, SELLER, "Head", "Crown") == 0  # encumbered by the fill


def test_move_asset_is_atomic_and_closes_orders():
    c = _conn()
    bid = _open_bid(c)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    cms.update_fill(c, fill["id"], state=cms.FUNDED, escrow_finish_hash="F")
    assert cms.move_asset(c, fill["id"]) == cms.ASSET_MOVED
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0
    assert cms.holding_count(c, BUYER, "Head", "Crown") == 1
    assert c.execute("SELECT COUNT(*) FROM closet_assets WHERE owner=?", (SELLER,)).fetchone()[0] == 0
    assert cms.get_order(c, bid["id"])["state"] == cms.FILLED
    assert cms.has_unmirrored_fill(c, SELLER) and cms.has_unmirrored_fill(c, BUYER)
    cms.mark_side_mirrored(c, fill["id"], "seller")
    assert not cms.has_unmirrored_fill(c, SELLER) and cms.has_unmirrored_fill(c, BUYER)
    cms.update_fill(c, fill["id"], state=cms.PAID)
    cms.mark_side_mirrored(c, fill["id"], "buyer")
    assert cms.get_fill(c, fill["id"])["state"] == cms.MIRRORED


def test_move_asset_refunds_when_seller_no_longer_holds():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    fill = cms.create_take_fill(c, ask["id"], BUYER, fee_bps=0, platform=None)
    assert cms.claim_ask_for_payment(c, fill["id"], "PAY1") == cms.FUNDED
    es.set_closet_contents(c, SELLER, [], [])  # the unit vanished (e.g. listener rebuild)
    assert cms.move_asset(c, fill["id"]) == cms.REFUND_PENDING
    f = cms.get_fill(c, fill["id"])
    assert (f["refund_to"], f["refund_brix"]) == (BUYER, "5")
    assert cms.get_order(c, ask["id"])["state"] == cms.CANCELLED


def test_second_payment_for_same_ask_is_refunded():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    f1 = cms.create_take_fill(c, ask["id"], BUYER, fee_bps=0, platform=None)
    f2 = cms.create_take_fill(c, ask["id"], OTHER, fee_bps=0, platform=None)
    assert cms.claim_ask_for_payment(c, f1["id"], "P1") == cms.FUNDED
    assert cms.claim_ask_for_payment(c, f2["id"], "P2") == cms.REFUND_PENDING
    with pytest.raises(sqlite3.IntegrityError):  # a payment hash can fund one fill only
        cms.update_fill(c, f2["id"], payment_tx_hash="P1")


def test_abort_unfunded_fill_reopens_bid_and_cancels_short_ask():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    bid = _open_bid(c, price="5")
    fill = cms.cross_incoming(c, bid["id"], fee_bps=0)
    es.set_closet_contents(c, SELLER, [], [])
    assert cms.fill_blocker(c, fill["id"]) == "the seller no longer holds the trait"
    cms.abort_unfunded_fill(c, fill["id"], "the seller no longer holds the trait")
    assert cms.get_fill(c, fill["id"])["state"] == cms.FAILED
    assert cms.get_order(c, bid["id"])["state"] == cms.OPEN
    assert cms.get_order(c, fill["ask_order_id"])["state"] == cms.CANCELLED


def test_begin_cancel_only_from_open():
    c = _conn()
    bid = _open_bid(c)
    got = cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    assert (got["state"], got["cancel_reason"]) == (cms.CANCELLING, "user")
    with pytest.raises(cms.OrderError):
        cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")


def test_bids_on_holdings_prefers_closet_then_token():
    c = _conn()
    es.upsert_trait_token(c, "TOK1", OTHER, "Head", "Crown")
    bid = _open_bid(c)
    [mine] = cms.bids_on_holdings(c, SELLER)
    assert (mine["id"], mine["source"], mine["nft_id"]) == (bid["id"], "closet", None)
    [theirs] = cms.bids_on_holdings(c, OTHER)
    assert (theirs["source"], theirs["nft_id"]) == ("token", "TOK1")
    assert cms.bids_on_holdings(c, BUYER) == []  # never your own bid


def test_meta_orders_and_sweep_queries():
    c = _conn(seller_count=2)  # one copy listed, one free to fill a bid with
    assert cms.open_orders_for_meta(c, SELLER) is None
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    assert cms.open_orders_for_meta(c, SELLER) == [
        {"side": "ask", "slot": "Head", "value": "Crown", "price_brix": "5"}
    ]
    bid = _open_bid(c, owner=OTHER, price="1")
    assert bid["id"] in cms.orders_needing_attention(c)
    assert cms.fills_needing_attention(c) == []
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    assert cms.fills_needing_attention(c) == [fill["id"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_store_fills.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.closet_market_store' has no attribute 'create_pending_bid'`

- [ ] **Step 3: Implement**

Append to `lfg_core/closet_market_store.py`:

```python
_LIVE_BID_STATES = (PENDING_ESCROW, OPEN, MATCHED, CANCELLING)


def has_live_bid(conn: sqlite3.Connection, owner: str, slot: str, value: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM closet_orders WHERE side = 'bid' AND owner = ? AND slot = ? AND value = ? "
        "AND state IN (?, ?, ?, ?) LIMIT 1",
        (owner, slot, value, *_LIVE_BID_STATES),
    ).fetchone()
    return row is not None


def create_pending_bid(conn: sqlite3.Connection, *, owner: str, slot: str, value: str,
                       price_brix: str, platform: str | None, condition: str,
                       fulfillment_enc: str, cancel_after: int, payload_uuid: str | None,
                       xumm_url: str | None, qr_url: str | None, push: str | None,
                       now: int | None = None) -> dict[str, Any]:
    ts = now if now is not None else _now()
    oid = new_id()
    try:
        with _immediate(conn):
            if not closet_active(conn, owner):
                raise OrderError("closet_required", "Create and claim your Closet first.")
            conn.execute(
                "INSERT INTO closet_orders (id, side, owner, slot, value, price_brix, state, created_ts, "
                "updated_ts, platform, payload_uuid, xumm_url, qr_url, push, condition, "
                "fulfillment_enc, cancel_after) VALUES (?, 'bid', ?, ?, ?, ?, 'pending_escrow', ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?)",
                (oid, owner, slot, value, price_brix, ts, ts, platform, payload_uuid, xumm_url,
                 qr_url, push, condition, fulfillment_enc, cancel_after),
            )
    except sqlite3.IntegrityError as exc:
        raise OrderError(
            "bid_exists", "you already have a live bid on this trait — cancel it first"
        ) from exc
    order = get_order(conn, oid)
    assert order is not None
    return order


def mark_bid_open(conn: sqlite3.Connection, order_id: str, *, escrow_tx_hash: str,
                  escrow_owner_seq: int) -> dict[str, Any]:
    with _immediate(conn):
        order = get_order(conn, order_id)
        if order is None or order["side"] != SIDE_BID:
            raise OrderError("not_found", "bid not found")
        if order["state"] != PENDING_ESCROW:
            raise OrderError("not_open", f"this bid is {order['state']}")
        conn.execute(
            "UPDATE closet_orders SET state = 'open', escrow_tx_hash = ?, escrow_owner_seq = ?, "
            "updated_ts = ? WHERE id = ?",
            (escrow_tx_hash, escrow_owner_seq, _now(), order_id),
        )
    opened = get_order(conn, order_id)
    assert opened is not None
    return opened


def begin_cancel_bid(conn: sqlite3.Connection, order_id: str, *, owner: str | None,
                     reason: str) -> dict[str, Any]:
    with _immediate(conn):
        order = get_order(conn, order_id)
        if order is None or order["side"] != SIDE_BID or (owner is not None and order["owner"] != owner):
            raise OrderError("not_found", "bid not found")
        if order["state"] != OPEN:
            raise OrderError("not_open", f"only an open bid can be cancelled (this one is {order['state']})")
        conn.execute(
            "UPDATE closet_orders SET state = 'cancelling', cancel_reason = ?, updated_ts = ? WHERE id = ?",
            (reason, _now(), order_id),
        )
    got = get_order(conn, order_id)
    assert got is not None
    return got


def _insert_fill(conn: sqlite3.Connection, *, ask_order_id: str | None, bid_order_id: str | None,
                 funds_source: str, seller: str, buyer: str, slot: str, value: str,
                 price_brix: str, fee_brix: str, overshoot_brix: str, platform: str | None,
                 ts: int) -> str:
    fid = new_id()
    conn.execute(
        "INSERT INTO closet_fills (id, ask_order_id, bid_order_id, funds_source, seller, buyer, slot, "
        "value, price_brix, fee_brix, overshoot_brix, state, platform, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'funds_pending', ?, ?, ?)",
        (fid, ask_order_id, bid_order_id, funds_source, seller, buyer, slot, value, price_brix,
         fee_brix, overshoot_brix, platform, ts, ts),
    )
    return fid


def _best_counter(conn: sqlite3.Connection, order: dict[str, Any]) -> dict[str, Any] | None:
    other = SIDE_BID if order["side"] == SIDE_ASK else SIDE_ASK
    rows = _many(
        conn,
        "SELECT * FROM closet_orders WHERE state = 'open' AND side = ? AND slot = ? AND value = ? AND owner != ?",
        (other, order["slot"], order["value"], order["owner"]),
    )
    mine = Decimal(order["price_brix"])
    if order["side"] == SIDE_ASK:
        eligible = [r for r in rows if Decimal(r["price_brix"]) >= mine]
        eligible.sort(key=lambda r: (-Decimal(r["price_brix"]), r["created_ts"], r["id"]))
    else:
        eligible = [r for r in rows if Decimal(r["price_brix"]) <= mine]
        eligible.sort(key=lambda r: (Decimal(r["price_brix"]), r["created_ts"], r["id"]))
    return eligible[0] if eligible else None


def cross_incoming(conn: sqlite3.Connection, order_id: str, *, fee_bps: int,
                   now: int | None = None) -> dict[str, Any] | None:
    """Auto-cross a just-opened order against the best resting counter-order.
    The RESTING order's price is the fill price; a bid above it records the
    difference as overshoot (refunded at settlement). Both orders -> matched
    and an escrow-funded fill is created, in one transaction."""
    ts = now if now is not None else _now()
    fid: str | None = None
    with _immediate(conn):
        incoming = get_order(conn, order_id)
        if incoming is None or incoming["state"] != OPEN:
            return None
        resting = _best_counter(conn, incoming)
        if resting is None:
            return None
        ask, bid = (incoming, resting) if incoming["side"] == SIDE_ASK else (resting, incoming)
        price = resting["price_brix"]
        conn.execute(
            "UPDATE closet_orders SET state = 'matched', updated_ts = ? WHERE id IN (?, ?)",
            (ts, ask["id"], bid["id"]),
        )
        fid = _insert_fill(
            conn, ask_order_id=ask["id"], bid_order_id=bid["id"], funds_source=FUNDS_ESCROW,
            seller=ask["owner"], buyer=bid["owner"], slot=ask["slot"], value=ask["value"],
            price_brix=price, fee_brix=fee_for(price, fee_bps),
            overshoot_brix=fmt_brix(Decimal(bid["price_brix"]) - Decimal(price)),
            platform=incoming["platform"], ts=ts,
        )
    return get_fill(conn, fid)


def fill_bid(conn: sqlite3.Connection, bid_id: str, filler: str, *, fee_bps: int,
             platform: str | None, now: int | None = None) -> dict[str, Any]:
    ts = now if now is not None else _now()
    with _immediate(conn):
        bid = get_order(conn, bid_id)
        if bid is None or bid["side"] != SIDE_BID:
            raise OrderError("not_found", "bid not found")
        if bid["state"] != OPEN:
            raise OrderError("not_open", "this bid is no longer open")
        if bid["owner"] == filler:
            raise OrderError("self_cross", "you can't fill your own bid")
        if not closet_active(conn, filler):
            raise OrderError("closet_required", "Create and claim your Closet first.")
        if available_count(conn, filler, bid["slot"], bid["value"]) < 1:
            raise OrderError("not_available", f"no unlisted '{bid['value']}' ({bid['slot']}) in your Closet")
        conn.execute("UPDATE closet_orders SET state = 'matched', updated_ts = ? WHERE id = ?", (ts, bid_id))
        fid = _insert_fill(
            conn, ask_order_id=None, bid_order_id=bid_id, funds_source=FUNDS_ESCROW, seller=filler,
            buyer=bid["owner"], slot=bid["slot"], value=bid["value"], price_brix=bid["price_brix"],
            fee_brix=fee_for(bid["price_brix"], fee_bps), overshoot_brix="0", platform=platform, ts=ts,
        )
    fill = get_fill(conn, fid)
    assert fill is not None
    return fill


def create_take_fill(conn: sqlite3.Connection, ask_id: str, buyer: str, *, fee_bps: int,
                     platform: str | None, now: int | None = None) -> dict[str, Any]:
    """A buyer starts paying for an ask. The ask is NOT reserved: the first
    validated payment wins (claim_ask_for_payment), later ones are refunded."""
    ts = now if now is not None else _now()
    with _immediate(conn):
        ask = get_order(conn, ask_id)
        if ask is None or ask["side"] != SIDE_ASK:
            raise OrderError("not_found", "ask not found")
        if ask["state"] != OPEN:
            raise OrderError("not_open", "this trait was just sold or unlisted")
        if ask["owner"] == buyer:
            raise OrderError("self_cross", "you can't buy your own listing")
        if not closet_active(conn, buyer):
            raise OrderError("closet_required", "Create and claim your Closet first.")
        fid = _insert_fill(
            conn, ask_order_id=ask_id, bid_order_id=None, funds_source=FUNDS_PAYMENT, seller=ask["owner"],
            buyer=buyer, slot=ask["slot"], value=ask["value"], price_brix=ask["price_brix"],
            fee_brix=fee_for(ask["price_brix"], fee_bps), overshoot_brix="0", platform=platform, ts=ts,
        )
    fill = get_fill(conn, fid)
    assert fill is not None
    return fill


def claim_ask_for_payment(conn: sqlite3.Connection, fill_id: str, payment_tx_hash: str) -> str:
    """Funds for a take landed. FUNDED if the ask was still open (ask ->
    matched), else REFUND_PENDING. Raises sqlite3.IntegrityError if this
    payment hash already funds another fill."""
    with _immediate(conn):
        fill = get_fill(conn, fill_id)
        assert fill is not None
        if fill["state"] != FUNDS_PENDING:
            return str(fill["state"])
        ask = get_order(conn, fill["ask_order_id"])
        ts = _now()
        if ask is not None and ask["state"] == OPEN and ask["owner"] == fill["seller"]:
            conn.execute("UPDATE closet_orders SET state = 'matched', updated_ts = ? WHERE id = ?", (ts, ask["id"]))
            conn.execute(
                "UPDATE closet_fills SET state = 'funded', payment_tx_hash = ?, updated_ts = ? WHERE id = ?",
                (payment_tx_hash, ts, fill_id),
            )
            return FUNDED
        conn.execute(
            "UPDATE closet_fills SET state = 'refund_pending', payment_tx_hash = ?, refund_to = ?, "
            "refund_brix = ?, error = ?, updated_ts = ? WHERE id = ?",
            (payment_tx_hash, fill["buyer"], fill["price_brix"], "the listing was no longer available", ts, fill_id),
        )
        return REFUND_PENDING


def fill_in_amount(fill: dict[str, Any]) -> str:
    """BRIX the app wallet receives for this fill (escrow = the whole bid)."""
    if fill["funds_source"] == FUNDS_ESCROW:
        return fmt_brix(Decimal(fill["price_brix"]) + Decimal(fill["overshoot_brix"]))
    return str(fill["price_brix"])


def fill_blocker(conn: sqlite3.Connection, fill_id: str) -> str | None:
    fill = get_fill(conn, fill_id)
    assert fill is not None
    if not closet_active(conn, fill["buyer"]):
        return "the buyer has no active Closet"
    if holding_count(conn, fill["seller"], fill["slot"], fill["value"]) < 1:
        return "the seller no longer holds the trait"
    return None


def abort_unfunded_fill(conn: sqlite3.Connection, fill_id: str, reason: str, *,
                        bid_state: str | None = None) -> None:
    """A pre-funding fill can't proceed: nothing moved on-ledger. Release the
    orders — a short seller's ask is cancelled, a buyer without a Closet has
    their bid sent to cancelling (so its escrow is returned)."""
    with _immediate(conn):
        fill = get_fill(conn, fill_id)
        assert fill is not None
        ts = _now()
        conn.execute(
            "UPDATE closet_fills SET state = 'failed', error = ?, updated_ts = ? WHERE id = ?",
            (reason, ts, fill_id),
        )
        seller_short = holding_count(conn, fill["seller"], fill["slot"], fill["value"]) < 1
        if fill["ask_order_id"]:
            conn.execute(
                "UPDATE closet_orders SET state = ?, updated_ts = ? WHERE id = ? AND state = 'matched'",
                (CANCELLED if seller_short else OPEN, ts, fill["ask_order_id"]),
            )
        if fill["bid_order_id"]:
            if bid_state is not None:
                new_state, cancel_reason = bid_state, None
            elif not closet_active(conn, fill["buyer"]):
                new_state, cancel_reason = CANCELLING, "no_closet"
            else:
                new_state, cancel_reason = OPEN, None
            conn.execute(
                "UPDATE closet_orders SET state = ?, cancel_reason = COALESCE(?, cancel_reason), "
                "updated_ts = ? WHERE id = ? AND state = 'matched'",
                (new_state, cancel_reason, ts, fill["bid_order_id"]),
            )


def move_asset(conn: sqlite3.Connection, fill_id: str, *, now: int | None = None) -> str:
    """Settlement step 2 — ONE transaction: seller -1, buyer +1, both orders
    filled, fill asset_moved. If the buyer lost their Closet or the seller no
    longer holds the unit, nothing moves and the fill goes refund_pending.
    Callers MUST hold owner_lock for seller and buyer (economy flows
    full-overwrite closet_assets from a snapshot read under that lock)."""
    ts = now if now is not None else _now()
    with _immediate(conn):
        fill = get_fill(conn, fill_id)
        assert fill is not None
        if fill["state"] != FUNDED:
            return str(fill["state"])
        buyer_ok = closet_active(conn, fill["buyer"])
        seller_ok = holding_count(conn, fill["seller"], fill["slot"], fill["value"]) >= 1
        if not (buyer_ok and seller_ok):
            reason = "the buyer has no active Closet" if not buyer_ok else "the seller no longer holds the trait"
            conn.execute(
                "UPDATE closet_fills SET state = 'refund_pending', refund_to = ?, refund_brix = ?, error = ?, "
                "updated_ts = ? WHERE id = ?",
                (fill["buyer"], fill_in_amount(fill), reason, ts, fill_id),
            )
            if fill["ask_order_id"]:
                conn.execute(
                    "UPDATE closet_orders SET state = ?, updated_ts = ? WHERE id = ?",
                    (CANCELLED if not seller_ok else OPEN, ts, fill["ask_order_id"]),
                )
            if fill["bid_order_id"]:  # its escrow is already finished: the refund returns the BRIX
                conn.execute(
                    "UPDATE closet_orders SET state = 'cancelled', cancel_reason = 'undeliverable', "
                    "updated_ts = ? WHERE id = ?",
                    (ts, fill["bid_order_id"]),
                )
            return REFUND_PENDING
        key = (fill["slot"], fill["value"])
        conn.execute(
            "UPDATE closet_assets SET count = count - 1 WHERE owner = ? AND slot = ? AND value = ?",
            (fill["seller"], *key),
        )
        conn.execute(
            "DELETE FROM closet_assets WHERE owner = ? AND slot = ? AND value = ? AND count <= 0",
            (fill["seller"], *key),
        )
        conn.execute(
            "INSERT INTO closet_assets (owner, slot, value, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(owner, slot, value) DO UPDATE SET count = count + 1",
            (fill["buyer"], *key),
        )
        for oid in (fill["ask_order_id"], fill["bid_order_id"]):
            if oid:
                conn.execute("UPDATE closet_orders SET state = 'filled', updated_ts = ? WHERE id = ?", (ts, oid))
        conn.execute("UPDATE closet_fills SET state = 'asset_moved', updated_ts = ? WHERE id = ?", (ts, fill_id))
    return ASSET_MOVED


def mark_side_mirrored(conn: sqlite3.Connection, fill_id: str, side: str) -> None:
    if side not in ("seller", "buyer"):
        raise ValueError(side)
    with _immediate(conn):
        conn.execute(
            f"UPDATE closet_fills SET {side}_mirrored = 1, updated_ts = ? WHERE id = ?",  # noqa: S608
            (_now(), fill_id),
        )
        conn.execute(
            "UPDATE closet_fills SET state = 'mirrored' WHERE id = ? AND state = 'paid' "
            "AND seller_mirrored = 1 AND buyer_mirrored = 1",
            (fill_id,),
        )


_UNMIRRORED = (
    "(state IN ('asset_moved', 'paid') OR (state = 'indeterminate' AND pending_phase IN ('forward', 'overshoot')))"
)


def has_unmirrored_fill(conn: sqlite3.Connection, owner: str) -> bool:
    """True while this owner's DB contents are ahead of their Closet token.
    The listener/backfill must not rebuild closet_assets from token metadata
    while this holds (it would resurrect a moved unit)."""
    row = conn.execute(
        "SELECT 1 FROM closet_fills WHERE ((seller = ? AND seller_mirrored = 0) OR "
        f"(buyer = ? AND buyer_mirrored = 0)) AND {_UNMIRRORED} LIMIT 1",  # noqa: S608
        (owner, owner),
    ).fetchone()
    return row is not None


def open_orders_for_meta(conn: sqlite3.Connection, owner: str) -> list[dict[str, Any]] | None:
    rows = _many(
        conn,
        "SELECT side, slot, value, price_brix FROM closet_orders WHERE owner = ? "
        "AND state IN ('open', 'matched') ORDER BY created_ts, id",
        (owner,),
    )
    return rows or None


def orders_for_owner(conn: sqlite3.Connection, owner: str, fills_limit: int = 20) -> dict[str, Any]:
    orders = _many(
        conn,
        "SELECT id, side, slot, value, price_brix, state, created_ts, cancel_after, error FROM closet_orders "
        "WHERE owner = ? AND state IN ('pending_escrow', 'open', 'matched', 'cancelling') ORDER BY created_ts DESC",
        (owner,),
    )
    fills = _many(
        conn,
        "SELECT id, seller, buyer, slot, value, price_brix, fee_brix, overshoot_brix, state, error, created_ts "
        "FROM closet_fills WHERE seller = ? OR buyer = ? ORDER BY created_ts DESC LIMIT ?",
        (owner, owner, fills_limit),
    )
    return {"orders": orders, "fills": fills}


def bids_on_holdings(conn: sqlite3.Connection, owner: str) -> list[dict[str, Any]]:
    """#496: open bids someone else placed on a key this wallet can deliver —
    from a loose Closet copy (fill directly) or else an extracted trait token
    (deposit first)."""
    bids = _many(
        conn,
        "SELECT id, owner, slot, value, price_brix, cancel_after, created_ts FROM closet_orders "
        "WHERE side = 'bid' AND state = 'open' AND owner != ?",
        (owner,),
    )
    bids.sort(key=lambda r: (-Decimal(r["price_brix"]), r["created_ts"], r["id"]))
    tokens: dict[tuple[str, str], str] = {}
    for nft_id, slot, value in conn.execute(
        "SELECT nft_id, slot, value FROM trait_tokens WHERE owner = ? ORDER BY nft_id", (owner,)
    ):
        tokens.setdefault((slot, value), nft_id)
    enc = encumbrance(conn, owner)
    out: list[dict[str, Any]] = []
    for b in bids:
        key = (b["slot"], b["value"])
        pub = {k: b[k] for k in ("id", "slot", "value", "price_brix", "cancel_after")}
        if holding_count(conn, owner, *key) - enc.get(key, 0) >= 1:
            out.append({**pub, "source": "closet", "nft_id": None})
        elif key in tokens:
            out.append({**pub, "source": "token", "nft_id": tokens[key]})
    return out


def orders_needing_attention(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM closet_orders WHERE side = 'bid' AND state IN ('pending_escrow', 'open', 'cancelling') "
            "ORDER BY updated_ts"
        )
    ]


def fills_needing_attention(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM closet_fills WHERE state NOT IN ('mirrored', 'refunded', 'failed') ORDER BY updated_ts"
        )
    ]
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_store_asks.py tests/test_closet_market_store_fills.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lfg_core/closet_market_store.py tests/test_closet_market_store_fills.py
git commit -m "feat(closet-market): bids, fills, auto-cross, atomic asset move (#443)"
```

---

### Task 5: Encumbrance gates in Equip, Assemble, Extract

**Files:**
- Modify: `lfg_core/economy_flow.py`:
  - imports
  - `run_assemble` (~line 727)
  - `run_equip` (~line 1079)
  - `run_extract` (~line 1275)
- Modify: `webapp/economy_api.py` `start_equip` (~line 352)
- Test: `tests/test_closet_market_encumbrance.py`

**Interfaces:**
- Consumes: `closet_market_store.encumbrance(conn, owner)`, `create_ask`
- Produces: `economy_flow._listed_error(conn, owner, need: Mapping[tuple[str, str], int], assets: dict[tuple[str, str], int]) -> str | None`

The trait-sell wizard (`TraitSellSession`) runs Extract, so the Extract gate covers it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_encumbrance.py
from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from tests.test_economy_flow_equip import _char, _conn_with_assets, _deps, _Fakes, _run
from tests.test_economy_flow_extract import _F
from tests.test_economy_flow_extract import _deps as _extract_deps


def _list(conn, owner, slot, value):
    es.set_closet_status(conn, owner, ct.ACTIVE)
    return cms.create_ask(conn, owner=owner, slot=slot, value=value, price_brix="5", platform=None)


def test_listed_error_counts_encumbrance(tmp_path):
    conn = _conn_with_assets([("Head", "Crown", 1)])
    _list(conn, "rUser", "Head", "Crown")
    assert "listed" in ef._listed_error(conn, "rUser", {("Head", "Crown"): 1}, {("Head", "Crown"): 1})
    assert ef._listed_error(conn, "rUser", {("Head", "Crown"): 1}, {("Head", "Crown"): 2}) is None


def test_equip_refuses_the_only_listed_copy(tmp_path):
    conn, f = _conn_with_assets([("Head", "Crown", 1)]), _Fakes()
    _list(conn, "rUser", "Head", "Crown")
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED and "listed" in s.error
    assert f.char_modifies == []


def test_equip_allows_an_unlisted_second_copy(tmp_path):
    conn, f = _conn_with_assets([("Head", "Crown", 2)]), _Fakes()
    _list(conn, "rUser", "Head", "Crown")
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.DONE


def test_extract_refuses_the_only_listed_copy(tmp_path):
    conn = _conn_with_assets([("Hat", "Cap", 1)])
    es.set_closet_token(conn, "rUser", "CLOSET", "AB", status=ct.ACTIVE, offer_id=None)
    _list(conn, "rUser", "Hat", "Cap")
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _extract_deps(conn, _F(), tmp_path)))
    assert s.state == ef.FAILED and "listed" in s.error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_encumbrance.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.economy_flow' has no attribute '_listed_error'`

- [ ] **Step 3: Implement the helper**

In `lfg_core/economy_flow.py`, add `from collections.abc import Mapping` and `from lfg_core import closet_market_store as cms` to the imports. Add below `_owner_contents`:

```python
def _listed_error(
    conn: Any,
    owner: str,
    need: Mapping[tuple[str, str], int],
    assets: dict[tuple[str, str], int],
) -> str | None:
    """#443: a Closet Market ask (or an in-flight holder fill) encumbers a
    unit without decrementing its count. Refuse to consume a unit the owner
    has promised to a buyer."""
    enc = cms.encumbrance(conn, owner)
    for (slot, value), qty in need.items():
        if assets.get((slot, value), 0) - enc.get((slot, value), 0) < qty:
            return f"'{value}' ({slot}) is listed in your Closet market — cancel that order first"
    return None
```

- [ ] **Step 4: Wire the three flows**

`run_equip`: directly after `assets = _owner_contents(conn, owner)`:

```python
        listed = _listed_error(conn, owner, Counter(session.changes), assets)
        if listed:
            session.resolution = "reverted"
            session.fail(f"cannot equip: {listed}")
            return
```

`run_assemble`: directly after the `if not chk.ok:` block:

```python
        need = Counter((s, session.chosen[s]) for s in te.NON_BODY_SLOTS)
        need[("Body", session.body_value)] += 1
        listed = _listed_error(conn, owner, need, assets)
        if listed:
            session.fail(f"cannot assemble: {listed}")
            return
```

`run_extract`: directly after the `no loose '{value}' {slot}` check:

```python
        listed = _listed_error(conn, owner, {(slot, value): 1}, assets)
        if listed:
            session.fail(listed)
            return
```

(`Counter` is already imported in `economy_flow.py` if `can_assemble` lives in `trait_economy`. If not, add `from collections import Counter`.)

`webapp/economy_api.py` `start_equip`: after the `assets = {...}` dict comprehension:

```python
        listed = economy_flow._listed_error(conn, owner, Counter(changes), assets)
        if listed:
            raise EconomyError(f"cannot equip: {listed}")
```

(add `from collections import Counter` if missing).

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_encumbrance.py tests/test_economy_flow_equip.py tests/test_economy_flow_assemble.py tests/test_economy_flow_extract.py webapp/test_economy_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add lfg_core/economy_flow.py webapp/economy_api.py tests/test_closet_market_encumbrance.py
git commit -m "feat(closet-market): listed traits can't be equipped, assembled or extracted (#443)"
```

---

### Task 6: Closet metadata `orders` block + unmirrored-fill rebuild guard

**Files:**
- Modify: `lfg_core/closet_token.py`:
  - `build_closet_metadata` (~line 71)
  - `sync_closet` (~line 266)
- Modify: `lfg_core/nft_listener.py` `_apply_closet` (~line 183)
- Modify: `scripts/backfill_economy.py` `_reconcile_closet` (~line 101)
- Test: `tests/test_closet_market_mirror_guard.py`

**Interfaces:**
- Consumes: `closet_market_store.open_orders_for_meta`, `has_unmirrored_fill`
- Produces: `closet_token.build_closet_metadata(owner, assets, bodies, orders: list[dict] | None = None) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_mirror_guard.py
import asyncio
import importlib.util
import pathlib
import sqlite3

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core import nft_listener

SELLER, BUYER = "rSeller", "rBuyer"


def _moved_fill_conn():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 1)], [])
    bid = cms.create_pending_bid(
        c, owner=BUYER, slot="Head", value="Crown", price_brix="3", platform=None, condition="A0",
        fulfillment_enc="s", cancel_after=9, payload_uuid=None, xumm_url=None, qr_url=None, push=None,
    )
    cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E", escrow_owner_seq=1)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    cms.update_fill(c, fill["id"], state=cms.FUNDED)
    assert cms.move_asset(c, fill["id"]) == cms.ASSET_MOVED
    return c


def _stale_seller_meta():  # the seller's Closet metadata from BEFORE the fill
    return ct.build_closet_metadata(SELLER, [("Head", "Crown", 1)], [])


def test_metadata_orders_block_is_optional_and_ignored_by_parse():
    meta = ct.build_closet_metadata("rX", [("Head", "Crown", 1)], [], orders=[{"side": "ask"}])
    assert meta["lfg_closet"]["orders"] == [{"side": "ask"}]
    assert "orders" not in ct.build_closet_metadata("rX", [], [])["lfg_closet"]
    assert ct.parse_closet_metadata(meta) == ([("Head", "Crown", 1)], [])


def test_sync_closet_writes_open_orders():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    es.set_closet_token(c, SELLER, "C1", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 1)], [])
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="4", platform=None)
    uploaded = []

    async def upload(meta):
        uploaded.append(meta)
        return "https://cdn/x.json"

    async def modify(nft_id, owner, url):
        return "MODHASH"

    asyncio.new_event_loop().run_until_complete(
        ct.sync_closet(c, SELLER, [("Head", "Crown", 1)], [], upload_fn=upload, modify_fn=modify)
    )
    assert uploaded[0]["lfg_closet"]["orders"] == [
        {"side": "ask", "slot": "Head", "value": "Crown", "price_brix": "4"}
    ]


def test_listener_does_not_resurrect_a_moved_unit():
    c = _moved_fill_conn()
    nft_listener._apply_closet(c, {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"}, _stale_seller_meta())
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0


def test_backfill_does_not_resurrect_a_moved_unit():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_economy.py"
    spec = importlib.util.spec_from_file_location("backfill_economy_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = _moved_fill_conn()
    mod._reconcile_closet(c, {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"}, _stale_seller_meta(), "rIssuer")
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0


def test_listener_rebuilds_normally_once_mirrored():
    c = _moved_fill_conn()
    fill_id = c.execute("SELECT id FROM closet_fills").fetchone()[0]
    cms.update_fill(c, fill_id, state=cms.PAID)
    cms.mark_side_mirrored(c, fill_id, "seller")
    cms.mark_side_mirrored(c, fill_id, "buyer")
    nft_listener._apply_closet(c, {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"},
                               ct.build_closet_metadata(SELLER, [("Head", "Tiara", 1)], []))
    assert cms.holding_count(c, SELLER, "Head", "Tiara") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_mirror_guard.py -v`
Expected: FAIL with `TypeError: build_closet_metadata() got an unexpected keyword argument 'orders'`

- [ ] **Step 3: Implement metadata `orders`**

In `lfg_core/closet_token.py`, change the signature to `def build_closet_metadata(owner: str, assets: list[Asset], bodies: list[int], orders: list[dict[str, Any]] | None = None) -> dict[str, Any]:`. Build the dict into a local `meta`, then:

```python
    if orders:
        # #443: open Closet Market orders, display-only. parse_closet_metadata
        # ignores this key; closet_orders in the DB stays authoritative.
        meta["lfg_closet"]["orders"] = orders
    return meta
```

In `sync_closet`, replace `url = await upload_fn(build_closet_metadata(owner, assets, bodies))` with:

```python
    url = await upload_fn(build_closet_metadata(owner, assets, bodies, _orders_for_meta(conn, owner)))
```

and add the module-level helper:

```python
def _orders_for_meta(conn: Any, owner: str) -> list[dict[str, Any]] | None:
    """Best-effort: the orders block is display-only, so a read failure (a
    test double conn, a DB without the table) must never block a Closet sync."""
    from lfg_core import closet_market_store  # local: closet_market_store must stay import-light

    try:
        return closet_market_store.open_orders_for_meta(conn, owner)
    except (sqlite3.Error, AttributeError, TypeError):
        logging.warning(f"closet orders unreadable for {owner}; syncing without an orders block")
        return None
```

(add `import logging` / `import sqlite3` to `closet_token.py` if missing).

- [ ] **Step 4: Implement the rebuild guard**

`lfg_core/nft_listener.py` `_apply_closet`: replace the `if isinstance(metadata, dict):` branch body with:

```python
    if isinstance(metadata, dict):
        if closet_market_store.has_unmirrored_fill(conn, owner):
            # #443: a Closet Market fill moved a unit in the DB first; this
            # owner's token metadata is behind until the fill's mirror modify
            # lands. Rebuilding now would resurrect the moved unit.
            logging.info(f"_apply_closet: {owner} has an unmirrored Closet Market fill; keeping DB contents")
        else:
            assets, bodies = closet_token.parse_closet_metadata(metadata, genesis)
            economy_store.set_closet_contents(conn, owner, assets, bodies)
```

and add `closet_market_store` to the `from lfg_core import (...)` list.

`scripts/backfill_economy.py` `_reconcile_closet`: replace

```python
    assets, bodies = closet_token.parse_closet_metadata(metadata, genesis)
    economy_store.set_closet_contents(conn, owner, assets, bodies)
```

with

```python
    if closet_market_store.has_unmirrored_fill(conn, owner):
        # #443: DB is ahead of the token until the fill's mirror lands — see nft_listener._apply_closet.
        print(f"skip contents for {owner}: unmirrored Closet Market fill")
    else:
        assets, bodies = closet_token.parse_closet_metadata(metadata, genesis)
        economy_store.set_closet_contents(conn, owner, assets, bodies)
```

and add `closet_market_store` to its `from lfg_core import (...)` list.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_mirror_guard.py tests/test_closet_token.py tests/test_closet_token_lifecycle.py tests/test_closet_reconcile.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add lfg_core/closet_token.py lfg_core/nft_listener.py scripts/backfill_economy.py tests/test_closet_market_mirror_guard.py
git commit -m "feat(closet-market): orders block in Closet metadata; never rebuild over an unmirrored fill (#443)"
```

---
### Task 7: Ledger primitives — escrow txs, app payments, lookups, Xaman payloads

**Files:**
- Modify: `lfg_core/xrpl_ops.py` (append after `current_validated_ledger_index`; extend the request imports)
- Modify: `lfg_core/xumm_ops.py` (append after `create_onramp_payment_payload`)
- Test: `tests/test_closet_market_xrpl_ops.py`

**Interfaces:**
- Consumes:
  - `xrpl_ops._current_validated_ledger_index(client)` and `_submit_and_confirm(tx, wallet, client, label)`
  - `IndeterminateResultError`, `_carries_memo(tx, tag)`, `_CLAIM_SCAN_LEDGER_SLACK`
  - `config.CLOSET_MARKET_LEDGER_MARGIN`
- Produces:
  - `@dataclass(frozen=True) TxOutcome(state: str, tx_hash: str | None, last_ledger_seq: int | None)`, where state ∈ `confirmed|failed|unknown`
  - `class TxNotSubmitted(RuntimeError)`
  - Backend txs:
    - `async app_brix_payment(destination: str, value: str, tag: str, action: str) -> TxOutcome`
    - `async escrow_finish(owner: str, offer_sequence: int, condition: str, fulfillment: str, tag: str) -> TxOutcome`
    - `async escrow_cancel(owner: str, offer_sequence: int, tag: str) -> TxOutcome`
  - Ledger reads:
    - `async get_escrow(owner: str, sequence: int) -> dict | None` (raises on transport failure)
    - `async find_app_txs_by_memo(tag: str, min_ledger: int | None) -> list[dict]`
    - `tx_entry_hash(entry: dict) -> str | None`, `tx_entry_result(entry: dict) -> str | None`
  - Xaman payloads:
    - `async xumm_ops.create_closet_bid_payload(account, amount, destination, condition, cancel_after, *, return_url=None, user_token=None, platform=memos.PLATFORM_BACKEND) -> dict | None`
    - `async xumm_ops.create_closet_buy_payload(account, amount, destination, invoice_id, *, send_max_drops=None, return_url=None, user_token=None, platform=memos.PLATFORM_BACKEND) -> dict | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_xrpl_ops.py
import asyncio

import pytest
from xrpl.models.transactions import EscrowCancel, EscrowFinish, Payment

from lfg_core import config, memos, xrpl_ops, xumm_ops


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tag_hex(tag):
    return tag.encode().hex().upper()


@pytest.fixture
def submit(monkeypatch):
    seen = {"result": {"hash": "H1", "meta": {"TransactionResult": "tesSUCCESS"}}, "raise": None}

    async def ledger(client):
        return seen.get("ledger", 1000)

    async def fake_submit(tx, wallet, client, label, **kwargs):
        seen["tx"] = tx
        if seen["raise"] is not None:
            raise seen["raise"]
        return seen["result"]

    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", ledger)
    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
    monkeypatch.setattr(config, "CLOSET_MARKET_LEDGER_MARGIN", 40)
    return seen


def test_app_brix_payment_shape(submit):
    out = _run(xrpl_ops.app_brix_payment("rSeller", "9.3", "lfg:closet_forward:F1", memos.ACTION_CLOSET_FORWARD))
    assert out == xrpl_ops.TxOutcome("confirmed", "H1", 1040)
    tx = submit["tx"]
    assert isinstance(tx, Payment)
    assert tx.account == config.SIGNING_ACCOUNT and tx.destination == "rSeller"
    assert tx.amount.currency == config.BRIX_CURRENCY_HEX and tx.amount.issuer == config.BRIX_ISSUER
    assert tx.amount.value == "9.3"
    assert tx.source_tag == config.SOURCE_TAG and tx.last_ledger_sequence == 1040
    assert tx.memos[-1].memo_data == _tag_hex("lfg:closet_forward:F1")
    assert len(tx.memos) > 1  # provenance memos precede the tag


def test_definitive_failure_and_unknown(submit):
    submit["result"] = None
    assert _run(xrpl_ops.app_brix_payment("rS", "1", "lfg:closet_refund:X", memos.ACTION_CLOSET_REFUND)).state == "failed"
    submit["raise"] = xrpl_ops.IndeterminateResultError("lost")
    out = _run(xrpl_ops.app_brix_payment("rS", "1", "lfg:closet_refund:X", memos.ACTION_CLOSET_REFUND))
    assert out == xrpl_ops.TxOutcome("unknown", None, 1040)


def test_unreadable_ledger_means_not_submitted(submit):
    submit["ledger"] = None
    with pytest.raises(xrpl_ops.TxNotSubmitted):
        _run(xrpl_ops.escrow_cancel("rBidder", 5, "lfg:closet_cancel:O1"))
    assert "tx" not in submit


def test_escrow_finish_and_cancel_shape(submit):
    _run(xrpl_ops.escrow_finish("rBidder", 5, "A025", "A022", "lfg:closet_finish:F1"))
    tx = submit["tx"]
    assert isinstance(tx, EscrowFinish)
    assert (tx.owner, tx.offer_sequence, tx.condition, tx.fulfillment) == ("rBidder", 5, "A025", "A022")
    assert tx.account == config.SIGNING_ACCOUNT and tx.source_tag == config.SOURCE_TAG
    _run(xrpl_ops.escrow_cancel("rBidder", 5, "lfg:closet_cancel:O1"))
    assert isinstance(submit["tx"], EscrowCancel) and submit["tx"].offer_sequence == 5


class _Resp:
    def __init__(self, result, ok=True):
        self.result, self._ok = result, ok

    def is_successful(self):
        return self._ok


class _Client:
    responses: list = []

    def __init__(self, url):
        pass

    def request(self, req):
        return _Client.responses.pop(0)


def test_get_escrow(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _Client)
    _Client.responses = [_Resp({"node": {"Account": "rA"}}), _Resp({"error": "entryNotFound"}, ok=False),
                         _Resp({"error": "tooBusy"}, ok=False)]
    assert _run(xrpl_ops.get_escrow("rA", 5)) == {"Account": "rA"}
    assert _run(xrpl_ops.get_escrow("rA", 5)) is None
    with pytest.raises(RuntimeError):
        _run(xrpl_ops.get_escrow("rA", 5))


def test_find_app_txs_by_memo_pages_and_ignores_inbound(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _Client)
    tag = "lfg:closet_forward:F1"
    memo = [{"Memo": {"MemoData": _tag_hex(tag)}}]
    app = config.SIGNING_ACCOUNT
    mine = {"validated": True, "hash": "OK", "meta": {"TransactionResult": "tesSUCCESS"},
            "tx_json": {"Account": app, "Memos": memo}}
    inbound = {"validated": True, "hash": "EVIL", "meta": {"TransactionResult": "tesSUCCESS"},
               "tx_json": {"Account": "rEvil", "Memos": memo}}
    unvalidated = {"validated": False, "hash": "U", "tx_json": {"Account": app, "Memos": memo}}
    _Client.responses = [
        _Resp({"transactions": [inbound, unvalidated], "marker": "m"}),
        _Resp({"transactions": [mine]}),
    ]
    found = _run(xrpl_ops.find_app_txs_by_memo(tag, 500))
    assert [xrpl_ops.tx_entry_hash(e) for e in found] == ["OK"]
    assert xrpl_ops.tx_entry_result(found[0]) == "tesSUCCESS"


def test_closet_payload_builders(monkeypatch):
    calls = []

    async def fake_create(txjson, options=None, user_token=None, memos_json=None, custom_meta=None):
        calls.append((txjson, options, user_token, memos_json))
        return {"uuid": "U", "xumm_url": "x", "qr_url": "q", "push": None}

    monkeypatch.setattr(xumm_ops, "_create_xumm_payload", fake_create)
    amount = {"currency": "BRIX", "issuer": "rI", "value": "10"}
    _run(xumm_ops.create_closet_bid_payload("rBidder", amount, "rApp", "A025", 123, user_token="T",
                                            platform=memos.PLATFORM_WEBAPP))
    txjson, options, token, memos_json = calls[0]
    assert txjson == {"TransactionType": "EscrowCreate", "Account": "rBidder", "Destination": "rApp",
                      "Amount": amount, "Condition": "A025", "CancelAfter": 123}
    assert token == "T" and options["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES
    assert memos_json == memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_CLOSET_BID)
    _run(xumm_ops.create_closet_buy_payload("rBuyer", amount, "rApp", "AB" * 32, send_max_drops="2500000"))
    txjson = calls[1][0]
    assert txjson["InvoiceID"] == "AB" * 32 and txjson["SendMax"] == "2500000" and "Memos" not in txjson
    _run(xumm_ops.create_closet_buy_payload("rBuyer", amount, "rApp", "AB" * 32))
    assert "SendMax" not in calls[2][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_xrpl_ops.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.xrpl_ops' has no attribute 'app_brix_payment'`

- [ ] **Step 3: Implement the XRPL helpers**

In `lfg_core/xrpl_ops.py`:
- Add `EscrowCancel, EscrowFinish` to the `from xrpl.models.transactions import (...)` list, and `AccountTx, LedgerEntry` to the requests list if absent.
- Add `from xrpl.models.requests.ledger_entry import Escrow as LedgerEntryEscrow`.
- Then append:

```python
# --- Closet Market (#443) ------------------------------------------------------
#
# Backend txs from the app wallet (config.SIGNING_ACCOUNT) with a pinned
# LastLedgerSequence and a per-phase `lfg:closet_<phase>:<id>` memo. The
# memo makes a tx findable; the pinned LLS makes absence decidable. The same
# rule as BRIX claims: "unknown" is never a failure until the validated
# ledger has passed last_ledger_seq and find_app_txs_by_memo finds nothing.


@dataclass(frozen=True)
class TxOutcome:
    state: str  # "confirmed" | "failed" | "unknown"
    tx_hash: str | None
    last_ledger_seq: int | None


class TxNotSubmitted(RuntimeError):
    """Nothing reached the ledger (the validated index could not be read)."""


def _closet_memos(action: str, tag: str) -> list[Memo]:
    return [
        *memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, action),
        Memo(memo_data=tag.encode().hex().upper()),
    ]


async def _submit_app_tx(build: Callable[[int], Transaction], label: str) -> TxOutcome:
    client = JsonRpcClient(config.JSON_RPC_URL)
    current = await _current_validated_ledger_index(client)
    if current is None:
        raise TxNotSubmitted(f"{label}: could not read the validated ledger index")
    last_ledger_seq = current + config.CLOSET_MARKET_LEDGER_MARGIN
    tx = build(last_ledger_seq)
    wallet = Wallet.from_seed(config.SEED)
    try:
        result = await _submit_and_confirm(tx, wallet, client, label)
    except IndeterminateResultError:
        return TxOutcome("unknown", None, last_ledger_seq)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception(f"{label}: submit raised; treating the outcome as unknown")
        return TxOutcome("unknown", None, last_ledger_seq)
    if result is None:
        return TxOutcome("failed", None, last_ledger_seq)
    tx_hash = result.get("hash")
    return TxOutcome("confirmed", tx_hash if isinstance(tx_hash, str) else None, last_ledger_seq)


async def app_brix_payment(destination: str, value: str, tag: str, action: str) -> TxOutcome:
    """BRIX Payment app wallet -> destination (fill forward, overshoot or refund)."""
    return await _submit_app_tx(
        lambda lls: Payment(
            account=config.SIGNING_ACCOUNT,
            destination=destination,
            amount=IssuedCurrencyAmount(
                currency=config.BRIX_CURRENCY_HEX, issuer=config.BRIX_ISSUER, value=value
            ),
            source_tag=config.SOURCE_TAG,
            last_ledger_sequence=lls,
            memos=_closet_memos(action, tag),
        ),
        "app_brix_payment",
    )


async def escrow_finish(
    owner: str, offer_sequence: int, condition: str, fulfillment: str, tag: str
) -> TxOutcome:
    return await _submit_app_tx(
        lambda lls: EscrowFinish(
            account=config.SIGNING_ACCOUNT,
            owner=owner,
            offer_sequence=offer_sequence,
            condition=condition,
            fulfillment=fulfillment,
            source_tag=config.SOURCE_TAG,
            last_ledger_sequence=lls,
            memos=_closet_memos(memos.ACTION_CLOSET_FILL, tag),
        ),
        "escrow_finish",
    )


async def escrow_cancel(owner: str, offer_sequence: int, tag: str) -> TxOutcome:
    """Return an expired bid's BRIX to its owner (valid only after CancelAfter)."""
    return await _submit_app_tx(
        lambda lls: EscrowCancel(
            account=config.SIGNING_ACCOUNT,
            owner=owner,
            offer_sequence=offer_sequence,
            source_tag=config.SOURCE_TAG,
            last_ledger_sequence=lls,
            memos=_closet_memos(memos.ACTION_CLOSET_REFUND, tag),
        ),
        "escrow_cancel",
    )


async def get_escrow(owner: str, sequence: int) -> dict[str, Any] | None:
    """The validated Escrow ledger object, or None if it does not exist
    (finished / cancelled / never created). Raises on any other failure —
    a failed lookup is never "absent"."""
    client = JsonRpcClient(config.JSON_RPC_URL)
    request = LedgerEntry(escrow=LedgerEntryEscrow(owner=owner, seq=sequence), ledger_index="validated")
    response = await asyncio.to_thread(client.request, request)
    result = response.result
    if response.is_successful() and isinstance(result, dict) and isinstance(result.get("node"), dict):
        return cast(dict[str, Any], result["node"])
    if isinstance(result, dict) and result.get("error") == "entryNotFound":
        return None
    raise RuntimeError(f"escrow lookup failed for {owner}/{sequence}: {result!r}")


def tx_entry_hash(entry: dict[str, Any]) -> str | None:
    tx = entry.get("tx") or entry.get("tx_json") or {}
    value = entry.get("hash") or tx.get("hash")
    return value if isinstance(value, str) and value else None


def tx_entry_result(entry: dict[str, Any]) -> str | None:
    meta = entry.get("meta") or entry.get("metaData") or {}
    return meta.get("TransactionResult") if isinstance(meta, dict) else None


async def find_app_txs_by_memo(tag: str, min_ledger: int | None = None) -> list[dict[str, Any]]:
    """Every VALIDATED transaction SENT by the app wallet carrying `tag`.
    Inbound transactions are ignored: memos are user-writable, so a stranger
    could send the app wallet a tx with a guessed tag. Raises on transport
    failure (never report a failed scan as "nothing found")."""
    account = config.SIGNING_ACCOUNT
    client = JsonRpcClient(config.JSON_RPC_URL)
    scan_from = -1 if min_ledger is None else max(1, min_ledger - _CLAIM_SCAN_LEDGER_SLACK)
    found: list[dict[str, Any]] = []
    marker: Any = None
    while True:
        request = AccountTx(account=account, limit=200, marker=marker, ledger_index_min=scan_from)
        response = await asyncio.to_thread(client.request, request)
        result = response.result
        if not response.is_successful() or not isinstance(result, dict):
            raise RuntimeError(f"account_tx failed while scanning for {tag}: {result!r}")
        for entry in result.get("transactions", []):
            tx = entry.get("tx") or entry.get("tx_json") or {}
            if entry.get("validated") and tx.get("Account") == account and _carries_memo(tx, tag):
                found.append(entry)
        marker = result.get("marker")
        if not marker:
            return found
```

- [ ] **Step 4: Implement the Xaman payload builders**

Append to `lfg_core/xumm_ops.py`:

```python
async def create_closet_bid_payload(
    account: str,
    amount: dict[str, str],
    destination: str,
    condition: str,
    cancel_after: int,
    *,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
) -> dict[str, Any] | None:
    """#443: the bidder locks BRIX in a TokenEscrow to the app wallet. Only the
    backend holds the fulfillment for `condition`; CancelAfter bounds the lock."""
    return await _create_xumm_payload(
        {
            "TransactionType": "EscrowCreate",
            "Account": account,
            "Destination": destination,
            "Amount": amount,
            "Condition": condition,
            "CancelAfter": cancel_after,
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(memos.INITIATOR_USER, platform, memos.ACTION_CLOSET_BID),
    )


async def create_closet_buy_payload(
    account: str,
    amount: dict[str, str],
    destination: str,
    invoice_id: str,
    *,
    send_max_drops: str | None = None,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
) -> dict[str, Any] | None:
    """#443: the buyer pays a Closet ask to the app wallet. InvoiceID (sha256 of
    the fill id) ties the payment to its fill. send_max_drops turns it into an
    XRP->BRIX path payment for buyers holding too little BRIX."""
    txjson: dict[str, Any] = {
        "TransactionType": "Payment",
        "Account": account,
        "Destination": destination,
        "Amount": amount,
        "InvoiceID": invoice_id,
    }
    if send_max_drops is not None:
        txjson["SendMax"] = send_max_drops
    return await _create_xumm_payload(
        txjson,
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(memos.INITIATOR_USER, platform, memos.ACTION_CLOSET_BUY),
    )
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_xrpl_ops.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add lfg_core/xrpl_ops.py lfg_core/xumm_ops.py tests/test_closet_market_xrpl_ops.py
git commit -m "feat(closet-market): escrow finish/cancel, app payments, memo lookups, bid/buy payloads (#443)"
```

---

### Task 8: Flow I — bid escrow lifecycle (open, cross, expire, cancel)

**Files:**
- Create: `lfg_core/closet_market_flow.py`
- Create: `tests/closet_market_helpers.py` (shared fakes for Tasks 8, 9)
- Test: `tests/test_closet_market_flow_bids.py`

**Interfaces:**
- Consumes:
  - Task 4 store functions
  - Task 7 `TxOutcome`, `TxNotSubmitted`, `tx_entry_hash`, `tx_entry_result`, `RIPPLE_EPOCH_OFFSET`
  - `market_ops.brix_amount_dict`, `owner_lock.owner_lock`
- Produces:
  - `@dataclass ClosetMarketDeps` with fields:
    - `conn_factory`, `app_account`, `fee_bps`
    - `payload_status_fn`, `get_tx_fn`, `get_escrow_fn`
    - `escrow_finish_fn`, `escrow_cancel_fn`, `payment_fn`
    - `find_txs_fn`, `ledger_index_fn`
    - `mirror_fn`, `unseal_fn`
    - `records_dir`, `now_fn`, `signing_wait_seconds`
  - `async advance_bid(order_id: str, deps: ClosetMarketDeps) -> str | None` returns the id of a fill created by crossing, if any
  - `CANCEL_SLACK_SECONDS = 60`

- [ ] **Step 1: Write the shared fakes**

```python
# tests/closet_market_helpers.py
"""Fakes shared by the Closet Market flow tests (#443)."""

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core import market_ops
from lfg_core.xrpl_ops import TxOutcome

APP = "rAppWallet"
SELLER, BUYER, OTHER = "rSeller", "rBuyer", "rOther"
CANCEL_AFTER = 800_000_000  # ripple epoch


def make_db(path, seller_count=1):
    c = sqlite3.connect(path)
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER, OTHER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", seller_count)], [])
    c.commit()
    c.close()


def conn(path):
    c = sqlite3.connect(path)
    es.init_economy_schema(c)
    return c


@dataclass
class Fakes:
    payload_status: dict = field(default_factory=dict)
    txs: dict = field(default_factory=dict)
    escrows: dict = field(default_factory=dict)
    finish_outcomes: list = field(default_factory=list)
    cancel_outcomes: list = field(default_factory=list)
    payment_outcomes: list = field(default_factory=list)
    found: dict = field(default_factory=dict)
    ledger_index: int | None = 100
    mirror_error: Exception | None = None
    finishes: list = field(default_factory=list)
    cancels: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    mirrored: list = field(default_factory=list)

    async def payload_status_fn(self, uuid):
        return self.payload_status.get(uuid)

    async def get_tx_fn(self, tx_hash):
        return self.txs.get(tx_hash, {"validated": False})

    async def get_escrow_fn(self, owner, seq):
        return self.escrows.get((owner, seq))

    async def escrow_finish_fn(self, owner, seq, condition, fulfillment, tag):
        self.finishes.append((owner, seq, condition, fulfillment, tag))
        state = self.finish_outcomes.pop(0) if self.finish_outcomes else "confirmed"
        if state == "confirmed":
            self.escrows.pop((owner, seq), None)
        return TxOutcome(state, f"FIN{len(self.finishes)}" if state == "confirmed" else None, 140)

    async def escrow_cancel_fn(self, owner, seq, tag):
        self.cancels.append((owner, seq, tag))
        state = self.cancel_outcomes.pop(0) if self.cancel_outcomes else "confirmed"
        if state == "confirmed":
            self.escrows.pop((owner, seq), None)
        return TxOutcome(state, "CAN" if state == "confirmed" else None, 140)

    async def payment_fn(self, destination, value, tag, action):
        self.payments.append((destination, value, tag, action))
        state = self.payment_outcomes.pop(0) if self.payment_outcomes else "confirmed"
        return TxOutcome(state, f"PAY{len(self.payments)}" if state == "confirmed" else None, 140)

    async def find_txs_fn(self, tag, lls):
        return self.found.get(tag, [])

    async def ledger_index_fn(self):
        return self.ledger_index

    async def mirror_fn(self, c, owner):
        if self.mirror_error is not None:
            raise self.mirror_error
        self.mirrored.append(owner)


def deps(path, f, tmp_path, *, fee_bps=0, now=1_000_000.0):
    return cmf.ClosetMarketDeps(
        conn_factory=lambda: conn(path),
        app_account=APP,
        fee_bps=fee_bps,
        payload_status_fn=f.payload_status_fn,
        get_tx_fn=f.get_tx_fn,
        get_escrow_fn=f.get_escrow_fn,
        escrow_finish_fn=f.escrow_finish_fn,
        escrow_cancel_fn=f.escrow_cancel_fn,
        payment_fn=f.payment_fn,
        find_txs_fn=f.find_txs_fn,
        ledger_index_fn=f.ledger_index_fn,
        mirror_fn=f.mirror_fn,
        unseal_fn=lambda blob: "FULFILL",
        records_dir=str(tmp_path / "records"),
        now_fn=lambda: now,
    )


def pending_bid(path, *, owner=BUYER, price="10"):
    c = conn(path)
    order = cms.create_pending_bid(
        c, owner=owner, slot="Head", value="Crown", price_brix=price, platform=None, condition="COND",
        fulfillment_enc="SEALED", cancel_after=CANCEL_AFTER, payload_uuid=f"U-{owner}", xumm_url="x",
        qr_url="q", push=None,
    )
    c.close()
    return order


def open_bid(path, f, *, owner=BUYER, price="10", seq=5):
    order = pending_bid(path, owner=owner, price=price)
    c = conn(path)
    order = cms.mark_bid_open(c, order["id"], escrow_tx_hash=f"ESC-{order['id']}", escrow_owner_seq=seq)
    c.close()
    f.escrows[(owner, seq)] = escrow_node(owner, price)
    return order


def escrow_node(owner, price, condition="COND"):
    return {"Account": owner, "Destination": APP, "Amount": market_ops.brix_amount_dict(price), "Condition": condition}


def escrow_create_tx(owner, price, *, seq=7, condition="COND", result="tesSUCCESS", amount=None):
    return {
        "validated": True,
        "hash": "ECH",
        "meta": {"TransactionResult": result},
        "tx_json": {
            "TransactionType": "EscrowCreate", "Account": owner, "Destination": APP,
            "Amount": amount or market_ops.brix_amount_dict(price), "Condition": condition,
            "CancelAfter": CANCEL_AFTER, "Sequence": seq,
        },
    }


def landed(tx_hash="LANDED", result="tesSUCCESS"):
    return {"validated": True, "hash": tx_hash, "meta": {"TransactionResult": result}, "tx_json": {"Account": APP}}


def run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def order(path, order_id) -> dict[str, Any]:
    c = conn(path)
    try:
        return cms.get_order(c, order_id)
    finally:
        c.close()


def fill(path, fill_id) -> dict[str, Any]:
    c = conn(path)
    try:
        return cms.get_fill(c, fill_id)
    finally:
        c.close()
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_closet_market_flow_bids.py
from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import market_ops, memos
from lfg_core.xrpl_ops import RIPPLE_EPOCH_OFFSET
from tests.closet_market_helpers import (
    BUYER, CANCEL_AFTER, OTHER, SELLER, Fakes, conn, deps, escrow_create_tx, escrow_node, landed,
    make_db, open_bid, order, pending_bid, run,
)

EXPIRED_NOW = float(CANCEL_AFTER + RIPPLE_EPOCH_OFFSET + cmf.CANCEL_SLACK_SECONDS + 1)


def _db(tmp_path):
    path = str(tmp_path / "onchain.db")
    make_db(path)
    return path


def test_unsigned_bid_stays_pending(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": False}
    assert run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5))) is None
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW


def test_expired_payload_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLED and "expired" in got["error"]


def test_signer_mismatch_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": OTHER, "txid": "T"}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED


def test_verified_escrow_opens_and_crosses_a_resting_ask(tmp_path):
    path, f = _db(tmp_path), Fakes()
    c = conn(path)
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    c.close()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": BUYER, "txid": "ECH"}
    f.txs["ECH"] = escrow_create_tx(BUYER, "10", seq=7)
    f.escrows[(BUYER, 7)] = escrow_node(BUYER, "10")
    fill_id = run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert (got["escrow_owner_seq"], got["escrow_tx_hash"], got["state"]) == (7, "ECH", cms.MATCHED)
    assert fill_id is not None
    assert order(path, ask["id"])["state"] == cms.MATCHED


def test_amount_mismatch_never_opens(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": BUYER, "txid": "ECH"}
    f.txs["ECH"] = escrow_create_tx(BUYER, "10", amount=market_ops.brix_amount_dict("1"))
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLED and "wrong amount" in got["error"]


def test_open_bid_expiry_cancels_escrow(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f)
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=EXPIRED_NOW)))
    got = order(path, bid["id"])
    assert got["state"] == cms.EXPIRED and got["cancel_refund_hash"] == "CAN"
    assert f.cancels == [(BUYER, 5, f"lfg:closet_cancel:{bid['id']}")]
    assert f.finishes == [] and f.payments == []


def test_user_cancel_before_expiry_finishes_then_refunds(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f, price="10")
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path)))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLED
    assert f.finishes == [(BUYER, 5, "COND", "FULFILL", f"lfg:closet_cancel_finish:{bid['id']}")]
    assert f.payments == [(BUYER, "10", f"lfg:closet_cancel_refund:{bid['id']}", memos.ACTION_CLOSET_REFUND)]


def test_unknown_cancel_finish_resolves_from_memo_then_refunds(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    bid = open_bid(path, f)
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    d = deps(path, f, tmp_path)
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["pending_phase"], got["pending_lls"]) == (cms.CANCELLING, "cancel_finish", 140)
    run(cmf.advance_bid(bid["id"], d))  # ledger 100 <= lls 140, nothing found: wait
    assert order(path, bid["id"])["pending_phase"] == "cancel_finish" and f.payments == []
    f.found[f"lfg:closet_cancel_finish:{bid['id']}"] = [landed("FINX")]
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["cancel_finish_hash"]) == (cms.CANCELLED, "FINX")
    assert len(f.finishes) == 1 and len(f.payments) == 1


def test_unknown_cancel_finish_absent_past_lls_retries(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    bid = open_bid(path, f)
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    d = deps(path, f, tmp_path)
    run(cmf.advance_bid(bid["id"], d))
    f.ledger_index = 200  # past lls 140, no memo'd tx: it can never validate
    run(cmf.advance_bid(bid["id"], d))
    run(cmf.advance_bid(bid["id"], d))
    assert len(f.finishes) == 2 and order(path, bid["id"])["state"] == cms.CANCELLED


def test_cancel_when_escrow_already_gone_sends_nothing(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f)
    f.escrows.clear()  # the bidder EscrowCancel'ed it themselves after CancelAfter
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED
    assert f.finishes == f.cancels == f.payments == []


def test_open_bid_recrosses_on_sweep(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f, price="10")
    c = conn(path)
    c.execute("INSERT INTO closet_orders (id, side, owner, slot, value, price_brix, state, created_ts, updated_ts) "
              "VALUES ('A1', 'ask', ?, 'Head', 'Crown', '9', 'open', 1, 1)", (SELLER,))
    c.commit()
    c.close()
    assert run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path))) is not None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_flow_bids.py -v`
Expected: FAIL with `ImportError: cannot import name 'closet_market_flow'`

- [ ] **Step 4: Implement the flow module (deps, shared helpers, bid lifecycle)**

```python
# lfg_core/closet_market_flow.py
"""Closet Market state machines (#443).

Two machines, both driven inline by the service and retried by the 2-minute
settlement sweep; every step is idempotent and safe to re-enter.

Bid (closet_orders, side='bid'):
  pending_escrow --signed+validated EscrowCreate matches--> open --cross--> matched
  open --expiry / user cancel--> cancelling --> expired | cancelled
  A cancelling bid is returned by EscrowCancel after CancelAfter, else by
  EscrowFinish (backend holds the fulfillment) followed by a refund Payment.

Fill (closet_fills):
  funds_pending --funds in--> funded --asset move (1 sqlite tx)--> asset_moved
  --forward seller [+ overshoot buyer]--> paid --mirror both Closets--> mirrored
  refund_pending --refund--> refunded ; funds_pending --cannot proceed--> failed
  Any backend tx with an unknown outcome parks the fill in `indeterminate`
  with pending_phase/pending_lls. It resolves ONLY by finding the memo-tagged
  tx (landed) or by the validated ledger passing pending_lls with nothing
  found (absent -> retry). Absence alone is never failure.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from lfg_core import closet_market_store as cms
from lfg_core import closet_token, config, market_ops, memos, owner_lock
from lfg_core.xrpl_ops import (
    RIPPLE_EPOCH_OFFSET,
    TxNotSubmitted,
    TxOutcome,
    tx_entry_hash,
    tx_entry_result,
)

CANCEL_SLACK_SECONDS = 60  # ledger close time lags wall clock; EscrowCancel before CancelAfter is tecNO_PERMISSION
MAX_STEPS = 8


@dataclass
class ClosetMarketDeps:
    conn_factory: Callable[[], sqlite3.Connection]
    app_account: str
    fee_bps: int
    payload_status_fn: Callable[[str], Awaitable[dict[str, Any] | None]]
    get_tx_fn: Callable[[str], Awaitable[dict[str, Any]]]
    get_escrow_fn: Callable[[str, int], Awaitable[dict[str, Any] | None]]
    escrow_finish_fn: Callable[[str, int, str, str, str], Awaitable[TxOutcome]]
    escrow_cancel_fn: Callable[[str, int, str], Awaitable[TxOutcome]]
    payment_fn: Callable[[str, str, str, str], Awaitable[TxOutcome]]
    find_txs_fn: Callable[[str, int | None], Awaitable[list[dict[str, Any]]]]
    ledger_index_fn: Callable[[], Awaitable[int | None]]
    mirror_fn: Callable[[sqlite3.Connection, str], Awaitable[None]]
    unseal_fn: Callable[[str], str]
    records_dir: str = config.ECONOMY_RECORDS_DIR
    now_fn: Callable[[], float] = time.time
    signing_wait_seconds: int = 3600


def _journal(deps: ClosetMarketDeps, kind: str, row: dict[str, Any] | None) -> None:
    """Best-effort on-disk record of every transition (economy_flow._write_record posture)."""
    if row is None:
        return
    try:
        os.makedirs(deps.records_dir, exist_ok=True)
        path = os.path.join(deps.records_dir, f"closet-{kind}-{row['id']}.json")
        public = {k: v for k, v in row.items() if k != "fulfillment_enc"}
        with open(path, "w") as fh:
            json.dump(public, fh, indent=2, default=str)
    except Exception:
        logging.error(f"closet market journal write failed: {traceback.format_exc()}")


def _amount_equals(a: Any, b: dict[str, str]) -> bool:
    if not isinstance(a, dict):
        return False
    try:
        return (
            str(a.get("currency", "")).upper() == str(b["currency"]).upper()
            and a.get("issuer") == b["issuer"]
            and Decimal(str(a.get("value"))) == Decimal(b["value"])
        )
    except (InvalidOperation, ValueError):
        return False


def _tx_body(tx: dict[str, Any]) -> dict[str, Any]:
    body = tx.get("tx_json")
    return body if isinstance(body, dict) else tx


async def _submit(fn: Callable[..., Awaitable[TxOutcome]], *args: Any) -> TxOutcome | None:
    try:
        return await fn(*args)
    except TxNotSubmitted as exc:
        logging.warning(f"closet market tx not submitted: {exc}")
        return None


async def _phase_outcome(deps: ClosetMarketDeps, tag: str, lls: int | None) -> tuple[str, str | None]:
    """("landed", hash) | ("absent", None) | ("wait", None) for an unknown tx."""
    try:
        entries = await deps.find_txs_fn(tag, lls)
    except Exception:
        logging.warning(f"closet market memo lookup failed for {tag}: {traceback.format_exc()}")
        return "wait", None
    for entry in entries:
        if tx_entry_result(entry) == "tesSUCCESS":
            return "landed", tx_entry_hash(entry) or cms.HASH_UNKNOWN
    index = await deps.ledger_index_fn()
    if index is not None and lls is not None and index > lls:
        return "absent", None
    return "wait", None


# --- bids ---------------------------------------------------------------------


def _bid_expired(order: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    cancel_after = order["cancel_after"]
    return cancel_after is not None and int(deps.now_fn()) - RIPPLE_EPOCH_OFFSET >= cancel_after + CANCEL_SLACK_SECONDS


def _final_cancel_state(order: dict[str, Any]) -> str:
    return cms.EXPIRED if order["cancel_reason"] == "expired" else cms.CANCELLED


def _escrow_mismatch(fields: dict[str, Any], order: dict[str, Any], deps: ClosetMarketDeps) -> str | None:
    if fields.get("Account") != order["owner"]:
        return "wrong account"
    if fields.get("Destination") != deps.app_account:
        return "wrong destination"
    if not _amount_equals(fields.get("Amount"), market_ops.brix_amount_dict(order["price_brix"])):
        return "wrong amount"
    if str(fields.get("Condition") or "").upper() != str(order["condition"]).upper():
        return "wrong condition"
    return None


def _cancel_unopened(conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps, reason: str) -> None:
    cms.update_order(conn, order["id"], state=cms.CANCELLED, cancel_reason="unopened", error=reason)
    _journal(deps, "order", cms.get_order(conn, order["id"]))


async def advance_bid(order_id: str, deps: ClosetMarketDeps) -> str | None:
    async with owner_lock.owner_lock(f"closet-order:{order_id}"):
        conn = deps.conn_factory()
        try:
            order = cms.get_order(conn, order_id)
            if order is None or order["side"] != cms.SIDE_BID:
                return None
            if order["state"] == cms.PENDING_ESCROW:
                return await _advance_pending_bid(conn, order, deps)
            if order["state"] == cms.OPEN:
                if not _bid_expired(order, deps):
                    fill = cms.cross_incoming(conn, order_id, fee_bps=deps.fee_bps)
                    return str(fill["id"]) if fill else None
                try:
                    cms.begin_cancel_bid(conn, order_id, owner=None, reason="expired")
                except cms.OrderError:
                    return None
            order = cms.get_order(conn, order_id)
            if order is not None and order["state"] == cms.CANCELLING:
                await _advance_cancelling_bid(conn, order, deps)
            return None
        finally:
            conn.close()


async def _advance_pending_bid(conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps) -> str | None:
    waited = deps.now_fn() - order["created_ts"]
    if not order["signed_txid"]:
        status = await deps.payload_status_fn(order["payload_uuid"]) if order["payload_uuid"] else None
        if status is None or not status.get("signed"):
            if (status or {}).get("expired") or waited > deps.signing_wait_seconds:
                _cancel_unopened(conn, order, deps, "signing request expired")
            return None
        if status.get("account") != order["owner"]:
            _cancel_unopened(conn, order, deps, "the bid was signed by a different account than yours")
            return None
        txid = status.get("txid")
        if not txid:
            return None
        cms.update_order(conn, order["id"], signed_txid=txid)
        order = cms.get_order(conn, order["id"]) or order
    try:
        tx = await deps.get_tx_fn(order["signed_txid"])
    except Exception:
        logging.warning(f"closet bid {order['id']}: tx lookup failed: {traceback.format_exc()}")
        return None
    if not tx.get("validated"):
        if waited > deps.signing_wait_seconds:
            _cancel_unopened(conn, order, deps, "the escrow never validated")
        return None
    result = (tx.get("meta") or {}).get("TransactionResult")
    if result != "tesSUCCESS":
        _cancel_unopened(conn, order, deps, f"escrow create failed: {result}")
        return None
    body = _tx_body(tx)
    problem: str | None
    if body.get("TransactionType") != "EscrowCreate":
        problem = "not an EscrowCreate"
    elif body.get("CancelAfter") != order["cancel_after"] or body.get("FinishAfter") is not None:
        problem = "wrong expiry"
    else:
        problem = _escrow_mismatch(body, order, deps)
    seq_raw = body.get("Sequence") or body.get("TicketSequence")
    seq = seq_raw if isinstance(seq_raw, int) else None
    if problem is None and seq is None:
        problem = "no escrow sequence"
    if problem is None and seq is not None:
        try:
            node = await deps.get_escrow_fn(order["owner"], seq)
        except Exception:
            logging.warning(f"closet bid {order['id']}: escrow lookup failed: {traceback.format_exc()}")
            return None
        problem = "the escrow is no longer on-ledger" if node is None else _escrow_mismatch(node, order, deps)
    if problem is not None or seq is None:
        logging.error(f"closet bid {order['id']}: validated escrow rejected ({problem})")
        _cancel_unopened(conn, order, deps, f"escrow did not match the bid ({problem})")
        return None
    cms.mark_bid_open(conn, order["id"], escrow_tx_hash=order["signed_txid"], escrow_owner_seq=seq)
    _journal(deps, "order", cms.get_order(conn, order["id"]))
    fill = cms.cross_incoming(conn, order["id"], fee_bps=deps.fee_bps)
    return str(fill["id"]) if fill else None


def _apply_order_outcome(conn: sqlite3.Connection, order: dict[str, Any], out: TxOutcome | None, *,
                         phase: str, hash_field: str, complete: bool) -> str:
    if out is None:
        return "not_submitted"
    if out.state == "confirmed":
        fields: dict[str, Any] = {hash_field: out.tx_hash or cms.HASH_UNKNOWN, "error": None}
        if complete:
            fields["state"] = _final_cancel_state(order)
        cms.update_order(conn, order["id"], **fields)
    elif out.state == "unknown":
        cms.update_order(conn, order["id"], pending_phase=phase, pending_lls=out.last_ledger_seq)
    else:
        cms.update_order(conn, order["id"], error=f"{phase} failed; will retry")
    return out.state


async def _advance_cancelling_bid(conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps) -> None:
    oid = order["id"]
    if order["pending_phase"]:
        verdict, tx_hash = await _phase_outcome(deps, cms.memo_tag(order["pending_phase"], oid), order["pending_lls"])
        if verdict == "wait":
            return
        fields: dict[str, Any] = {"pending_phase": None, "pending_lls": None}
        if verdict == "landed":
            if order["pending_phase"] == "cancel_finish":
                fields["cancel_finish_hash"] = tx_hash
            else:  # "cancel" (EscrowCancel) and "cancel_refund" both complete it
                fields["cancel_refund_hash"] = tx_hash
                fields["state"] = _final_cancel_state(order)
        cms.update_order(conn, oid, **fields)
        order = cms.get_order(conn, oid) or order
        if order["state"] != cms.CANCELLING:
            _journal(deps, "order", order)
            return
    seq = order["escrow_owner_seq"]
    if order["cancel_finish_hash"] is None:
        if seq is None:
            cms.update_order(conn, oid, state=_final_cancel_state(order))
            return
        try:
            node = await deps.get_escrow_fn(order["owner"], seq)
        except Exception:
            logging.warning(f"closet bid {oid}: escrow lookup failed: {traceback.format_exc()}")
            return
        if node is None:  # cancelled on-ledger after CancelAfter (anyone may): the BRIX is home
            cms.update_order(conn, oid, state=_final_cancel_state(order))
            _journal(deps, "order", cms.get_order(conn, oid))
            return
        if _bid_expired(order, deps):
            out = await _submit(deps.escrow_cancel_fn, order["owner"], seq, cms.memo_tag("cancel", oid))
            _apply_order_outcome(conn, order, out, phase="cancel", hash_field="cancel_refund_hash", complete=True)
            _journal(deps, "order", cms.get_order(conn, oid))
            return
        out = await _submit(
            deps.escrow_finish_fn, order["owner"], seq, order["condition"],
            deps.unseal_fn(order["fulfillment_enc"]), cms.memo_tag("cancel_finish", oid),
        )
        if _apply_order_outcome(conn, order, out, phase="cancel_finish", hash_field="cancel_finish_hash",
                                complete=False) != "confirmed":
            return
        order = cms.get_order(conn, oid) or order
    if order["cancel_refund_hash"] is None:
        out = await _submit(
            deps.payment_fn, order["owner"], order["price_brix"], cms.memo_tag("cancel_refund", oid),
            memos.ACTION_CLOSET_REFUND,
        )
        _apply_order_outcome(conn, order, out, phase="cancel_refund", hash_field="cancel_refund_hash", complete=True)
    _journal(deps, "order", cms.get_order(conn, oid))
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_flow_bids.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Commit**

```bash
git add lfg_core/closet_market_flow.py tests/closet_market_helpers.py tests/test_closet_market_flow_bids.py
git commit -m "feat(closet-market): bid escrow lifecycle — verify, open, cross, expire, cancel (#443)"
```

---

### Task 9: Flow II — fill settlement

**Files:**
- Modify: `lfg_core/closet_market_flow.py` (append)
- Modify: `lfg_core/closet_market_store.py` (add `HASH_NOTHING_DUE`)
- Test: `tests/test_closet_market_flow_settle.py`

**Interfaces:**
- Consumes: Task 4 store functions, Task 8 helpers (`_submit`, `_phase_outcome`, `_journal`, `_tx_body`)
- Produces: `async settle_fill(fill_id: str, deps: ClosetMarketDeps) -> str | None` (the fill state it stopped at); `cms.HASH_NOTHING_DUE`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_flow_settle.py
import json
import os

from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core import market_ops, memos
from tests.closet_market_helpers import (
    APP, BUYER, OTHER, SELLER, Fakes, conn, deps, fill, landed, make_db, open_bid, run,
)


def _db(tmp_path, seller_count=1):
    path = str(tmp_path / "onchain.db")
    make_db(path, seller_count=seller_count)
    return path


def _holder_fill(path, f, *, price="10", fee_bps=0):
    bid = open_bid(path, f, price=price)
    c = conn(path)
    got = cms.fill_bid(c, bid["id"], SELLER, fee_bps=fee_bps, platform=None)
    c.close()
    return got


def _counts(path):
    c = conn(path)
    try:
        return cms.holding_count(c, SELLER, "Head", "Crown"), cms.holding_count(c, BUYER, "Head", "Crown")
    finally:
        c.close()


def test_holder_fill_happy_path(tmp_path):
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f, price="10", fee_bps=700)
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, fee_bps=700))) == cms.MIRRORED
    assert f.finishes == [(BUYER, 5, "COND", "FULFILL", f"lfg:closet_finish:{fl['id']}")]
    assert f.payments == [(SELLER, "9.3", f"lfg:closet_forward:{fl['id']}", memos.ACTION_CLOSET_FORWARD)]
    assert sorted(f.mirrored) == [BUYER, SELLER]
    assert _counts(path) == (0, 1)
    got = fill(path, fl["id"])
    assert (got["escrow_finish_hash"], got["forward_tx_hash"]) == ("FIN1", "PAY1")
    journal = json.load(open(os.path.join(tmp_path, "records", f"closet-fill-{fl['id']}.json")))
    assert journal["state"] == cms.MIRRORED


def test_cross_refunds_overshoot_to_the_bidder(tmp_path):
    path, f = _db(tmp_path), Fakes()
    c = conn(path)
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    c.close()
    bid = open_bid(path, f, price="10")
    c = conn(path)
    fl = cms.cross_incoming(c, bid["id"], fee_bps=0)
    c.close()
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path))) == cms.MIRRORED
    assert [(p[0], p[1]) for p in f.payments] == [(SELLER, "8"), (BUYER, "2")]
    assert f.payments[1][3] == memos.ACTION_CLOSET_REFUND


def test_precheck_blocks_finish_when_buyer_has_no_closet(tmp_path):
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f)
    c = conn(path)
    es.set_closet_status(c, BUYER, ct.PENDING_ACCEPT)
    c.close()
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path))) == cms.FAILED
    assert f.finishes == []
    c = conn(path)
    assert cms.get_order(c, fl["bid_order_id"])["state"] == cms.CANCELLING
    c.close()


def test_unknown_finish_waits_then_resolves_landed(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.INDETERMINATE
    c = conn(path)
    assert cms.available_count(c, SELLER, "Head", "Crown") == 0  # still encumbered while unknown
    c.close()
    f.found[f"lfg:closet_finish:{fl['id']}"] = [landed("FINX")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.finishes) == 1 and fill(path, fl["id"])["escrow_finish_hash"] == "FINX"


def test_unknown_finish_absent_past_lls_is_retried(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    fl = _holder_fill(path, f)  # an unknown finish leaves the fake escrow in place
    d = deps(path, f, tmp_path)
    run(cmf.settle_fill(fl["id"], d))
    f.ledger_index = 500
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.finishes) == 2


def test_failed_finish_with_escrow_gone_fails_the_fill(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["failed"])
    fl = _holder_fill(path, f)
    f.escrows.clear()
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path))) == cms.FAILED
    c = conn(path)
    assert cms.get_order(c, fl["bid_order_id"])["state"] == cms.EXPIRED
    assert cms.available_count(c, SELLER, "Head", "Crown") == 1  # seller's unit released
    c.close()


def test_forward_failure_retries_without_moving_twice(tmp_path):
    path, f = _db(tmp_path), Fakes(payment_outcomes=["failed"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.ASSET_MOVED
    assert fill(path, fl["id"])["attempts"] == 1
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert _counts(path) == (0, 1) and len(f.payments) == 2


def test_unknown_forward_keeps_owner_unmirrored_until_resolved(tmp_path):
    path, f = _db(tmp_path), Fakes(payment_outcomes=["unknown"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.INDETERMINATE
    c = conn(path)
    assert cms.has_unmirrored_fill(c, SELLER)
    c.close()
    f.found[f"lfg:closet_forward:{fl['id']}"] = [landed("FWD")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.payments) == 1 and fill(path, fl["id"])["forward_tx_hash"] == "FWD"


def test_mirror_failure_stays_paid_and_retries(tmp_path):
    path, f = _db(tmp_path), Fakes(mirror_error=ct.ClosetIndeterminateError("boom"))
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.PAID
    f.mirror_error = None
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED


def _take(path, f, *, price="5"):
    c = conn(path)
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix=price, platform=None)
    fl = cms.create_take_fill(c, ask["id"], BUYER, fee_bps=0, platform=None)
    cms.update_fill(c, fl["id"], payload_uuid="PU")
    c.close()
    return ask, fl


def _payment_tx(fill_id, *, account=BUYER, delivered="5", destination=APP):
    return {
        "validated": True, "hash": "PAYTX", "meta": {"TransactionResult": "tesSUCCESS",
                                                   "delivered_amount": market_ops.brix_amount_dict(delivered)},
        "tx_json": {"TransactionType": "Payment", "Account": account, "Destination": destination,
                    "InvoiceID": cms.invoice_id(fill_id), "Amount": market_ops.brix_amount_dict("5")},
    }


def test_take_happy_path(tmp_path):
    path, f = _db(tmp_path), Fakes()
    ask, fl = _take(path, f)
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING  # unsigned
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert f.payments == [(SELLER, "5", f"lfg:closet_forward:{fl['id']}", memos.ACTION_CLOSET_FORWARD)]
    assert _counts(path) == (0, 1)


def test_take_signed_by_another_account_refunds_that_account(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": OTHER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"], account=OTHER)
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10))) == cms.REFUNDED
    assert f.payments == [(OTHER, "5", f"lfg:closet_refund:{fl['id']}", memos.ACTION_CLOSET_REFUND)]
    assert _counts(path) == (1, 0)


def test_take_partial_delivery_refunds_what_arrived(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"], delivered="4.5")
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10))) == cms.REFUNDED
    assert f.payments[0][:2] == (BUYER, "4.5")


def test_take_wrong_invoice_fails_without_refund(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    tx = _payment_tx(fl["id"])
    tx["tx_json"]["InvoiceID"] = "00" * 32
    f.txs["PAYTX"] = tx
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10))) == cms.FAILED
    assert f.payments == []


def test_take_after_ask_sold_refunds(tmp_path):
    path, f = _db(tmp_path), Fakes()
    ask, fl = _take(path, f)
    c = conn(path)
    cms.cancel_ask(c, ask["id"], SELLER)
    c.close()
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10))) == cms.REFUNDED
    assert f.payments[0][:2] == (BUYER, "5")


def test_take_payload_expiry_fails_quietly(tmp_path):
    path, f = _db(tmp_path), Fakes()
    ask, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10))) == cms.FAILED
    c = conn(path)
    assert cms.get_order(c, ask["id"])["state"] == cms.OPEN
    c.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_flow_settle.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.closet_market_flow' has no attribute 'settle_fill'`

- [ ] **Step 3: Add the store constant**

In `lfg_core/closet_market_store.py` below `HASH_UNKNOWN`:

```python
# Stored as forward_tx_hash when price - fee is zero (nothing to send).
HASH_NOTHING_DUE = "nothing-due"
```

- [ ] **Step 4: Implement settlement**

Append to `lfg_core/closet_market_flow.py`:

```python
# --- fills ----------------------------------------------------------------------


def _brix_value(amount: Any) -> Decimal | None:
    if not isinstance(amount, dict):
        return None
    if str(amount.get("currency", "")).upper() != str(config.BRIX_CURRENCY_HEX).upper():
        return None
    if amount.get("issuer") != config.BRIX_ISSUER:
        return None
    try:
        return Decimal(str(amount.get("value")))
    except (InvalidOperation, ValueError):
        return None


def _fail(conn: sqlite3.Connection, fill: dict[str, Any], reason: str) -> bool:
    cms.update_fill(conn, fill["id"], state=cms.FAILED, error=reason)
    return True


def _apply_fill_outcome(conn: sqlite3.Connection, fill: dict[str, Any], out: TxOutcome | None, *, phase: str,
                        hash_field: str, success_state: str | None = None) -> str:
    if out is None:
        return "not_submitted"
    if out.state == "confirmed":
        fields: dict[str, Any] = {hash_field: out.tx_hash or cms.HASH_UNKNOWN, "error": None}
        if success_state is not None:
            fields["state"] = success_state
        cms.update_fill(conn, fill["id"], **fields)
    elif out.state == "unknown":
        cms.update_fill(conn, fill["id"], state=cms.INDETERMINATE, pending_phase=phase, pending_lls=out.last_ledger_seq)
    else:
        cms.update_fill(conn, fill["id"], attempts=int(fill["attempts"]) + 1, error=f"{phase} failed; will retry")
    return out.state


async def _funds(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    if fill["funds_source"] == cms.FUNDS_PAYMENT:
        return await _take_funds(conn, fill, deps)
    return await _escrow_funds(conn, fill, deps)


async def _take_funds(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    waited = deps.now_fn() - fill["created_ts"]
    if not fill["signed_txid"]:
        status = await deps.payload_status_fn(fill["payload_uuid"]) if fill["payload_uuid"] else None
        if status is None or not status.get("signed"):
            if (status or {}).get("expired") or waited > deps.signing_wait_seconds:
                return _fail(conn, fill, "payment request expired")
            return False
        txid = status.get("txid")
        if not txid:
            return False
        cms.update_fill(conn, fill["id"], signed_txid=txid)
        return True
    try:
        tx = await deps.get_tx_fn(fill["signed_txid"])
    except Exception:
        logging.warning(f"closet fill {fill['id']}: payment lookup failed: {traceback.format_exc()}")
        return False
    if not tx.get("validated"):
        if waited > deps.signing_wait_seconds:
            return _fail(conn, fill, "the payment never validated")
        return False
    meta = tx.get("meta") or {}
    body = _tx_body(tx)
    if meta.get("TransactionResult") != "tesSUCCESS":
        return _fail(conn, fill, f"payment failed: {meta.get('TransactionResult')}")
    if (
        body.get("TransactionType") != "Payment"
        or body.get("Destination") != deps.app_account
        or str(body.get("InvoiceID", "")).upper() != cms.invoice_id(fill["id"])
    ):
        logging.error(f"closet fill {fill['id']}: signed tx {fill['signed_txid']} is not this fill's payment")
        return _fail(conn, fill, "the signed transaction is not this purchase's payment")
    delivered = _brix_value(meta.get("delivered_amount"))
    if delivered is None or delivered <= 0:
        return _fail(conn, fill, "the payment delivered no BRIX")
    tx_hash = str(tx.get("hash") or body.get("hash") or fill["signed_txid"])
    sender = body.get("Account")
    try:
        if sender != fill["buyer"] or delivered < Decimal(fill["price_brix"]):
            reason = "payment signed by a different account" if sender != fill["buyer"] else "partial payment"
            cms.update_fill(conn, fill["id"], state=cms.REFUND_PENDING, payment_tx_hash=tx_hash, refund_to=sender,
                            refund_brix=cms.fmt_brix(delivered), error=reason)
            return True
        cms.claim_ask_for_payment(conn, fill["id"], tx_hash)
    except sqlite3.IntegrityError:
        return _fail(conn, fill, "this payment was already used for another purchase")
    return True


async def _escrow_funds(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    blocker = cms.fill_blocker(conn, fill["id"])
    if blocker is not None:
        cms.abort_unfunded_fill(conn, fill["id"], blocker)
        return True
    bid = cms.get_order(conn, fill["bid_order_id"])
    assert bid is not None and bid["escrow_owner_seq"] is not None
    out = await _submit(
        deps.escrow_finish_fn, bid["owner"], bid["escrow_owner_seq"], bid["condition"],
        deps.unseal_fn(bid["fulfillment_enc"]), cms.memo_tag("finish", fill["id"]),
    )
    verdict = _apply_fill_outcome(conn, fill, out, phase="finish", hash_field="escrow_finish_hash",
                                  success_state=cms.FUNDED)
    if verdict in ("confirmed", "unknown"):
        return True
    if verdict == "failed":
        try:
            node = await deps.get_escrow_fn(bid["owner"], bid["escrow_owner_seq"])
        except Exception:
            return False
        if node is None:
            cms.abort_unfunded_fill(conn, fill["id"], "the bid's escrow is gone (expired or cancelled)",
                                    bid_state=cms.EXPIRED)
            return True
    return False


async def _move(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    if fill["seller"] == fill["buyer"]:
        raise RuntimeError(f"closet fill {fill['id']} has seller == buyer")
    first, second = sorted((fill["seller"], fill["buyer"]))
    async with owner_lock.owner_lock(first), owner_lock.owner_lock(second):
        result = cms.move_asset(conn, fill["id"])
    return result in (cms.ASSET_MOVED, cms.REFUND_PENDING)


async def _pay(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    fid = fill["id"]
    changed = False
    if fill["forward_tx_hash"] is None:
        net = Decimal(fill["price_brix"]) - Decimal(fill["fee_brix"])
        if net <= 0:
            cms.update_fill(conn, fid, forward_tx_hash=cms.HASH_NOTHING_DUE)
        else:
            out = await _submit(deps.payment_fn, fill["seller"], cms.fmt_brix(net), cms.memo_tag("forward", fid),
                                memos.ACTION_CLOSET_FORWARD)
            verdict = _apply_fill_outcome(conn, fill, out, phase="forward", hash_field="forward_tx_hash")
            if verdict != "confirmed":
                return verdict == "unknown"
        changed = True
        fill = cms.get_fill(conn, fid) or fill
    if Decimal(fill["overshoot_brix"]) > 0 and fill["overshoot_tx_hash"] is None:
        out = await _submit(deps.payment_fn, fill["buyer"], fill["overshoot_brix"], cms.memo_tag("overshoot", fid),
                            memos.ACTION_CLOSET_REFUND)
        verdict = _apply_fill_outcome(conn, fill, out, phase="overshoot", hash_field="overshoot_tx_hash")
        if verdict != "confirmed":
            return changed or verdict == "unknown"
    cms.update_fill(conn, fid, state=cms.PAID, error=None)
    return True


async def _mirror(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    """Re-sync each side's Closet token from the (authoritative) DB. A full
    overwrite from current DB state is idempotent, so every error — including
    an indeterminate modify — is simply retried."""
    changed = False
    for side in ("seller", "buyer"):
        if fill[f"{side}_mirrored"]:
            continue
        owner = fill[side]
        try:
            async with owner_lock.owner_lock(owner):
                await deps.mirror_fn(conn, owner)
        except closet_token.ClosetMirrorError:
            pass  # the modify committed; only the token-record mirror write failed
        except Exception:
            logging.warning(f"closet fill {fill['id']}: mirror for {owner} failed: {traceback.format_exc()}")
            cms.update_fill(conn, fill["id"], attempts=int(fill["attempts"]) + 1,
                            error=f"Closet update for {owner} failed; will retry")
            continue
        cms.mark_side_mirrored(conn, fill["id"], side)
        changed = True
    return changed


async def _refund(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    to = fill["refund_to"] or fill["buyer"]
    value = fill["refund_brix"] or cms.fill_in_amount(fill)
    if Decimal(value) <= 0:
        cms.update_fill(conn, fill["id"], state=cms.REFUNDED, refund_tx_hash=cms.HASH_NOTHING_DUE)
        return True
    out = await _submit(deps.payment_fn, to, value, cms.memo_tag("refund", fill["id"]), memos.ACTION_CLOSET_REFUND)
    verdict = _apply_fill_outcome(conn, fill, out, phase="refund", hash_field="refund_tx_hash",
                                  success_state=cms.REFUNDED)
    return verdict in ("confirmed", "unknown")


_PHASE_ROLLBACK = {
    "finish": cms.FUNDS_PENDING,
    "forward": cms.ASSET_MOVED,
    "overshoot": cms.ASSET_MOVED,
    "refund": cms.REFUND_PENDING,
}


async def _resolve_fill_phase(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    phase = fill["pending_phase"]
    if phase not in _PHASE_ROLLBACK:
        logging.error(f"closet fill {fill['id']}: indeterminate with unknown phase {phase!r}")
        return False
    verdict, tx_hash = await _phase_outcome(deps, cms.memo_tag(phase, fill["id"]), fill["pending_lls"])
    if verdict == "wait":
        return False
    fields: dict[str, Any] = {"state": _PHASE_ROLLBACK[phase], "pending_phase": None, "pending_lls": None}
    if verdict == "landed":
        if phase == "finish":
            fields.update(state=cms.FUNDED, escrow_finish_hash=tx_hash)
        elif phase == "refund":
            fields.update(state=cms.REFUNDED, refund_tx_hash=tx_hash)
        else:
            fields[f"{phase}_tx_hash"] = tx_hash
    cms.update_fill(conn, fill["id"], **fields)
    return True


_STEPS: dict[str, Callable[[sqlite3.Connection, dict[str, Any], ClosetMarketDeps], Awaitable[bool]]] = {
    cms.FUNDS_PENDING: _funds,
    cms.FUNDED: _move,
    cms.ASSET_MOVED: _pay,
    cms.PAID: _mirror,
    cms.REFUND_PENDING: _refund,
    cms.INDETERMINATE: _resolve_fill_phase,
}


async def settle_fill(fill_id: str, deps: ClosetMarketDeps) -> str | None:
    """Advance one fill as far as it can go right now. Serialized per fill
    (inline kick + sweep may race); returns the state it stopped at."""
    async with owner_lock.owner_lock(f"closet-fill:{fill_id}"):
        state: str | None = None
        for _ in range(MAX_STEPS):
            conn = deps.conn_factory()
            try:
                current = cms.get_fill(conn, fill_id)
                if current is None:
                    return None
                state = str(current["state"])
                if state in cms.TERMINAL_FILL_STATES:
                    return state
                progressed = await _STEPS[state](conn, current, deps)
                after = cms.get_fill(conn, fill_id)
                assert after is not None
                if progressed:
                    _journal(deps, "fill", after)
                state = str(after["state"])
                if not progressed or state in cms.TERMINAL_FILL_STATES:
                    return state
            finally:
                conn.close()
        return state
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_flow_settle.py tests/test_closet_market_flow_bids.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add lfg_core/closet_market_flow.py lfg_core/closet_market_store.py tests/test_closet_market_flow_settle.py
git commit -m "feat(closet-market): fill settlement — funds, atomic move, forward, mirror, refunds (#443)"
```

---

### Task 10: Service I — gates, deps, book/keys/mine, asks, holder fills, fill status

**Files:**
- Modify: `lfg_service/app.py`:
  - imports
  - new "Closet Market (#443)" section placed after `sweep_pending_closet_accepts`
  - route registration in `create_app` next to the `/api/shop/*` routes
- Test: `tests/test_closet_market_api.py`

**Interfaces:**
- Consumes: Tasks 1, 4, 8, 9
- Produces:
  - Gates: `require_closet_market`, `require_closet_settleable`
  - Plumbing:
    - `_closet_conn() -> sqlite3.Connection`
    - `async _closet_db(fn, *args, **kwargs)`
    - `_closet_market_deps() -> closet_market_flow.ClosetMarketDeps`
    - `async _closet_market_mirror(conn, owner) -> None`
    - `_schedule_closet_settle(fill_id: str) -> None`, `_schedule_closet_bid(order_id: str) -> None`
    - `_invalidate_closet_book() -> None`
  - Views: `_closet_order_public(order) -> dict`, `_closet_bid_view(order) -> dict`, `_closet_fill_view(fill, wallet) -> dict`
  - Helpers: `_known_trait(network, slot, value) -> bool`, `_closet_keys(network) -> list[dict]`
  - Handlers: `handle_closet_book`, `handle_closet_keys`, `handle_closet_orders_mine`, `handle_closet_ask_create`, `handle_closet_ask_cancel`, `handle_closet_bid_fill`, `handle_closet_fill_status`
  - Routes:
    - `GET /api/closet/book`
    - `GET /api/closet/keys`
    - `GET /api/closet/orders/mine`
    - `POST /api/closet/ask`
    - `DELETE /api/closet/ask/{order_id}`
    - `POST /api/closet/bid/{order_id}/fill`
    - `GET /api/closet/fill/{fill_id}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_api.py
import asyncio
import json

import pytest
from aiohttp.test_utils import make_mocked_request
from cryptography.fernet import Fernet

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core.nft_index import init_db as init_onchain_db
from webapp import mock_economy
from lfg_service import app as server

ME, BIDDER = "rMeSeller", "rBidder"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _json(resp):
    return json.loads(resp.body)


@pytest.fixture
def closet_env(tmp_path, monkeypatch):
    path = str(tmp_path / "onchain_testnet.db")
    c = init_onchain_db(path)
    es.init_economy_schema(c)
    for owner in (ME, BIDDER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, ME, [("Head", "Crown", 1)], [])
    c.commit()
    c.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", path)
    for name, value in {
        "XRPL_NETWORK": "testnet", "ECONOMY_NETWORK": "testnet", "ECONOMY_ENABLED": True,
        "CLOSET_MARKET_ENABLED": True, "CLOSET_MARKET_ENC_KEY": Fernet.generate_key().decode(),
        "CLOSET_MARKET_FEE_BPS": 700, "WEBAPP_DEV_MODE": True,
    }.items():
        monkeypatch.setattr(server.config, name, value)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", ME)
    scheduled = []
    monkeypatch.setattr(server, "_schedule_closet_settle", lambda fid: scheduled.append(("fill", fid)))
    monkeypatch.setattr(server, "_schedule_closet_bid", lambda oid: scheduled.append(("bid", oid)))

    async def no_token(user):
        return None

    monkeypatch.setattr(server, "_push_token", no_token)
    server._CLOSET_BOOK_CACHE.clear()
    yield {"path": path, "scheduled": scheduled}
    server._CLOSET_BOOK_CACHE.clear()


def _db(env):
    c = init_onchain_db(env["path"])
    es.init_economy_schema(c)
    return c


def _req(method, path, body=None, match_info=None):
    req = make_mocked_request(method, path, match_info=match_info or {})

    async def _body():
        return body or {}

    req.json = _body  # type: ignore[method-assign]
    return req


def _open_bid(env, owner=BIDDER, price="10"):
    c = _db(env)
    bid = cms.create_pending_bid(c, owner=owner, slot="Head", value="Crown", price_brix=price, platform=None,
                                 condition="C", fulfillment_enc="S", cancel_after=9, payload_uuid=None,
                                 xumm_url=None, qr_url=None, push=None)
    bid = cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E" + bid["id"], escrow_owner_seq=3)
    c.close()
    return bid


def test_ask_create_encumbers_and_invalidates_book(closet_env):
    _run(server.handle_closet_book(_req("GET", "/api/closet/book")))
    assert server._CLOSET_BOOK_CACHE
    resp = _run(server.handle_closet_ask_create(_req("POST", "/api/closet/ask",
                                                     {"slot": "Head", "value": "Crown", "price_brix": "12.50"})))
    assert resp.status == 200
    body = _json(resp)
    assert body["order"]["price_brix"] == "12.5" and body["fill"] is None
    assert "fulfillment_enc" not in body["order"]
    assert not server._CLOSET_BOOK_CACHE
    rows = _json(_run(server.handle_closet_book(_req("GET", "/api/closet/book"))))["rows"]
    assert rows[0]["best_ask_brix"] == "12.5"


def test_ask_create_rejects_bad_price_and_unheld(closet_env):
    bad = _run(server.handle_closet_ask_create(_req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "0"})))
    assert bad.status == 400
    unheld = _run(server.handle_closet_ask_create(_req("POST", "/", {"slot": "Head", "value": "Tiara", "price_brix": "1"})))
    assert unheld.status == 409 and _json(unheld)["code"] == "not_available"


def test_ask_crossing_a_bid_schedules_settlement(closet_env):
    _open_bid(closet_env, price="10")
    body = _json(_run(server.handle_closet_ask_create(_req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "9"}))))
    assert body["fill"]["state"] == "pending" and body["fill"]["role"] == "seller"
    assert closet_env["scheduled"] == [("fill", body["fill"]["id"])]


def test_kill_switch_blocks_new_orders_but_not_cancel(closet_env, monkeypatch):
    order = _json(_run(server.handle_closet_ask_create(_req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "3"}))))["order"]
    monkeypatch.setattr(server.config, "CLOSET_MARKET_ENABLED", False)
    blocked = _run(server.handle_closet_ask_create(_req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "3"})))
    assert blocked.status == 403 and _json(blocked)["code"] == "closet_market_disabled"
    resp = _run(server.handle_closet_ask_cancel(_req("DELETE", "/", match_info={"order_id": order["id"]})))
    assert resp.status == 200 and _json(resp)["order"]["state"] == "cancelled"


def test_orders_mine_lists_bids_on_my_traits(closet_env):
    bid = _open_bid(closet_env)
    body = _json(_run(server.handle_closet_orders_mine(_req("GET", "/api/closet/orders/mine"))))
    assert [b["id"] for b in body["bids_on_my_traits"]] == [bid["id"]]
    assert body["bids_on_my_traits"][0]["source"] == "closet"


def test_fill_bid_and_status_are_party_only(closet_env, monkeypatch):
    bid = _open_bid(closet_env)
    resp = _run(server.handle_closet_bid_fill(_req("POST", "/", match_info={"order_id": bid["id"]})))
    assert resp.status == 200
    view = _json(resp)
    assert (view["kind"], view["state"], view["role"]) == ("closet_fill", "pending", "seller")
    status = _run(server.handle_closet_fill_status(_req("GET", "/", match_info={"fill_id": view["id"]})))
    assert status.status == 200
    monkeypatch.setattr(mock_economy, "DEV_OWNER", "rStranger")
    denied = _run(server.handle_closet_fill_status(_req("GET", "/", match_info={"fill_id": view["id"]})))
    assert denied.status == 404


def test_fill_view_state_mapping():
    base = {"id": "F", "seller": "rS", "buyer": "rB", "slot": "Head", "value": "Crown", "price_brix": "1",
            "fee_brix": "0", "overshoot_brix": "0", "error": None, "qr_url": None, "xumm_url": None, "push": None,
            "funds_source": "payment", "signed_txid": None}
    assert server._closet_fill_view({**base, "state": "funds_pending"}, "rB")["state"] == "awaiting_signature"
    assert server._closet_fill_view({**base, "state": "funds_pending", "signed_txid": "T"}, "rB")["state"] == "pending"
    assert server._closet_fill_view({**base, "state": "asset_moved"}, "rB")["state"] == "done"
    assert server._closet_fill_view({**base, "state": "refunded"}, "rB")["state"] == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_api.py -v`
Expected: FAIL with `AttributeError: module 'lfg_service.app' has no attribute '_schedule_closet_settle'`

- [ ] **Step 3: Implement**

Add `closet_market_flow, closet_market_store, crypto_condition, owner_lock` to the `from lfg_core import (...)` block (skip any already present). Then add the section:

```python
# ---- Closet Market (#443) ----------------------------------------------------
#
# New orders are gated on config.closet_market_enabled(); cancel, status and
# settlement only on closet_market_settleable(), so the kill switch never
# strands BRIX in an escrow. The DB (closet_market_store) is authoritative;
# settlement is closet_market_flow, run inline and by _settlement_sweep_loop.


def _closet_market_disabled_response():
    return web.json_response(
        {"error": "the Closet market is not enabled", "code": "closet_market_disabled"}, status=403
    )


def require_closet_market(handler):
    @functools.wraps(handler)
    async def wrapper(request):
        if not config.closet_market_enabled():
            return _closet_market_disabled_response()
        return await handler(request)

    return wrapper


def require_closet_settleable(handler):
    @functools.wraps(handler)
    async def wrapper(request):
        if not config.closet_market_settleable():
            return _closet_market_disabled_response()
        return await handler(request)

    return wrapper


def _closet_conn() -> sqlite3.Connection:
    conn = nft_index.init_db(nft_index.index_db_path(config.ECONOMY_NETWORK))
    economy_store.init_economy_schema(conn)
    return conn


async def _closet_db(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one closet_market_store call on a fresh connection off the loop."""

    def run() -> Any:
        conn = _closet_conn()
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    return await asyncio.get_event_loop().run_in_executor(None, run)


_CLOSET_BOOK_CACHE: dict[tuple[str, str | None, str | None], tuple[float, dict[str, Any]]] = {}
_CLOSET_BOOK_TTL = 60.0


def _invalidate_closet_book() -> None:
    _CLOSET_BOOK_CACHE.clear()


_ORDER_ERROR_STATUS = {
    "not_found": 404, "not_open": 409, "not_available": 409, "bid_exists": 409, "self_cross": 400,
    "closet_required": 403,
}


def _order_error_response(exc: closet_market_store.OrderError) -> web.Response:
    if exc.code == "closet_required":  # the client keys promptClosetRequired() off this exact message
        return web.json_response({"error": "closet_required", "code": "closet_required"}, status=403)
    return web.json_response({"error": str(exc), "code": exc.code}, status=_ORDER_ERROR_STATUS.get(exc.code, 400))


def _parse_brix_price(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return market_ops.validate_brix_value(raw)
    except (TypeError, ValueError):
        return None


_ORDER_PUBLIC_FIELDS = ("id", "side", "slot", "value", "price_brix", "state", "created_ts", "cancel_after", "error")


def _closet_order_public(order: dict[str, Any]) -> dict[str, Any]:
    return {k: order.get(k) for k in _ORDER_PUBLIC_FIELDS}


_BID_VIEW_STATE = {
    closet_market_store.OPEN: "done", closet_market_store.MATCHED: "done", closet_market_store.FILLED: "done",
    closet_market_store.CANCELLING: "pending", closet_market_store.CANCELLED: "failed",
    closet_market_store.EXPIRED: "failed",
}


def _closet_bid_view(order: dict[str, Any]) -> dict[str, Any]:
    """Market-flow-compatible session dict (states the client's marketFlow knows)."""
    if order["state"] == closet_market_store.PENDING_ESCROW:
        state = "pending" if order["signed_txid"] else "awaiting_signature"
    else:
        state = _BID_VIEW_STATE[order["state"]]
    return {
        **_closet_order_public(order), "kind": "closet_bid", "state": state, "order_state": order["state"],
        "qr_url": order["qr_url"], "xumm_url": order["xumm_url"], "push": order["push"],
    }


def _closet_fill_view(fill: dict[str, Any], wallet: str) -> dict[str, Any]:
    s = fill["state"]
    if s == closet_market_store.FUNDS_PENDING and fill["funds_source"] == closet_market_store.FUNDS_PAYMENT and not fill["signed_txid"]:
        state = "awaiting_signature"
    elif s in (closet_market_store.ASSET_MOVED, closet_market_store.PAID, closet_market_store.MIRRORED):
        state = "done"
    elif s in (closet_market_store.REFUNDED, closet_market_store.FAILED):
        state = "failed"
    else:
        state = "pending"
    return {
        "id": fill["id"], "kind": "closet_fill", "state": state, "fill_state": s,
        "role": "buyer" if wallet == fill["buyer"] else "seller",
        **{k: fill[k] for k in ("slot", "value", "price_brix", "fee_brix", "overshoot_brix", "error", "qr_url", "xumm_url", "push")},
    }


def _known_trait(network: str, slot: str, value: str) -> bool:
    conn = sqlite3.connect(db_path.app_db_path(network))
    try:
        rarity.ensure_schema(conn)
        return conn.execute(
            "SELECT 1 FROM trait_rarity WHERE network = ? AND category = ? AND trait = ? LIMIT 1",
            (network, slot, value),
        ).fetchone() is not None
    finally:
        conn.close()


def _closet_keys(network: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path.app_db_path(network))
    try:
        rarity.ensure_schema(conn)
        rows = conn.execute(
            "SELECT DISTINCT category, trait FROM trait_rarity WHERE network = ? AND category != ? "
            "AND trait != 'None' ORDER BY category, trait",
            (network, rarity.BODY_CATEGORY),
        ).fetchall()
    finally:
        conn.close()
    cfg = trait_config.get_config()
    return [{"slot": s, "value": v, "image_url": _trait_image_url(cfg, s, v)} for s, v in rows]


async def _closet_market_mirror(conn: sqlite3.Connection, owner: str) -> None:
    """Re-sync one owner's Closet token from the DB (settlement step 4)."""
    deps = economy_api.build_settlement_deps(conn)
    assets = [(s, v, c) for (o, s, v, c) in economy_store.read_closet_assets(conn) if o == owner and c > 0]
    await closet_token.sync_closet(
        conn, owner, assets, [], upload_fn=deps.closet_upload_fn, modify_fn=deps.closet_modify_fn
    )


def _closet_market_deps() -> closet_market_flow.ClosetMarketDeps:
    return closet_market_flow.ClosetMarketDeps(
        conn_factory=_closet_conn,
        app_account=config.SIGNING_ACCOUNT,
        fee_bps=config.CLOSET_MARKET_FEE_BPS,
        payload_status_fn=xumm_ops.get_payload_status,
        get_tx_fn=xrpl_ops.get_tx,
        get_escrow_fn=xrpl_ops.get_escrow,
        escrow_finish_fn=xrpl_ops.escrow_finish,
        escrow_cancel_fn=xrpl_ops.escrow_cancel,
        payment_fn=xrpl_ops.app_brix_payment,
        find_txs_fn=xrpl_ops.find_app_txs_by_memo,
        ledger_index_fn=xrpl_ops.current_validated_ledger_index,
        mirror_fn=_closet_market_mirror,
        unseal_fn=crypto_condition.unseal,
        records_dir=config.ECONOMY_RECORDS_DIR,
    )


_closet_tasks: dict[str, asyncio.Task[Any]] = {}


def _schedule_closet(key: str, make_coro: Callable[[], Awaitable[Any]]) -> None:
    for done in [k for k, t in _closet_tasks.items() if t.done()]:
        del _closet_tasks[done]
    if key in _closet_tasks:
        return

    async def run() -> None:
        try:
            await make_coro()
        except Exception:
            logging.error(f"closet market task {key} crashed: {traceback.format_exc()}")
        finally:
            _invalidate_closet_book()

    _closet_tasks[key] = asyncio.get_event_loop().create_task(run())


def _schedule_closet_settle(fill_id: str) -> None:
    _schedule_closet(f"fill:{fill_id}", lambda: closet_market_flow.settle_fill(fill_id, _closet_market_deps()))


def _schedule_closet_bid(order_id: str) -> None:
    async def go() -> None:
        fill_id = await closet_market_flow.advance_bid(order_id, _closet_market_deps())
        if fill_id:
            _schedule_closet_settle(fill_id)

    _schedule_closet(f"bid:{order_id}", go)


@require_closet_market
async def handle_closet_book(request):
    """GET /api/closet/book[?slot=&value=] — public. Both params: price
    levels for one key; otherwise one summary row per key with open orders."""
    slot = request.query.get("slot") or None
    value = request.query.get("value") or None
    key = (config.ECONOMY_NETWORK, slot, value)
    now_mono = time.monotonic()
    cached = _CLOSET_BOOK_CACHE.get(key)
    if cached is not None and now_mono - cached[0] < _CLOSET_BOOK_TTL:
        return web.json_response(cached[1])
    if slot is not None and value is not None:
        body = await _closet_db(closet_market_store.book_levels, slot, value)
        summary = await _closet_db(closet_market_store.book_summary, slot, value)
        body["summary"] = summary[0] if summary else None
    else:
        body = {"rows": await _closet_db(closet_market_store.book_summary, slot, value)}
    _CLOSET_BOOK_CACHE[key] = (now_mono, body)
    return web.json_response(body)


@require_closet_market
async def handle_closet_keys(request):
    rows = await asyncio.get_event_loop().run_in_executor(None, _closet_keys, config.ECONOMY_NETWORK)
    return web.json_response({"keys": rows})


@require_closet_settleable
@require_wallet
async def handle_closet_orders_mine(request):
    wallet = request["wallet"]

    def run(conn: sqlite3.Connection) -> dict[str, Any]:
        data = closet_market_store.orders_for_owner(conn, wallet)
        data["bids_on_my_traits"] = (
            closet_market_store.bids_on_holdings(conn, wallet) if config.closet_market_enabled() else []
        )
        return data

    return web.json_response(await _closet_db(run))


@require_closet_market
@require_wallet
async def handle_closet_ask_create(request):
    wallet, user = request["wallet"], request["user"]
    body = await _json_body(request)
    slot, value = body.get("slot"), body.get("value")
    price = _parse_brix_price(body.get("price_brix"))
    if not isinstance(slot, str) or not isinstance(value, str) or price is None:
        return web.json_response(
            {"error": "slot, value and a valid price_brix are required", "code": "bad_request"}, status=400
        )
    try:
        async with owner_lock.owner_lock(wallet):
            order = await _closet_db(
                closet_market_store.create_ask, owner=wallet, slot=slot, value=value, price_brix=price,
                platform=_platform(user),
            )
    except closet_market_store.OrderError as exc:
        return _order_error_response(exc)
    fill = await _closet_db(closet_market_store.cross_incoming, order["id"], fee_bps=config.CLOSET_MARKET_FEE_BPS)
    _invalidate_closet_book()
    if fill:
        _schedule_closet_settle(fill["id"])
    return web.json_response(
        {"order": _closet_order_public(order), "fill": _closet_fill_view(fill, wallet) if fill else None}
    )


@require_closet_settleable
@require_wallet
async def handle_closet_ask_cancel(request):
    try:
        order = await _closet_db(closet_market_store.cancel_ask, request.match_info["order_id"], request["wallet"])
    except closet_market_store.OrderError as exc:
        return _order_error_response(exc)
    _invalidate_closet_book()
    return web.json_response({"order": _closet_order_public(order)})


@require_closet_market
@require_wallet
async def handle_closet_bid_fill(request):
    wallet, user = request["wallet"], request["user"]
    try:
        async with owner_lock.owner_lock(wallet):
            fill = await _closet_db(
                closet_market_store.fill_bid, request.match_info["order_id"], wallet,
                fee_bps=config.CLOSET_MARKET_FEE_BPS, platform=_platform(user),
            )
    except closet_market_store.OrderError as exc:
        return _order_error_response(exc)
    _invalidate_closet_book()
    _schedule_closet_settle(fill["id"])
    return web.json_response(_closet_fill_view(fill, wallet))


@require_closet_settleable
@require_wallet
async def handle_closet_fill_status(request):
    wallet = request["wallet"]
    fill = await _closet_db(closet_market_store.get_fill, request.match_info["fill_id"])
    if fill is None or wallet not in (fill["seller"], fill["buyer"]):
        return web.json_response({"error": "fill not found", "code": "not_found"}, status=404)
    if fill["state"] not in closet_market_store.TERMINAL_FILL_STATES:
        _schedule_closet_settle(fill["id"])
    return web.json_response(_closet_fill_view(fill, wallet))
```

Register the routes in `create_app()` after the `/api/shop/*` routes (literal paths before parameterised ones):

```python
    app.router.add_get("/api/closet/book", handle_closet_book)
    app.router.add_get("/api/closet/keys", handle_closet_keys)
    app.router.add_get("/api/closet/orders/mine", handle_closet_orders_mine)
    app.router.add_post("/api/closet/ask", handle_closet_ask_create)
    app.router.add_delete("/api/closet/ask/{order_id}", handle_closet_ask_cancel)
    app.router.add_post("/api/closet/bid/{order_id}/fill", handle_closet_bid_fill)
    app.router.add_get("/api/closet/fill/{fill_id}", handle_closet_fill_status)
```

(`Awaitable`/`Callable` come from `collections.abc`; add them to the existing import if missing. `rarity`, `db_path`, `trait_config`, `market_ops`, `economy_api`, `closet_token` are already imported by app.py.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_api.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_closet_market_api.py
git commit -m "feat(closet-market): book, keys, mine, asks and holder fills endpoints (#443)"
```

---

### Task 11: Service II — place/poll/cancel bids, buy an ask

**Files:**
- Modify: `lfg_service/app.py` (Closet Market section + routes)
- Test: `tests/test_closet_market_api_signing.py`

**Interfaces:**
- Consumes:
  - Task 7 `xumm_ops.create_closet_bid_payload` / `create_closet_buy_payload`
  - `xrpl_ops.get_trustline_state` → `(TrustlineState, Decimal | None)`
  - `brix_payment.detect_payment_path`
  - Task 10 plumbing
- Produces:
  - Handlers: `handle_closet_bid_create`, `handle_closet_bid_status`, `handle_closet_bid_cancel`, `handle_closet_ask_buy`
  - Routes:
    - `POST /api/closet/bid`
    - `GET /api/closet/bid/{order_id}`
    - `DELETE /api/closet/bid/{order_id}`
    - `POST /api/closet/ask/{order_id}/buy`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_api_signing.py
from decimal import Decimal

from lfg_core import closet_market_store as cms
from lfg_core import crypto_condition
from lfg_service import app as server
from tests import test_closet_market_api as api_tests
from tests.test_closet_market_api import BIDDER, ME, _db, _json, _open_bid, _req, _run

closet_env = api_tests.closet_env  # re-export the fixture (a direct import trips ruff F811)


def _trustline(monkeypatch, state="PRESENT", balance="50"):
    async def fake(wallet, currency, issuer):
        return getattr(server.xrpl_ops.TrustlineState, state), (Decimal(balance) if balance is not None else None)

    monkeypatch.setattr(server.xrpl_ops, "get_trustline_state", fake)


def _capture_bid_payload(monkeypatch, result=None):
    seen = {}

    async def fake(account, amount, destination, condition, cancel_after, **kwargs):
        seen.update(account=account, amount=amount, destination=destination, condition=condition,
                    cancel_after=cancel_after)
        return result if result is not None else {"uuid": "BU", "xumm_url": "https://xumm.app/sign/BU", "qr_url": "q", "push": "sent"}

    monkeypatch.setattr(server.xumm_ops, "create_closet_bid_payload", fake)
    return seen


def test_bid_create_builds_escrow_payload_and_seals_fulfillment(closet_env, monkeypatch):
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: True)
    _trustline(monkeypatch)
    seen = _capture_bid_payload(monkeypatch)
    resp = _run(server.handle_closet_bid_create(_req("POST", "/api/closet/bid", {"slot": "Head", "value": "Tiara", "price_brix": "10"})))
    assert resp.status == 200
    view = _json(resp)
    assert (view["kind"], view["state"], view["xumm_url"]) == ("closet_bid", "awaiting_signature", "https://xumm.app/sign/BU")
    assert seen["account"] == ME and seen["destination"] == server.config.SIGNING_ACCOUNT
    assert seen["amount"]["value"] == "10"
    c = _db(closet_env)
    row = cms.get_order(c, view["id"])
    c.close()
    assert row["condition"] == seen["condition"] and row["cancel_after"] == seen["cancel_after"]
    fulfillment = crypto_condition.unseal(row["fulfillment_enc"])
    assert crypto_condition.condition_hex(bytes.fromhex(fulfillment[8:])) == seen["condition"]


def test_bid_create_refusals(closet_env, monkeypatch):
    body = {"slot": "Head", "value": "Tiara", "price_brix": "10"}
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: False)
    assert _run(server.handle_closet_bid_create(_req("POST", "/", body))).status == 404
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: True)
    _trustline(monkeypatch, state="ABSENT", balance=None)
    resp = _run(server.handle_closet_bid_create(_req("POST", "/", body)))
    assert resp.status == 409 and _json(resp)["code"] == "trustline_required"
    _trustline(monkeypatch, state="UNKNOWN", balance=None)
    assert _run(server.handle_closet_bid_create(_req("POST", "/", body))).status == 503
    _trustline(monkeypatch, balance="9.99")
    resp = _run(server.handle_closet_bid_create(_req("POST", "/", body)))
    assert resp.status == 409 and _json(resp)["code"] == "insufficient_brix"


def test_bid_create_refuses_a_second_live_bid(closet_env, monkeypatch):
    monkeypatch.setattr(server, "_known_trait", lambda net, s, v: True)
    _trustline(monkeypatch)
    _capture_bid_payload(monkeypatch)
    _open_bid(closet_env, owner=ME)
    resp = _run(server.handle_closet_bid_create(_req("POST", "/", {"slot": "Head", "value": "Crown", "price_brix": "10"})))
    assert resp.status == 409 and _json(resp)["code"] == "bid_exists"


def test_bid_status_advances_pending_bids_inline(closet_env, monkeypatch):
    c = _db(closet_env)
    bid = cms.create_pending_bid(c, owner=ME, slot="Head", value="Tiara", price_brix="1", platform=None, condition="C",
                                 fulfillment_enc="S", cancel_after=9, payload_uuid="BU", xumm_url="x", qr_url="q", push=None)
    c.close()
    calls = []

    async def fake_advance(order_id, deps):
        calls.append(order_id)
        return "FILL1"

    monkeypatch.setattr(server.closet_market_flow, "advance_bid", fake_advance)
    monkeypatch.setattr(server, "_closet_market_deps", lambda: None)
    resp = _run(server.handle_closet_bid_status(_req("GET", "/", match_info={"order_id": bid["id"]})))
    assert resp.status == 200 and calls == [bid["id"]]
    assert ("fill", "FILL1") in closet_env["scheduled"]


def test_bid_cancel_moves_to_cancelling_and_schedules(closet_env):
    bid = _open_bid(closet_env, owner=ME)
    resp = _run(server.handle_closet_bid_cancel(_req("DELETE", "/", match_info={"order_id": bid["id"]})))
    assert resp.status == 200
    assert _json(resp)["order_state"] == "cancelling"
    assert ("bid", bid["id"]) in closet_env["scheduled"]
    other = _open_bid(closet_env, owner=BIDDER)
    denied = _run(server.handle_closet_bid_cancel(_req("DELETE", "/", match_info={"order_id": other["id"]})))
    assert denied.status == 404


def test_ask_buy_builds_invoice_payment(closet_env, monkeypatch):
    c = _db(closet_env)
    ask = cms.create_ask(c, owner=ME, slot="Head", value="Crown", price_brix="5", platform=None)
    c.close()
    monkeypatch.setattr(server.mock_economy, "DEV_OWNER", BIDDER)

    async def xrp_path(wallet, amount, **kwargs):
        return "XRP", "2.5"

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", xrp_path)
    seen = {}

    async def fake_buy(account, amount, destination, invoice_id, **kwargs):
        seen.update(account=account, invoice_id=invoice_id, send_max=kwargs.get("send_max_drops"))
        return {"uuid": "PU", "xumm_url": "https://xumm.app/sign/PU", "qr_url": "q", "push": None}

    monkeypatch.setattr(server.xumm_ops, "create_closet_buy_payload", fake_buy)
    resp = _run(server.handle_closet_ask_buy(_req("POST", "/", match_info={"order_id": ask["id"]})))
    assert resp.status == 200
    view = _json(resp)
    assert (view["state"], view["role"], view["pay_with"]) == ("awaiting_signature", "buyer", "XRP")
    assert seen["account"] == BIDDER and seen["send_max"] == "2500000"
    assert seen["invoice_id"] == cms.invoice_id(view["id"])


def test_ask_buy_own_listing_is_refused(closet_env, monkeypatch):
    c = _db(closet_env)
    ask = cms.create_ask(c, owner=ME, slot="Head", value="Crown", price_brix="5", platform=None)
    c.close()

    async def brix_path(wallet, amount, **kwargs):
        return "BRIX", amount

    monkeypatch.setattr(server.brix_payment, "detect_payment_path", brix_path)
    resp = _run(server.handle_closet_ask_buy(_req("POST", "/", match_info={"order_id": ask["id"]})))
    assert resp.status == 400 and _json(resp)["code"] == "self_cross"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_api_signing.py -v`
Expected: FAIL with `AttributeError: module 'lfg_service.app' has no attribute 'handle_closet_bid_create'`

- [ ] **Step 3: Implement**

Append to the Closet Market section of `lfg_service/app.py`:

```python
@require_closet_market
@require_wallet
async def handle_closet_bid_create(request):
    """POST /api/closet/bid {slot, value, price_brix}: one Xaman signature
    locks the BRIX in a TokenEscrow to the app wallet. The fulfillment is
    generated here and stored sealed; the bid opens once the escrow is
    verified on-ledger (GET /api/closet/bid/{id})."""
    wallet, user = request["wallet"], request["user"]
    body = await _json_body(request)
    slot, value = body.get("slot"), body.get("value")
    price = _parse_brix_price(body.get("price_brix"))
    if not isinstance(slot, str) or not isinstance(value, str) or price is None:
        return web.json_response(
            {"error": "slot, value and a valid price_brix are required", "code": "bad_request"}, status=400
        )
    network = config.ECONOMY_NETWORK
    loop = asyncio.get_event_loop()
    if not await loop.run_in_executor(None, _known_trait, network, slot, value):
        return web.json_response({"error": "unknown trait", "code": "unknown_trait"}, status=404)
    if not await loop.run_in_executor(None, _closet_active, network, wallet):
        return web.json_response({"error": "closet_required", "code": "closet_required"}, status=403)
    if await _closet_db(closet_market_store.has_live_bid, wallet, slot, value):
        return web.json_response(
            {"error": "you already have a live bid on this trait — cancel it first", "code": "bid_exists"},
            status=409,
        )
    state, balance = await xrpl_ops.get_trustline_state(wallet, config.BRIX_CURRENCY_HEX, config.BRIX_ISSUER)
    if state == xrpl_ops.TrustlineState.ABSENT:
        return web.json_response({"error": "a BRIX trustline is required", "code": "trustline_required"}, status=409)
    if state != xrpl_ops.TrustlineState.PRESENT or balance is None:
        return web.json_response(
            {"error": "could not read your BRIX balance — try again", "code": "balance_unavailable"}, status=503
        )
    if balance < Decimal(price):
        return web.json_response(
            {"error": f"you hold {balance} BRIX; this bid locks {price}", "code": "insufficient_brix"}, status=409
        )
    preimage = crypto_condition.new_preimage()
    condition = crypto_condition.condition_hex(preimage)
    sealed = crypto_condition.seal(crypto_condition.fulfillment_hex(preimage))
    cancel_after = int(time.time()) - xrpl_ops.RIPPLE_EPOCH_OFFSET + config.CLOSET_BID_TTL_SECONDS
    payload = await xumm_ops.create_closet_bid_payload(
        wallet, market_ops.brix_amount_dict(price), config.SIGNING_ACCOUNT, condition, cancel_after,
        return_url=xumm_ops.discord_return_url(body.get("guild_id"), body.get("channel_id")),
        user_token=await _push_token(user),
        platform=memos.platform_for_surface(_platform(user)),
    )
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    try:
        order = await _closet_db(
            closet_market_store.create_pending_bid, owner=wallet, slot=slot, value=value, price_brix=price,
            platform=_platform(user), condition=condition, fulfillment_enc=sealed, cancel_after=cancel_after,
            payload_uuid=payload.get("uuid"), xumm_url=payload.get("xumm_url"), qr_url=payload.get("qr_url"),
            push=payload.get("push"),
        )
    except closet_market_store.OrderError as exc:
        if payload.get("uuid"):
            await xumm_ops.cancel_xumm_payload(payload["uuid"])
        return _order_error_response(exc)
    return web.json_response(_closet_bid_view(order))


@require_closet_settleable
@require_wallet
async def handle_closet_bid_status(request):
    wallet, order_id = request["wallet"], request.match_info["order_id"]
    order = await _closet_db(closet_market_store.get_order, order_id)
    if order is None or order["side"] != closet_market_store.SIDE_BID or order["owner"] != wallet:
        return web.json_response({"error": "bid not found", "code": "not_found"}, status=404)
    if order["state"] == closet_market_store.PENDING_ESCROW:
        fill_id = await closet_market_flow.advance_bid(order_id, _closet_market_deps())
        if fill_id:
            _schedule_closet_settle(fill_id)
        _invalidate_closet_book()
        order = await _closet_db(closet_market_store.get_order, order_id)
    elif order["state"] == closet_market_store.CANCELLING:
        _schedule_closet_bid(order_id)
    return web.json_response(_closet_bid_view(order))


@require_closet_settleable
@require_wallet
async def handle_closet_bid_cancel(request):
    order_id = request.match_info["order_id"]
    try:
        order = await _closet_db(
            closet_market_store.begin_cancel_bid, order_id, owner=request["wallet"], reason="user"
        )
    except closet_market_store.OrderError as exc:
        return _order_error_response(exc)
    _invalidate_closet_book()
    _schedule_closet_bid(order_id)
    return web.json_response(_closet_bid_view(order))


@require_closet_market
@require_wallet
async def handle_closet_ask_buy(request):
    """POST /api/closet/ask/{id}/buy: one Xaman signature pays the ask to the
    app wallet (InvoiceID = sha256(fill id)). Non-holders pay XRP via SendMax
    on the same Payment — no trustline needed, the BRIX lands at the app."""
    wallet, user = request["wallet"], request["user"]
    order_id = request.match_info["order_id"]
    ask = await _closet_db(closet_market_store.get_order, order_id)
    if ask is None or ask["side"] != closet_market_store.SIDE_ASK:
        return web.json_response({"error": "listing not found", "code": "not_found"}, status=404)
    if ask["state"] != closet_market_store.OPEN:
        return web.json_response({"error": "this trait was just sold or unlisted", "code": "not_open"}, status=409)
    if ask["owner"] == wallet:
        return web.json_response({"error": "you can't buy your own listing", "code": "self_cross"}, status=400)
    try:
        pay_with, quote = await brix_payment.detect_payment_path(
            wallet, ask["price_brix"], currency=config.BRIX_CURRENCY_HEX, issuer=config.BRIX_ISSUER
        )
    except RuntimeError:
        return web.json_response({"error": "pricing unavailable — try again", "code": "pricing_unavailable"}, status=503)
    try:
        fill = await _closet_db(
            closet_market_store.create_take_fill, order_id, wallet, fee_bps=config.CLOSET_MARKET_FEE_BPS,
            platform=_platform(user),
        )
    except closet_market_store.OrderError as exc:
        return _order_error_response(exc)
    payload = await xumm_ops.create_closet_buy_payload(
        wallet, market_ops.brix_amount_dict(ask["price_brix"]), config.SIGNING_ACCOUNT,
        closet_market_store.invoice_id(fill["id"]),
        send_max_drops=market_ops.xrp_to_drops_str(quote) if pay_with == "XRP" else None,
        return_url=xumm_ops.discord_return_url(None, None),
        user_token=await _push_token(user),
        platform=memos.platform_for_surface(_platform(user)),
    )
    if not payload:
        await _closet_db(closet_market_store.update_fill, fill["id"], state=closet_market_store.FAILED,
                         error="could not reach Xaman")
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    await _closet_db(
        closet_market_store.update_fill, fill["id"], payload_uuid=payload.get("uuid"),
        xumm_url=payload.get("xumm_url"), qr_url=payload.get("qr_url"), push=payload.get("push"),
    )
    fill = await _closet_db(closet_market_store.get_fill, fill["id"])
    view = _closet_fill_view(fill, wallet)
    view["pay_with"] = pay_with
    view["price_xrp_quote"] = quote if pay_with == "XRP" else None
    return web.json_response(view)
```

Register (after the Task 10 closet routes; the literal `/buy` and `/fill` suffixes are distinct from the bare `{order_id}` GET/DELETE):

```python
    app.router.add_post("/api/closet/bid", handle_closet_bid_create)
    app.router.add_get("/api/closet/bid/{order_id}", handle_closet_bid_status)
    app.router.add_delete("/api/closet/bid/{order_id}", handle_closet_bid_cancel)
    app.router.add_post("/api/closet/ask/{order_id}/buy", handle_closet_ask_buy)
```

`self_cross` is checked before `create_take_fill`, so a seller's own buy never builds a payload.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_api_signing.py tests/test_closet_market_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_closet_market_api_signing.py
git commit -m "feat(closet-market): place/poll/cancel escrow bids and buy asks (#443)"
```

---

### Task 12: Service III — sweep, browse `book` annotation, Mine `listed` counts

**Files:**
- Modify: `lfg_service/app.py`:
  - `_settlement_sweep_loop` (~line 4909)
  - `handle_market_listings` grouped branch (~line 2895)
  - `_compute_mine_data` closet_assets (~line 2977)
- Modify: `webapp/mock_market.py` (mine closet rows gain `listed`)
- Modify: `tests/test_market_api.py` (`test_mine_returns_four_groups` expected shape)
- Test: `tests/test_closet_market_sweep_browse.py`

**Interfaces:**
- Consumes: Tasks 4, 8, 9, 10
- Produces:
  - `async sweep_closet_market() -> None`
  - `_attach_closet_book(rows_out: list[dict], raw_rows: list[dict], summary: list[dict]) -> None`
  - Browse rows carry `book: {best_ask_brix, ask_count, best_bid_brix, bid_count} | None` for grouped trait rows while the Closet market is enabled
  - Mine `closet_assets[*].listed: int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_sweep_browse.py
import asyncio

from lfg_core import closet_market_store as cms
from lfg_service import app as server
from tests import test_closet_market_api as api_tests
from tests.test_closet_market_api import ME, _db, _open_bid

closet_env = api_tests.closet_env  # re-export the fixture (a direct import trips ruff F811)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_sweep_advances_bids_then_settles_fills(closet_env, monkeypatch):
    bid = _open_bid(closet_env)
    order_calls, fill_calls = [], []

    async def fake_advance(order_id, deps):
        order_calls.append(order_id)
        return "NEWFILL" if order_id == bid["id"] else None

    async def fake_settle(fill_id, deps):
        fill_calls.append(fill_id)

    monkeypatch.setattr(server.closet_market_flow, "advance_bid", fake_advance)
    monkeypatch.setattr(server.closet_market_flow, "settle_fill", fake_settle)
    monkeypatch.setattr(server, "_closet_market_deps", lambda: None)
    _run(server.sweep_closet_market())
    assert bid["id"] in order_calls
    assert fill_calls == ["NEWFILL"]


def test_sweep_is_a_noop_when_not_settleable(closet_env, monkeypatch):
    monkeypatch.setattr(server.config, "CLOSET_MARKET_ENC_KEY", "")
    called = []

    async def fake_advance(order_id, deps):
        called.append(order_id)

    monkeypatch.setattr(server.closet_market_flow, "advance_bid", fake_advance)
    _open_bid(closet_env)
    _run(server.sweep_closet_market())
    assert called == []


def test_attach_closet_book_keys_by_raw_slot_value():
    raw = [{"slot": "Head", "value": "Crown"}, {"slot": "Eyes", "value": "Laser"}]
    out = [{"nft_id": "A"}, {"nft_id": "B"}]
    summary = [{"slot": "Head", "value": "Crown", "best_ask_brix": None, "ask_count": 0,
                "best_bid_brix": "10", "bid_count": 1}]
    server._attach_closet_book(out, raw, summary)
    assert out[0]["book"]["best_bid_brix"] == "10"
    assert out[1]["book"] is None


def test_mine_closet_assets_report_listed_counts(closet_env):
    c = _db(closet_env)
    cms.create_ask(c, owner=ME, slot="Head", value="Crown", price_brix="5", platform=None)
    c.close()
    data = server._compute_mine_data("testnet", "testnet", ME)
    [asset] = data["closet_assets"]
    assert (asset["count"], asset["listed"]) == (1, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_sweep_browse.py -v`
Expected: FAIL with `AttributeError: module 'lfg_service.app' has no attribute 'sweep_closet_market'`

- [ ] **Step 3: Implement the sweep**

Add to the Closet Market section:

```python
async def sweep_closet_market() -> None:
    """2-minute backstop: poll pending bid escrows, expire/cancel bids, retry
    every non-terminal fill. Runs whenever settlement is possible — NOT gated
    on CLOSET_MARKET_ENABLED (the kill switch must not strand escrows)."""
    if not config.closet_market_settleable():
        return
    deps = _closet_market_deps()
    order_ids = await _closet_db(closet_market_store.orders_needing_attention)
    fill_ids = await _closet_db(closet_market_store.fills_needing_attention)
    for order_id in order_ids:
        try:
            fill_id = await closet_market_flow.advance_bid(order_id, deps)
            if fill_id and fill_id not in fill_ids:
                fill_ids.append(fill_id)
        except Exception:
            logging.error(f"closet market bid sweep crashed for {order_id}: {traceback.format_exc()}")
    for fill_id in fill_ids:
        try:
            await closet_market_flow.settle_fill(fill_id, deps)
        except Exception:
            logging.error(f"closet market fill sweep crashed for {fill_id}: {traceback.format_exc()}")
    _invalidate_closet_book()
```

In `_settlement_sweep_loop`, inside the `if config.ECONOMY_ENABLED:` block after `sweep_pending_closet_accepts`:

```python
            try:
                await sweep_closet_market()
            except Exception:
                logging.error(f"closet market sweep loop crashed: {traceback.format_exc()}")
```

- [ ] **Step 4: Implement the browse annotation**

Add:

```python
def _attach_closet_book(
    rows_out: list[dict[str, Any]], raw_rows: list[dict[str, Any]], summary: list[dict[str, Any]]
) -> None:
    """#496: a grouped trait card shows the Closet book beside its floor."""
    by_key = {(r["slot"], r["value"]): r for r in summary}
    for out, raw in zip(rows_out, raw_rows, strict=True):
        hit = by_key.get((raw.get("slot"), raw.get("value")))
        out["book"] = (
            {k: hit[k] for k in ("best_ask_brix", "ask_count", "best_bid_brix", "bid_count")} if hit else None
        )
```

In `handle_market_listings`, directly after the `for r in page:` loop that builds `rows_out` and before the final `return`:

```python
    if kind == "trait" and group and config.closet_market_enabled():
        summary = await _closet_db(closet_market_store.book_summary)
        _attach_closet_book(rows_out, page, summary)
```

- [ ] **Step 5: Implement Mine `listed`**

In `_compute_mine_data`, replace the `closet_assets = [...]` comprehension with:

```python
        listed = closet_market_store.encumbrance(conn, wallet)
        closet_assets = [
            {"slot": s, "value": v, "count": c, "listed": listed.get((s, v), 0), "image_url": _img(s, v)}
            for (o, s, v, c) in economy_store.read_closet_assets(conn)
            if o == wallet and c > 0
        ]
```

(The connection there already ran `economy_store.init_economy_schema`, which now ensures the Closet Market tables.)

Keep the two other producers of this shape in step:
- `tests/test_market_api.py::test_mine_returns_four_groups` compares the dict exactly. Change its expected row to `{"slot": "Mouth", "value": "Grin", "count": 2, "listed": 0}`.
- `webapp/mock_market.py` (~line 315, dev-mode parity) builds the same rows. Add `"listed": 0` to its dict.

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_sweep_browse.py tests/test_market_api.py tests/test_market_trait_flow.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add lfg_service/app.py webapp/mock_market.py tests/test_market_api.py tests/test_closet_market_sweep_browse.py
git commit -m "feat(closet-market): settlement sweep, book on trait cards, listed counts in Mine (#443)"
```

---
### Task 13: Ops — audit script, ledger-prerequisite setup script, cron, runbook, docs

**Files:**
- Create: `scripts/audit_closet_market.py`
- Create: `scripts/closet_market_setup.py`
- Modify: `lfg_core/memos.py` (add `ACTION_ACCOUNT_SET`)
- Modify: `ecosystem.prod.config.js`, `ecosystem.staging.config.js`
- Create: `docs/ops/closet-market.md`
- Modify: `CLAUDE.md` (env block + a "Closet Market (#443)" section after "Trait Shop (#217)")
- Test: `tests/test_closet_market_ops.py`

**Interfaces:**
- Consumes:
  - Task 4 store
  - Task 7 `xrpl_ops.get_escrow`
  - `xrpl_ops.get_trustline_balance(address, currency, issuer) -> Decimal | None`
  - `_economy_deps.open_index(network)`
- Produces:
  - audit: `audit_rows(conn, now: int | None = None) -> list[str]`, `unforwarded_brix(conn) -> Decimal`, `async audit_onchain(conn) -> list[str]`, `main(argv) -> int` (exit 0 = clean, 1 = drift)
  - setup: `flag_set(flags: int) -> bool`, `signer_allowed(signer: str, account: str, account_data: dict) -> bool`, `issuer_signer_seed(network: str) -> str`, `main(argv) -> int`
  - `memos.ACTION_ACCOUNT_SET = "account-set"`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_ops.py
import asyncio
import importlib.util
import pathlib
import sqlite3
from decimal import Decimal

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import config, memos
from lfg_core import economy_store as es

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load("audit_closet_market")
setup = _load("closet_market_setup")
SELLER, BUYER = "rSeller", "rBuyer"


def _conn():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 1)], [])
    return c


def _settled_fill(c, *, forward=True):
    bid = cms.create_pending_bid(c, owner=BUYER, slot="Head", value="Crown", price_brix="10", platform=None,
                                 condition="C", fulfillment_enc="S", cancel_after=9, payload_uuid=None,
                                 xumm_url=None, qr_url=None, push=None)
    cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E", escrow_owner_seq=3)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    cms.update_fill(c, fill["id"], state=cms.FUNDED, escrow_finish_hash="FIN")
    cms.move_asset(c, fill["id"])
    cms.update_fill(c, fill["id"], state=cms.PAID, forward_tx_hash="FWD" if forward else None)
    return fill


def test_clean_book_passes():
    c = _conn()
    _settled_fill(c)
    assert audit.audit_rows(c) == []


def test_paid_without_forward_is_drift():
    c = _conn()
    fill = _settled_fill(c, forward=False)
    assert any(fill["id"] in p and "forward" in p for p in audit.audit_rows(c))


def test_over_encumbered_owner_is_drift():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    es.set_closet_contents(c, SELLER, [], [])
    assert any("encumbered" in p for p in audit.audit_rows(c))


def test_stuck_fill_is_reported():
    c = _conn()
    fill = _settled_fill(c)
    cms.update_fill(c, fill["id"], state=cms.ASSET_MOVED, forward_tx_hash=None)
    assert any("stuck" in p for p in audit.audit_rows(c, now=fill["created_ts"] + 7200))


def test_onchain_missing_escrow_and_short_balance(monkeypatch):
    c = _conn()
    bid = cms.create_pending_bid(c, owner=BUYER, slot="Head", value="Tiara", price_brix="10", platform=None,
                                 condition="C", fulfillment_enc="S", cancel_after=9, payload_uuid=None,
                                 xumm_url=None, qr_url=None, push=None)
    cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E2", escrow_owner_seq=4)
    _settled_fill(c)
    fill_id = c.execute("SELECT id FROM closet_fills").fetchone()[0]
    cms.update_fill(c, fill_id, state=cms.ASSET_MOVED, forward_tx_hash=None)

    async def no_escrow(owner, seq):
        return None

    async def balance(address, currency, issuer):
        return Decimal("1")

    monkeypatch.setattr(audit.xrpl_ops, "get_escrow", no_escrow)
    monkeypatch.setattr(audit.xrpl_ops, "get_trustline_balance", balance)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rApp")
    monkeypatch.setattr(config, "BRIX_ISSUER", "rIssuer")
    problems = asyncio.new_event_loop().run_until_complete(audit.audit_onchain(c))
    assert any(bid["id"] in p and "escrow is gone" in p for p in problems)
    assert any("owes 10" in p for p in problems)


def test_setup_helpers(monkeypatch):
    assert setup.flag_set(0x40000000 | 0x00100000) is True
    assert setup.flag_set(0x00100000) is False
    data = {"RegularKey": "rRegKey"}
    assert setup.signer_allowed("rRegKey", "rIssuer", data)
    assert setup.signer_allowed("rIssuer", "rIssuer", {})
    assert not setup.signer_allowed("rOther", "rIssuer", data)
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_SEED", "sDistributor")
    monkeypatch.setattr(config, "SEED", "sSeed")
    assert setup.issuer_signer_seed("mainnet") == "sDistributor"
    assert setup.issuer_signer_seed("testnet") == "sSeed"


def test_account_set_memo_action_exists():
    assert memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_ACCOUNT_SET)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_ops.py -v`
Expected: FAIL with `FileNotFoundError` for `scripts/audit_closet_market.py`

- [ ] **Step 3: Add the memo action**

In `lfg_core/memos.py`, add `ACTION_ACCOUNT_SET = "account-set"  # ops AccountSet (e.g. #443 asfAllowTrustLineLocking)` beside the other actions and add it to `_ACTIONS`.

- [ ] **Step 4: Implement the audit**

```python
#!/usr/bin/env python3
"""Nightly Closet Market audit (#443).

Offline checks (always):
  * every paid/mirrored fill recorded its funds (payment or escrow finish),
    its forward payment, and — if it had overshoot — the overshoot refund
  * every refunded fill recorded its refund
  * no owner has more units encumbered (open/matched asks + pre-move holder
    fills) than they hold
  * every live bid knows its escrow sequence
  * no fill has sat non-terminal for over an hour
With --onchain:
  * every live bid's escrow object exists with the recorded amount
  * the app wallet holds at least the BRIX it has received but not yet paid
    out (skipped when the app wallet IS the BRIX issuer, as on testnet)

Exit 0 clean, 1 drift. Usage:
  .venv/bin/python scripts/audit_closet_market.py --network mainnet [--onchain]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from decimal import Decimal
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import _economy_deps  # noqa: E402

from lfg_core import closet_market_flow, config, market_ops, xrpl_ops  # noqa: E402
from lfg_core import closet_market_store as cms  # noqa: E402

STUCK_SECONDS = 3600


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def audit_rows(conn: sqlite3.Connection, now: int | None = None) -> list[str]:
    now = now if now is not None else int(time.time())
    problems: list[str] = []
    for f in _rows(conn, "SELECT * FROM closet_fills"):
        fid, state = f["id"], f["state"]
        if Decimal(f["fee_brix"]) > Decimal(f["price_brix"]):
            problems.append(f"fill {fid}: fee {f['fee_brix']} exceeds price {f['price_brix']}")
        if state in (cms.PAID, cms.MIRRORED):
            funded = f["payment_tx_hash"] if f["funds_source"] == cms.FUNDS_PAYMENT else f["escrow_finish_hash"]
            if not funded:
                problems.append(f"fill {fid}: {state} without recorded funds")
            if not f["forward_tx_hash"]:
                problems.append(f"fill {fid}: {state} without a forward payment")
            if Decimal(f["overshoot_brix"]) > 0 and not f["overshoot_tx_hash"]:
                problems.append(f"fill {fid}: {state} but overshoot {f['overshoot_brix']} never refunded")
        if state == cms.REFUNDED and not f["refund_tx_hash"]:
            problems.append(f"fill {fid}: refunded without a refund payment")
        if state not in cms.TERMINAL_FILL_STATES and now - int(f["updated_ts"]) > STUCK_SECONDS:
            problems.append(f"fill {fid}: stuck in {state} for {now - int(f['updated_ts'])}s")
    for o in _rows(conn, "SELECT * FROM closet_orders WHERE side = 'bid' AND state IN ('open', 'matched', 'cancelling')"):
        if o["escrow_owner_seq"] is None:
            problems.append(f"bid {o['id']}: {o['state']} without an escrow sequence")
    owners = {r[0] for r in conn.execute(
        "SELECT owner FROM closet_orders WHERE side = 'ask' AND state IN ('open', 'matched') "
        "UNION SELECT seller FROM closet_fills WHERE ask_order_id IS NULL AND state IN ('funds_pending', 'funded')"
    )}
    for owner in sorted(owners):
        for (slot, value), n in cms.encumbrance(conn, owner).items():
            held = cms.holding_count(conn, owner, slot, value)
            if n > held:
                problems.append(f"{owner}: {n} x {slot}={value} encumbered but only {held} held")
    return problems


def unforwarded_brix(conn: sqlite3.Connection) -> Decimal:
    """BRIX the app wallet has received for fills it has not yet paid out."""
    total = Decimal(0)
    for f in _rows(conn, "SELECT * FROM closet_fills WHERE state IN ('funded', 'asset_moved', 'indeterminate', 'refund_pending')"):
        if not (f["payment_tx_hash"] or f["escrow_finish_hash"]):
            continue
        total += Decimal(f["refund_brix"] or cms.fill_in_amount(f))
    return total


async def audit_onchain(conn: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    bids = _rows(
        conn,
        "SELECT o.* FROM closet_orders o WHERE o.side = 'bid' AND o.state IN ('open', 'matched', 'cancelling') "
        "AND o.escrow_owner_seq IS NOT NULL AND o.cancel_finish_hash IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM closet_fills f WHERE f.bid_order_id = o.id AND f.escrow_finish_hash IS NOT NULL)",
    )
    for o in bids:
        try:
            node = await xrpl_ops.get_escrow(o["owner"], o["escrow_owner_seq"])
        except Exception as exc:
            problems.append(f"bid {o['id']}: escrow lookup failed ({exc})")
            continue
        if node is None:
            problems.append(f"bid {o['id']}: {o['state']} but its escrow is gone")
        elif not closet_market_flow._amount_equals(node.get("Amount"), market_ops.brix_amount_dict(o["price_brix"])):
            problems.append(f"bid {o['id']}: escrow amount {node.get('Amount')} != {o['price_brix']} BRIX")
    if config.SIGNING_ACCOUNT != config.BRIX_ISSUER:
        owed = unforwarded_brix(conn)
        balance = await xrpl_ops.get_trustline_balance(config.SIGNING_ACCOUNT, config.BRIX_CURRENCY_HEX, config.BRIX_ISSUER)
        if balance is None:
            problems.append("app wallet BRIX balance unreadable")
        elif balance < owed:
            problems.append(f"app wallet holds {balance} BRIX but owes {cms.fmt_brix(owed)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", required=True, choices=["mainnet", "testnet"])
    parser.add_argument("--onchain", action="store_true")
    args = parser.parse_args(argv)
    conn = _economy_deps.open_index(args.network)
    try:
        problems = audit_rows(conn)
        if args.onchain:
            problems += asyncio.run(audit_onchain(conn))
    finally:
        conn.close()
    for p in problems:
        print(f"DRIFT {p}")
    print("PASS" if not problems else f"FAIL ({len(problems)} problems)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Implement the setup script**

```python
#!/usr/bin/env python3
"""Closet Market (#443) ledger prerequisites. Read-only unless an --apply-* flag is given.

  --check                  issuer lsfAllowTrustLineLocking + the app wallet's BRIX line
  --apply-issuer-flag      AccountSet SetFlag=17 (asfAllowTrustLineLocking) on BRIX_ISSUER.
                           ONE-WAY IN PRACTICE: it cannot be cleared while any BRIX is escrowed.
  --apply-app-limit        TrustSet the app wallet's BRIX line limit to BRIX_TRUSTLINE_LIMIT
                           (EscrowFinish into the app wallet fails past the limit)

Signing:
  mainnet: the BRIX issuer rLfgoBriX… has its master key disabled and its RegularKey
           is the distributor wallet -> BRIX_DISTRIBUTOR_SEED signs the AccountSet.
           The app wallet rLfgoMint… signs via SEED (its regkey), Account=SIGNING_ACCOUNT.
  testnet: the SEED account IS the BRIX issuer and the app wallet (no app limit needed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from xrpl.clients import JsonRpcClient  # noqa: E402
from xrpl.models import IssuedCurrencyAmount  # noqa: E402
from xrpl.models.requests import AccountInfo, AccountLines  # noqa: E402
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag, TrustSet  # noqa: E402
from xrpl.transaction import submit_and_wait  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

from lfg_core import config, memos  # noqa: E402

LSF_ALLOW_TRUSTLINE_LOCKING = 0x40000000


def flag_set(flags: int) -> bool:
    return bool(int(flags) & LSF_ALLOW_TRUSTLINE_LOCKING)


def signer_allowed(signer: str, account: str, account_data: dict[str, Any]) -> bool:
    return signer == account or signer == account_data.get("RegularKey")


def issuer_signer_seed(network: str) -> str:
    return str(config.BRIX_DISTRIBUTOR_SEED if network == "mainnet" else config.SEED)


def _account_data(client: JsonRpcClient, account: str) -> dict[str, Any]:
    result = client.request(AccountInfo(account=account, ledger_index="validated")).result
    if "account_data" not in result:
        raise SystemExit(f"account_info failed for {account}: {result}")
    return dict(result["account_data"])


def _app_line(client: JsonRpcClient) -> dict[str, Any] | None:
    result = client.request(
        AccountLines(account=config.SIGNING_ACCOUNT, peer=config.BRIX_ISSUER, ledger_index="validated")
    ).result
    for line in result.get("lines", []):
        if str(line.get("currency", "")).upper() == str(config.BRIX_CURRENCY_HEX).upper():
            return dict(line)
    return None


def check(client: JsonRpcClient) -> dict[str, Any]:
    issuer = _account_data(client, config.BRIX_ISSUER)
    app_is_issuer = config.SIGNING_ACCOUNT == config.BRIX_ISSUER
    line = None if app_is_issuer else _app_line(client)
    return {
        "network": config.XRPL_NETWORK,
        "brix_issuer": config.BRIX_ISSUER,
        "allow_trustline_locking": flag_set(issuer.get("Flags", 0)),
        "issuer_regular_key": issuer.get("RegularKey"),
        "app_wallet": config.SIGNING_ACCOUNT,
        "app_is_issuer": app_is_issuer,
        "app_line_limit": line.get("limit") if line else None,
        "app_line_balance": line.get("balance") if line else None,
        "target_limit": str(config.BRIX_TRUSTLINE_LIMIT),
    }


def _confirm(network: str, what: str) -> None:
    typed = input(f"About to {what} on {network}. Type the network name to confirm: ").strip()
    if typed != network:
        raise SystemExit("aborted")


def apply_issuer_flag(client: JsonRpcClient, network: str) -> None:
    data = _account_data(client, config.BRIX_ISSUER)
    if flag_set(data.get("Flags", 0)):
        print("lsfAllowTrustLineLocking already set — nothing to do")
        return
    wallet = Wallet.from_seed(issuer_signer_seed(network))
    if not signer_allowed(wallet.classic_address, config.BRIX_ISSUER, data):
        raise SystemExit(f"{wallet.classic_address} is neither {config.BRIX_ISSUER} nor its RegularKey")
    _confirm(network, f"set asfAllowTrustLineLocking on {config.BRIX_ISSUER} (one-way)")
    tx = AccountSet(
        account=config.BRIX_ISSUER,
        set_flag=AccountSetAsfFlag.ASF_ALLOW_TRUSTLINE_LOCKING,
        source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_ACCOUNT_SET),
    )
    print(json.dumps(submit_and_wait(tx, client, wallet).result.get("meta", {}).get("TransactionResult")))


def apply_app_limit(client: JsonRpcClient, network: str) -> None:
    if config.SIGNING_ACCOUNT == config.BRIX_ISSUER:
        print("app wallet is the BRIX issuer — no trust line needed")
        return
    _confirm(network, f"raise {config.SIGNING_ACCOUNT}'s BRIX limit to {config.BRIX_TRUSTLINE_LIMIT}")
    tx = TrustSet(
        account=config.SIGNING_ACCOUNT,
        limit_amount=IssuedCurrencyAmount(
            currency=config.BRIX_CURRENCY_HEX, issuer=config.BRIX_ISSUER, value=str(config.BRIX_TRUSTLINE_LIMIT)
        ),
        source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_TRUSTSET),
    )
    result = submit_and_wait(tx, client, Wallet.from_seed(config.SEED)).result
    print(json.dumps(result.get("meta", {}).get("TransactionResult")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--network", required=True, choices=["mainnet", "testnet"])
    parser.add_argument("--apply-issuer-flag", action="store_true")
    parser.add_argument("--apply-app-limit", action="store_true")
    args = parser.parse_args(argv)
    config.assert_cli_network_match(args.network)
    client = JsonRpcClient(config.JSON_RPC_URL)
    if args.apply_issuer_flag:
        apply_issuer_flag(client, args.network)
    if args.apply_app_limit:
        apply_app_limit(client, args.network)
    print(json.dumps(check(client), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Cron entries**

`ecosystem.prod.config.js`, after `lfg-economy-audit`:

```js
    // Closet Market (#443) conservation/escrow audit — 00:35, after the 00:20/00:25 economy crons.
    { name: "lfg-closet-market-audit", cwd: CWD, script: "scripts/audit_closet_market.py", interpreter: PY, args: ["--network", "mainnet", "--onchain"], cron_restart: "35 0 * * *", autorestart: false },
```

`ecosystem.staging.config.js`, after `stg-economy-audit`:

```js
    { name: "stg-closet-market-audit", cwd: CWD, script: "scripts/audit_closet_market.py", interpreter: PY, args: ["--network", "testnet", "--onchain"], cron_restart: "35 0 * * *", autorestart: false },
```

- [ ] **Step 7: Runbook**

Create `docs/ops/closet-market.md`:

````markdown
# Closet Market (#443) — ops runbook

Off-ledger trait asks + TokenEscrow-backed bids. Ships dark (`CLOSET_MARKET_ENABLED=0`).

## 1. Ledger prerequisites (once per network)

Check first (read-only):

```bash
.venv/bin/python scripts/closet_market_setup.py --network mainnet
```

| fact (verified 2026-09-14) | value |
|---|---|
| BRIX issuer | `rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px` — master disabled, RegularKey `rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ` (= distributor) |
| `lsfAllowTrustLineLocking` | NOT set |
| app wallet (`SIGNING_ACCOUNT`) | `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` — BRIX line limit 258,055, balance 45,509 |

1. **Issuer flag** (signed by `BRIX_DISTRIBUTOR_SEED`, `Account` = issuer). One-way in practice: it cannot be cleared while any BRIX sits in an escrow.
   ```bash
   .venv/bin/python scripts/closet_market_setup.py --network mainnet --apply-issuer-flag
   ```
2. **App wallet limit** → `BRIX_TRUSTLINE_LIMIT` (EscrowFinish into the app wallet fails once balance + bid > limit):
   ```bash
   .venv/bin/python scripts/closet_market_setup.py --network mainnet --apply-app-limit
   ```
3. Testnet: `--network testnet --apply-issuer-flag` (the SEED account is issuer + app wallet; no limit step).

## 2. Configuration

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```
CLOSET_MARKET_ENC_KEY=<fernet key>     # REQUIRED; losing it strands every open bid until CancelAfter
CLOSET_MARKET_FEE_BPS=700              # 0 <= bps < 10000
CLOSET_BID_TTL_SECONDS=604800
CLOSET_MARKET_LEDGER_MARGIN=40
CLOSET_MARKET_ENABLED=1                # last — new orders only
```

Back up `CLOSET_MARKET_ENC_KEY` with the other secrets. Without it the backend can
neither fill nor refund an open bid early; the bidder's funds return only after
`CancelAfter` via EscrowCancel (anyone may submit it).

## 3. Audit cron

```bash
pm2 start ecosystem.prod.config.js --only lfg-closet-market-audit && pm2 save
.venv/bin/python scripts/audit_closet_market.py --network mainnet --onchain   # manual; exit 1 = drift
```

## 4. Kill switch & rollback

- `CLOSET_MARKET_ENABLED=0` + restart: no new asks/bids/buys/fills. Cancels, status
  polls and the 2-minute settlement sweep keep running (they need only
  `ECONOMY_ENABLED=1` and the enc key), so nothing escrowed is stranded.
- Never delete `closet_orders` / `closet_fills` rows: a code rollback must keep
  both tables (open escrows are only findable through them).

## 5. Reading a stuck fill

Journals: `$ECONOMY_RECORDS_DIR/closet-fill-<id>.json` and `closet-order-<id>.json`.

| state | meaning | action |
|---|---|---|
| `indeterminate` | a backend tx outcome is unknown | none — resolves when the validated ledger passes `pending_lls` (memo `lfg:closet_<phase>:<id>` found = landed, absent = retried) |
| `asset_moved` + attempts > 0 | forward payment failing | check app wallet BRIX balance (audit `owes` line) |
| `paid` + attempts > 0 | Closet mirror failing | check CDN / NFTokenModify; the listener will not rebuild this owner until mirrored |
| `refund_pending` + attempts > 0 | refund failing | app wallet balance |
````

- [ ] **Step 8: CLAUDE.md**

Add to the env block after `SHOP_OFFER_TTL_SECONDS`:

```
CLOSET_MARKET_ENABLED=0                                     # optional (#443); Closet Market NEW orders — cancel/status/settlement run whenever ECONOMY_ENABLED + CLOSET_MARKET_ENC_KEY
CLOSET_MARKET_ENC_KEY=<fernet-key>                          # optional (#443); seals each bid's escrow fulfillment — REQUIRED for the feature; back it up
CLOSET_MARKET_FEE_BPS=700                                   # optional (#443); market fee on fills, 0 <= bps < 10000
CLOSET_BID_TTL_SECONDS=604800                               # optional (#443); bid escrow CancelAfter
CLOSET_MARKET_LEDGER_MARGIN=40                              # optional (#443); LastLedgerSequence headroom on backend Closet Market txs
```

Add a section after "Trait Shop (#217)":

```markdown
### Closet Market (#443)

Off-ledger trait order book for loose Closet assets. Design:
`docs/superpowers/specs/2026-08-25-closet-market-design.md`; plan + spec deltas:
`docs/superpowers/plans/2026-09-14-closet-market.md`; ops: `docs/ops/closet-market.md`.

- **Asks** are `closet_orders` rows (zero signatures). They ENCUMBER, never
  decrement: `available = closet_assets.count − open/matched asks − pre-move
  holder fills` (`closet_market_store.encumbrance`). Equip/Assemble/Extract
  refuse listed units (`economy_flow._listed_error`).
- **Bids** lock BRIX in an XRPL TokenEscrow to the app wallet with a
  PREIMAGE-SHA-256 condition (`lfg_core/crypto_condition.py`); the fulfillment
  is Fernet-sealed with `CLOSET_MARKET_ENC_KEY`. Bids open only after the
  validated EscrowCreate AND the escrow ledger object match.
- **Fills** (`closet_market_flow.settle_fill`): funds (buyer Payment with
  `InvoiceID = sha256(fill id)`, or EscrowFinish) → ONE sqlite tx moves the
  unit and closes both orders → forward `price − fee` (+ overshoot refund) →
  re-sync both Closet tokens. Backend txs carry `lfg:closet_<phase>:<id>`
  memos + pinned LastLedgerSequence; unknown outcomes resolve only by memo
  lookup or LLS passing (never "absent = failed").
- **The DB is ahead of the Closet token between asset move and mirror.**
  `nft_listener._apply_closet` and `backfill_economy._reconcile_closet` skip
  rebuilding `closet_assets` for an owner with an unmirrored fill
  (`has_unmirrored_fill`) — removing that guard resurrects moved units.
- Mainnet BRIX issuer is `rLfgoBriX…` (RegularKey = distributor), not the NFT
  issuer; testnet's SEED account is both issuer and app wallet.
```

- [ ] **Step 9: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_ops.py tests/test_memos.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add scripts/audit_closet_market.py scripts/closet_market_setup.py lfg_core/memos.py ecosystem.prod.config.js ecosystem.staging.config.js docs/ops/closet-market.md CLAUDE.md tests/test_closet_market_ops.py
git commit -m "feat(closet-market): audit + ledger-prerequisite scripts, cron, runbook, docs (#443)"
```

---

### Task 14: Client pure helpers

**Files:**
- Create: `webapp/client/closet_market_pure.js`
- Test: `tests/test_closet_market_pure_js.py`

**Interfaces:**
- Consumes: row shapes from Tasks 10–12 (`book_summary` rows, `book_levels`, `orders_for_owner`, `bids_on_holdings`, grouped `row.book`, Mine `closet_assets[*].listed`, `/api/closet/keys`)
- Produces (ES module exports):
  - Money math: `toMicro(s) -> BigInt`, `fromMicro(n) -> string`, `sellerNet(priceBrix, feeBps) -> string`
  - Disclosures: `askDisclosure(priceBrix, feeBps)`, `bidDisclosure(priceBrix | null, ttlDays)`, `fillDisclosure(priceBrix, feeBps, needsDeposit)`
  - Book: `mapBookRow(row)`, `bookBadge(book)`, `bookLine(book)`, `bestAskFor(levels, wallet)`
  - Trait keys: `keySlots(keys)`, `keyValues(keys, slot)`
  - Chip labels: `orderChipLabel(order)`, `fillStateText(state)`, `fillChipLabel(fill, wallet)`, `holdingFillAction(entry)`, `holdingLabel(entry)`, `closetAssetLabel(asset)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_pure_js.py
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/closet_market_pure.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result, "
        "(k, v) => typeof v === 'bigint' ? v.toString() : v));\n"
    )
    proc = subprocess.run([NODE, "--input-type=module"], input=script, capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("raw,micro", [("1", "1000000"), ("12.5", "12500000"), ("0.000001", "1")])
def test_to_micro(raw, micro):
    assert run_js(f"M.toMicro({json.dumps(raw)})") == micro


@pytest.mark.parametrize("price,bps,net", [("10", 700, "9.3"), ("0.000001", 700, "0.000001"), ("12.345678", 250, "12.037037")])
def test_seller_net_matches_server_round_down(price, bps, net):
    assert run_js(f"M.sellerNet({json.dumps(price)}, {bps})") == net


def test_disclosures_mention_money_and_terms():
    assert "9.3 BRIX" in run_js('M.askDisclosure("10", 700)') and "7%" in run_js('M.askDisclosure("10", 700)')
    assert "fee" not in run_js('M.askDisclosure("10", 0)')
    bid = run_js('M.bidDisclosure("10", 7)')
    assert "10 BRIX" in bid and "7 days" in bid and "escrow" in bid
    assert "BRIX" in run_js("M.bidDisclosure(null, 7)")
    assert "deposit" in run_js('M.fillDisclosure("10", 0, true)').lower()
    assert "deposit" not in run_js('M.fillDisclosure("10", 0, false)').lower()


def test_book_row_and_badges():
    row = {"slot": "Head", "value": "Crown", "best_ask_brix": "12", "ask_count": 2, "best_bid_brix": None, "bid_count": 0}
    vm = run_js(f"M.mapBookRow({json.dumps(row)})")
    assert vm["title"] == "Head: Crown" and vm["askLabel"] == "Ask 12 BRIX (2)" and vm["bidLabel"] == "No bids"
    assert run_js("M.bookBadge(null)") is None
    assert run_js(f"M.bookBadge({json.dumps({**row, 'best_bid_brix': '9'})})") == "Bid 9"
    assert run_js(f"M.bookLine({json.dumps({**row, 'best_bid_brix': '9', 'bid_count': 1})})") == "Closet market: best bid 9 BRIX · best ask 12 BRIX"


def test_best_ask_skips_own():
    levels = {"asks": [{"id": "A", "owner": "rMe", "price_brix": "1"}, {"id": "B", "owner": "rYou", "price_brix": "2"}]}
    assert run_js(f"M.bestAskFor({json.dumps(levels)}, 'rMe').id") == "B"
    assert run_js(f"M.bestAskFor({json.dumps({'asks': []})}, 'rMe')") is None


def test_keys_group_by_slot():
    keys = [{"slot": "Head", "value": "Crown"}, {"slot": "Eyes", "value": "Laser"}, {"slot": "Head", "value": "Tiara"}]
    assert run_js(f"M.keySlots({json.dumps(keys)})") == ["Eyes", "Head"]
    assert run_js(f"M.keyValues({json.dumps(keys)}, 'Head')") == ["Crown", "Tiara"]


def test_chip_labels():
    order = {"side": "bid", "slot": "Head", "value": "Crown", "price_brix": "10", "state": "pending_escrow"}
    assert run_js(f"M.orderChipLabel({json.dumps(order)})") == "Bid · Head: Crown · 10 BRIX (awaiting signature)"
    fill = {"buyer": "rMe", "seller": "rYou", "slot": "Head", "value": "Crown", "price_brix": "10", "state": "refunded"}
    assert run_js(f"M.fillChipLabel({json.dumps(fill)}, 'rMe')") == "Bought · Head: Crown · 10 BRIX — Refunded"
    token = {"source": "token", "nft_id": "N1", "slot": "Head", "value": "Crown", "price_brix": "10"}
    assert run_js(f"M.holdingFillAction({json.dumps(token)})") == {"label": "Deposit & fill", "needsDeposit": True, "nftId": "N1"}
    assert run_js(f"M.holdingLabel({json.dumps(token)})") == "Head: Crown — 10 BRIX (in your wallet)"
    assert run_js("M.closetAssetLabel({slot: 'Head', value: 'Crown', count: 3, listed: 1})") == "Head: Crown ×3 (1 listed)"
    assert run_js("M.closetAssetLabel({slot: 'Head', value: 'Crown', count: 3})") == "Head: Crown ×3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_pure_js.py -v`
Expected: FAIL (`Cannot find module .../closet_market_pure.js`)

- [ ] **Step 3: Implement**

```js
// webapp/client/closet_market_pure.js
// Closet Market (#443) pure helpers — labels, disclosures and BRIX money math
// for the order book UI. Kept DOM-free so tests/test_closet_market_pure_js.py
// can execute it under Node. BRIX math is BigInt micro-units (6 dp), rounding
// the fee DOWN exactly like closet_market_store.fee_for on the server.

const MICRO = 1000000n;

export function toMicro(s) {
  const [whole, frac = ''] = String(s).split('.');
  return BigInt(whole || '0') * MICRO + BigInt((frac + '000000').slice(0, 6));
}

export function fromMicro(n) {
  const whole = n / MICRO;
  const frac = (n % MICRO).toString().padStart(6, '0').replace(/0+$/, '');
  return frac ? `${whole}.${frac}` : `${whole}`;
}

export function sellerNet(priceBrix, feeBps) {
  const price = toMicro(priceBrix);
  return fromMicro(price - (price * BigInt(feeBps)) / 10000n);
}

function feePct(feeBps) {
  return `${Number(feeBps) / 100}%`;
}

export function askDisclosure(priceBrix, feeBps) {
  const net = sellerNet(priceBrix, feeBps);
  const fee = Number(feeBps) > 0 ? ` after the ${feePct(feeBps)} market fee` : '';
  return `You'll receive ${net} BRIX${fee} when it sells. Listing is free and instant — unlist any time.`;
}

export function bidDisclosure(priceBrix, ttlDays) {
  const amount = priceBrix == null ? 'your BRIX' : `${priceBrix} BRIX`;
  return `Locks ${amount} in an on-ledger escrow until a holder fills it, you cancel, or it expires in ${ttlDays} days. `
    + 'If you match a cheaper listing, the difference comes back to you.';
}

export function fillDisclosure(priceBrix, feeBps, needsDeposit) {
  const net = sellerNet(priceBrix, feeBps);
  const fee = Number(feeBps) > 0 ? ` after the ${feePct(feeBps)} market fee` : '';
  const deposit = needsDeposit ? 'Your trait token is deposited back into your Closet first. ' : '';
  return `${deposit}You'll receive ${net} BRIX${fee}; the trait moves to the buyer's Closet. No signature needed.`;
}

export function mapBookRow(row) {
  const count = (n) => (n > 1 ? ` (${n})` : '');
  return {
    slot: row.slot,
    value: row.value,
    title: `${row.slot}: ${row.value}`,
    bestAsk: row.best_ask_brix ?? null,
    bestBid: row.best_bid_brix ?? null,
    askLabel: row.best_ask_brix == null ? 'No asks' : `Ask ${row.best_ask_brix} BRIX${count(row.ask_count)}`,
    bidLabel: row.best_bid_brix == null ? 'No bids' : `Bid ${row.best_bid_brix} BRIX${count(row.bid_count)}`,
  };
}

export function bookBadge(book) {
  return book && book.best_bid_brix != null ? `Bid ${book.best_bid_brix}` : null;
}

export function bookLine(book) {
  if (!book) return '';
  const parts = [];
  if (book.best_bid_brix != null) parts.push(`best bid ${book.best_bid_brix} BRIX`);
  if (book.best_ask_brix != null) parts.push(`best ask ${book.best_ask_brix} BRIX`);
  return parts.length ? `Closet market: ${parts.join(' · ')}` : '';
}

export function bestAskFor(levels, wallet) {
  const asks = (levels && levels.asks) || [];
  return asks.find((a) => a.owner !== wallet) || null;
}

export function keySlots(keys) {
  return [...new Set(keys.map((k) => k.slot))].sort();
}

export function keyValues(keys, slot) {
  return keys.filter((k) => k.slot === slot).map((k) => k.value).sort();
}

const ORDER_STATE_TEXT = {
  pending_escrow: 'awaiting signature', open: 'open', matched: 'filling', cancelling: 'cancelling',
  filled: 'filled', cancelled: 'cancelled', expired: 'expired',
};

export function orderChipLabel(order) {
  const side = order.side === 'bid' ? 'Bid' : 'Ask';
  return `${side} · ${order.slot}: ${order.value} · ${order.price_brix} BRIX (${ORDER_STATE_TEXT[order.state] ?? order.state})`;
}

const FILL_STATE_TEXT = {
  funds_pending: 'Waiting for funds', funded: 'Moving the trait', asset_moved: 'Paying the seller',
  paid: 'Updating Closets', mirrored: 'Complete', refund_pending: 'Refunding', refunded: 'Refunded',
  indeterminate: 'Confirming on-ledger', failed: 'Failed',
};

export function fillStateText(state) {
  return FILL_STATE_TEXT[state] ?? state;
}

export function fillChipLabel(fill, wallet) {
  const role = fill.buyer === wallet ? 'Bought' : 'Sold';
  return `${role} · ${fill.slot}: ${fill.value} · ${fill.price_brix} BRIX — ${fillStateText(fill.state)}`;
}

export function holdingFillAction(entry) {
  return entry.source === 'token'
    ? { label: 'Deposit & fill', needsDeposit: true, nftId: entry.nft_id }
    : { label: 'Fill', needsDeposit: false, nftId: null };
}

export function holdingLabel(entry) {
  const where = entry.source === 'token' ? ' (in your wallet)' : '';
  return `${entry.slot}: ${entry.value} — ${entry.price_brix} BRIX${where}`;
}

export function closetAssetLabel(asset) {
  const listed = asset.listed ? ` (${asset.listed} listed)` : '';
  return `${asset.slot}: ${asset.value} ×${asset.count}${listed}`;
}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_pure_js.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/client/closet_market_pure.js tests/test_closet_market_pure_js.py
git commit -m "feat(closet-market): client pure helpers for the order book UI (#443)"
```

---

### Task 15: Client wiring — Wanted tab, bid form, Mine sections, card badge, flows

**Files:**
- Modify: `webapp/client/index.html`:
  - market tab chip
  - `#market-book` section
  - three Mine sections
  - `#closet-bid-form-panel`
  - `app.js?v=` and stylesheet filename
- Modify: `webapp/client/app.js`:
  - import
  - flags
  - `switchMarketTab`
  - `MARKET_STATUS_PATH`
  - `MARKET_RESUME_RENDER`
  - `renderMarketGrid`
  - `openListingDetail`
  - `renderMineGroups`
  - `loadMarketMine`
  - `openListForm`
  - `updateListFormRoyaltyPreview`
  - `submitListForm`
  - new functions
  - `main()` wiring
- Rename + modify: `webapp/client/style.v29.css` → `style.v30.css` (check `main`'s current number first; use the next free one)
- Modify: `tests/test_harvest_pure_js.py:188`, `tests/test_app_js_deeplink.py:74` (pinned `app.js?v=`)
- Test: `tests/test_closet_market_dom.py`

**Interfaces:**
- Consumes: Task 14 exports; endpoints from Tasks 10–12; existing app.js helpers:
  - `api`, `showFlow`, `signText`, `marketFlow`, `pollMarketFlow`
  - `renderChipList`, `mineTraitImgSrc`, `confirmDialog`, `showError`, `showPanel`
  - `pollEconomyOp`, `status`, `closeListingDetail`, `marketPure.validateBrixPrice`, `me`
- Produces: `closetBidRender(s)`, `closetFillRender(s)`, `loadClosetBook()`, `loadClosetMine()`, `openClosetBidForm(slot?, value?)`, `placeClosetBid(slot, value, price)`, `postClosetAsk(item, price)`, `buyBestClosetAsk(row)`, `fillClosetBid(entry)`, `cancelClosetOrder(order)`, `viewClosetFill(fill)`, `applyClosetMarketVisibility(cfg)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_closet_market_dom.py
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel)) as fh:
        return fh.read()


def _stylesheet():
    html = _read("webapp/client/index.html")
    return _read("webapp/client/" + re.search(r'href="(style\.v\d+\.css)"', html).group(1))


def test_markup_has_closet_market_surfaces():
    html = _read("webapp/client/index.html")
    for element_id in (
        "market-book", "closet-book-asks", "closet-book-asks-empty", "closet-book-bids", "closet-book-bids-empty",
        "closet-bid-new-btn", "closet-bid-form-panel", "closet-bid-slot", "closet-bid-value", "closet-bid-price",
        "closet-bid-note", "closet-bid-confirm-btn", "closet-bid-cancel-btn",
        "mine-closet-orders-section", "mine-closet-orders", "mine-closet-incoming-section", "mine-closet-incoming",
        "mine-closet-fills-section", "mine-closet-fills",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert 'data-tab="book"' in html


def test_app_js_wires_closet_market():
    js = _read("webapp/client/app.js")
    assert re.search(r"import \* as closetPure from './closet_market_pure\.js\?v=\d+';", js)
    assert "closet_bid: (id) => `/api/closet/bid/${id}`" in js
    assert "closet_fill: (id) => `/api/closet/fill/${id}`" in js
    assert "cfg.closet_market_enabled === true" in js
    for fn in (
        "function closetBidRender(s)", "function closetFillRender(s)", "async function loadClosetBook()",
        "async function loadClosetMine()", "async function openClosetBidForm(", "async function placeClosetBid(",
        "async function postClosetAsk(", "async function buyBestClosetAsk(", "async function fillClosetBid(",
        "async function cancelClosetOrder(", "async function viewClosetFill(", "function applyClosetMarketVisibility(",
    ):
        assert fn in js, fn
    assert "'/api/deposit'" in js and "Deposit & fill" not in js  # label comes from closetPure, not hard-coded
    assert "closetPure.bookBadge(row.book)" in js


def test_badge_is_styled():
    assert ".market-card-bid" in _stylesheet()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_closet_market_dom.py -v`
Expected: FAIL on the first missing id (`market-book`)

- [ ] **Step 3: Markup**

In `webapp/client/index.html`:

(a) After the Mine chip in `#market-tabs`:

```html
        <button class="lb-chip" role="tab" aria-selected="false" data-tab="book" hidden>Wanted</button>
```

(b) Inside `#market-mine`, after the "Loose Closet traits" section:

```html
        <div id="mine-closet-orders-section" class="trait-strip-section" hidden>
          <h4 class="trait-strip-heading">My Closet market orders</h4>
          <div id="mine-closet-orders" class="trait-strip"></div>
          <p id="mine-closet-orders-empty" class="trait-strip-empty" hidden>No open asks or bids.</p>
        </div>
        <div id="mine-closet-incoming-section" class="trait-strip-section" hidden>
          <h4 class="trait-strip-heading">Bids on my traits</h4>
          <div id="mine-closet-incoming" class="trait-strip"></div>
          <p id="mine-closet-incoming-empty" class="trait-strip-empty" hidden>Nobody is bidding on traits you hold.</p>
        </div>
        <div id="mine-closet-fills-section" class="trait-strip-section" hidden>
          <h4 class="trait-strip-heading">Recent Closet trades</h4>
          <div id="mine-closet-fills" class="trait-strip"></div>
          <p id="mine-closet-fills-empty" class="trait-strip-empty" hidden>No trades yet.</p>
        </div>
```

(c) Right after the closing `</div>` of `#market-mine`:

```html
      <div id="market-book" class="market-section" hidden>
        <p><button id="closet-bid-new-btn" class="primary">Place a bid</button></p>
        <div class="trait-strip-section">
          <h4 class="trait-strip-heading">Closet listings</h4>
          <div id="closet-book-asks" class="trait-strip"></div>
          <p id="closet-book-asks-empty" class="trait-strip-empty" hidden>No Closet listings yet.</p>
        </div>
        <div class="trait-strip-section">
          <h4 class="trait-strip-heading">Wanted</h4>
          <div id="closet-book-bids" class="trait-strip"></div>
          <p id="closet-book-bids-empty" class="trait-strip-empty" hidden>No open bids yet — be the first.</p>
        </div>
      </div>
```

(d) After `#market-list-form-panel`:

```html
    <section id="closet-bid-form-panel" class="card" hidden>
      <h2>Place a bid</h2>
      <p class="card-sub">Name the trait and your price — no listing needs to exist. Any holder can fill it.</p>
      <label class="card-sub" for="closet-bid-slot">Slot</label>
      <select id="closet-bid-slot"></select>
      <label class="card-sub" for="closet-bid-value">Trait</label>
      <select id="closet-bid-value"></select>
      <input id="closet-bid-price" type="text" inputmode="decimal" placeholder="Bid in BRIX">
      <p id="closet-bid-note" class="cost"></p>
      <button id="closet-bid-confirm-btn" class="primary big">Lock bid</button>
      <p><button id="closet-bid-cancel-btn" class="back">← Cancel</button></p>
    </section>
```

(e) Bump `app.js?v=88` → `app.js?v=89`, and update the exact pins in `tests/test_harvest_pure_js.py` and `tests/test_app_js_deeplink.py` to `89`. If `main` has moved, use its next number. Then `git mv webapp/client/style.v29.css webapp/client/style.v30.css` and point the `<link>` at it.

- [ ] **Step 4: CSS**

Append to the renamed stylesheet:

```css
/* Closet Market (#443/#496): best Closet bid on a grouped trait card */
.market-card-bid {
  position: absolute; top: 6px; right: 6px;
  background: #3ecf8e; color: #111; font-size: .62rem; font-weight: 800;
  padding: 2px 6px; border-radius: 999px; pointer-events: none;
}
#closet-bid-form-panel select { width: 100%; margin: 4px 0 10px; }
```

- [ ] **Step 5: app.js — imports, flags, tabs, status tables**

Below the `marketPure` import:

```js
// Closet Market (#443) pure helpers — Node-tested in tests/test_closet_market_pure_js.py.
import * as closetPure from './closet_market_pure.js?v=1';
```

Near `shopEnabled`'s declaration:

```js
let closetMarketEnabled = false; // /api/config closet_market_enabled (fails closed)
let closetMarketFeeBps = 0;
let closetBidTtlDays = 7;
let closetKeysCache = null;
```

Replace `switchMarketTab` with:

```js
function switchMarketTab(tab) {
  if (tab === 'shop' && !shopEnabled) tab = 'browse';
  if (tab === 'book' && !closetMarketEnabled) tab = 'browse';
  marketState.tab = tab;
  highlightTabs('market-tabs', 'tab', tab);
  el('market-browse').hidden = tab !== 'browse';
  el('market-mine').hidden = tab !== 'mine';
  el('market-shop').hidden = tab !== 'shop';
  el('market-book').hidden = tab !== 'book';
  if (tab === 'browse') loadMarketBrowse();
  else if (tab === 'mine') loadMarketMine();
  else if (tab === 'book') loadClosetBook().catch((e) => showError(e.message));
  else loadShopCatalog();
}
```

Add to `MARKET_STATUS_PATH`:

```js
  closet_bid: (id) => `/api/closet/bid/${id}`,
  closet_fill: (id) => `/api/closet/fill/${id}`,
```

Add to `MARKET_RESUME_RENDER`:

```js
  closet_bid: () => closetBidRender,
  closet_fill: () => closetFillRender,
```

- [ ] **Step 6: app.js — visibility, renders, book, bid form, asks, Mine**

Add after `applyShopVisibility`:

```js
function applyClosetMarketVisibility(cfg) {
  closetMarketEnabled = cfg.closet_market_enabled === true;
  closetMarketFeeBps = Number(cfg.closet_market_fee_bps || 0);
  closetBidTtlDays = Math.round(Number(cfg.closet_bid_ttl_seconds || 604800) / 86400);
  const chip = document.querySelector('#market-tabs [data-tab="book"]');
  if (chip) chip.hidden = !closetMarketEnabled;
  for (const id of ['mine-closet-orders-section', 'mine-closet-incoming-section', 'mine-closet-fills-section']) {
    el(id).hidden = !closetMarketEnabled;
  }
  if (!closetMarketEnabled) el('market-book').hidden = true;
}

function closetBidRender(s) {
  if (s.state === 'awaiting_signature') {
    return { title: '🏷️ Place bid', text: signText(s.push, 'Scan to lock your bid in escrow with Xaman.'), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
  }
  if (s.state === 'pending') {
    return { title: '⏳ Confirming', text: s.order_state === 'cancelling' ? 'Returning your BRIX…' : 'Signature received — verifying your escrow on-ledger…', spinner: true };
  }
  if (s.state === 'done') {
    return s.order_state === 'open'
      ? { title: '🎉 Bid live', text: `${s.price_brix} BRIX is locked in escrow for ${s.slot}: ${s.value}. Any holder can fill it; cancel any time from Mine.`, done: true }
      : { title: '🎉 Bid matched', text: `Your bid matched a Closet listing — ${s.slot}: ${s.value} is on its way to your Closet.`, done: true, celebrate: true };
  }
  return { title: '❌ Bid not placed', text: s.error || 'Something went wrong.', done: true };
}

function closetFillRender(s) {
  if (s.state === 'awaiting_signature') {
    const via = s.pay_with === 'XRP' ? ` (about ${s.price_xrp_quote} XRP)` : '';
    return { title: '🛒 Buy trait', text: signText(s.push, `Scan to pay ${s.price_brix} BRIX${via} in Xaman.`), qrData: s.xumm_url, link: s.xumm_url, push: s.push };
  }
  if (s.state === 'pending') {
    return { title: '⏳ Settling', text: `${closetPure.fillStateText(s.fill_state)}…`, spinner: true };
  }
  if (s.state === 'done') {
    return s.role === 'buyer'
      ? { title: '🎉 Trait is yours', text: `${s.slot}: ${s.value} is in your Closet.`, done: true, celebrate: true }
      : { title: '🎉 Sold', text: `${s.slot}: ${s.value} sold — the BRIX is on its way to your wallet.`, done: true, celebrate: true };
  }
  return { title: s.fill_state === 'refunded' ? '↩️ Refunded' : '❌ Not completed', text: s.error || 'Something went wrong.', done: true };
}

async function loadClosetBook() {
  const data = await api('/api/closet/book');
  const rows = data.rows.map(closetPure.mapBookRow);
  const chip = (r, label) => ({ imgSrc: mineTraitImgSrc(r.slot, r.value, null), label: `${r.title} — ${label}`, payload: r });
  renderChipList(el('closet-book-asks'), el('closet-book-asks-empty'),
    rows.filter((r) => r.bestAsk != null).map((r) => chip(r, r.askLabel)), 'Buy', buyBestClosetAsk);
  renderChipList(el('closet-book-bids'), el('closet-book-bids-empty'),
    rows.filter((r) => r.bestBid != null).map((r) => chip(r, r.bidLabel)), 'Bid', (r) => openClosetBidForm(r.slot, r.value));
}

async function buyBestClosetAsk(r) {
  const levels = await api(`/api/closet/book?slot=${encodeURIComponent(r.slot)}&value=${encodeURIComponent(r.value)}`);
  const ask = closetPure.bestAskFor(levels, me && me.wallet);
  if (!ask) { showError('No listing you can buy right now.'); return; }
  const ok = await confirmDialog({ title: `Buy ${r.title}?`, text: `${ask.price_brix} BRIX — it goes straight into your Closet.`, confirmLabel: 'Buy' });
  if (!ok) return;
  await marketFlow('closet_fill', `/api/closet/ask/${ask.id}/buy`, {}, closetFillRender);
}

function fillSelect(selectEl, options, selected) {
  selectEl.replaceChildren(...options.map((o) => {
    const opt = document.createElement('option');
    opt.value = o;
    opt.textContent = o;
    opt.selected = o === selected;
    return opt;
  }));
}

async function openClosetBidForm(slot = null, value = null) {
  if (!closetKeysCache) closetKeysCache = (await api('/api/closet/keys')).keys;
  const slots = closetPure.keySlots(closetKeysCache);
  fillSelect(el('closet-bid-slot'), slots, slot || slots[0]);
  const refreshValues = (preferred) => fillSelect(
    el('closet-bid-value'), closetPure.keyValues(closetKeysCache, el('closet-bid-slot').value), preferred,
  );
  refreshValues(value);
  el('closet-bid-slot').onchange = () => refreshValues(null);
  el('closet-bid-price').value = '';
  el('closet-bid-note').textContent = closetPure.bidDisclosure(null, closetBidTtlDays);
  showPanel('closet-bid-form-panel');
}

async function submitClosetBidForm() {
  const price = el('closet-bid-price').value.trim();
  const check = marketPure.validateBrixPrice(price);
  if (!check.ok) { showError(check.error); return; }
  const slot = el('closet-bid-slot').value;
  const value = el('closet-bid-value').value;
  const ok = await confirmDialog({ title: `Bid ${price} BRIX for ${slot}: ${value}?`, text: closetPure.bidDisclosure(price, closetBidTtlDays), confirmLabel: 'Lock bid' });
  if (!ok) return;
  await placeClosetBid(slot, value, price);
}

async function placeClosetBid(slot, value, price) {
  closeListingDetail();
  await marketFlow('closet_bid', '/api/closet/bid', { slot, value, price_brix: price }, closetBidRender);
}

async function postClosetAsk(item, price) {
  const res = await api('/api/closet/ask', { method: 'POST', body: JSON.stringify({ slot: item.slot, value: item.value, price_brix: price }) });
  if (res.fill) {
    showFlow(closetFillRender(res.fill));
    if (!marketPure.isMarketTerminal(res.fill.state)) pollMarketFlow('closet_fill', res.fill.id, closetFillRender);
    return;
  }
  showPanel('market-panel');
  switchMarketTab('mine');
}

async function loadClosetMine() {
  if (!closetMarketEnabled) return;
  const data = await api('/api/closet/orders/mine');
  const img = (x) => mineTraitImgSrc(x.slot, x.value, null);
  renderChipList(el('mine-closet-orders'), el('mine-closet-orders-empty'),
    data.orders.map((o) => ({ imgSrc: img(o), label: closetPure.orderChipLabel(o), payload: o })), 'Cancel', cancelClosetOrder);
  renderChipList(el('mine-closet-incoming'), el('mine-closet-incoming-empty'),
    data.bids_on_my_traits.map((b) => ({ imgSrc: img(b), label: closetPure.holdingLabel(b), payload: b })), 'Fill', fillClosetBid);
  renderChipList(el('mine-closet-fills'), el('mine-closet-fills-empty'),
    data.fills.map((f) => ({ imgSrc: img(f), label: closetPure.fillChipLabel(f, me && me.wallet), payload: f })), 'View', viewClosetFill);
}

async function cancelClosetOrder(order) {
  const isBid = order.side === 'bid';
  const ok = await confirmDialog({
    title: isBid ? 'Cancel this bid?' : 'Unlist this trait?',
    text: isBid ? 'Your escrowed BRIX goes back to your wallet.' : 'It is free to use again right away.',
    confirmLabel: isBid ? 'Cancel bid' : 'Unlist',
  });
  if (!ok) return;
  await api(`/api/closet/${order.side}/${order.id}`, { method: 'DELETE' });
  await loadMarketMine();
}

async function fillClosetBid(entry) {
  const action = closetPure.holdingFillAction(entry);
  const ok = await confirmDialog({
    title: `Sell ${entry.slot}: ${entry.value} for ${entry.price_brix} BRIX?`,
    text: closetPure.fillDisclosure(entry.price_brix, closetMarketFeeBps, action.needsDeposit),
    confirmLabel: action.label,
  });
  if (!ok) return;
  if (action.needsDeposit) {
    status('Depositing trait…');
    const res = await api('/api/deposit', { method: 'POST', body: JSON.stringify({ nft_id: action.nftId }) });
    const final = await pollEconomyOp('deposit', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'deposit failed');
  }
  await marketFlow('closet_fill', `/api/closet/bid/${entry.id}/fill`, {}, closetFillRender);
}

async function viewClosetFill(fill) {
  const s = await api(`/api/closet/fill/${fill.id}`);
  showFlow(closetFillRender(s));
  if (!marketPure.isMarketTerminal(s.state)) pollMarketFlow('closet_fill', s.id, closetFillRender);
}
```

- [ ] **Step 7: app.js — hook existing surfaces**

`renderMarketGrid`: directly after the `#481` count-badge block:

```js
    const bidBadge = closetMarketEnabled ? closetPure.bookBadge(row.book) : null;
    if (bidBadge) {
      const b = document.createElement('span');
      b.className = 'market-card-bid';
      b.textContent = bidBadge;
      card.appendChild(b);
    }
```

`openListingDetail`: replace the `const canBid = ...` line through the `el('listing-bid-confirm').onclick = ...` handler with:

```js
  // #496: with the Closet market on, a trait card takes a (slot, value) BRIX
  // bid — escrowed, fillable by any holder — instead of the character-only
  // native NFTokenOffer bid.
  const traitBid = closetMarketEnabled && vm.kind === 'trait';
  const canBid = traitBid || (vm.kind === 'character' && (!me || !me.wallet || me.wallet !== vm.seller));
  bidBtn.hidden = !canBid;
  el('listing-bid-price').placeholder = traitBid ? 'Bid in BRIX' : 'Bid in XRP';
  if (traitBid && closetPure.bookLine(row.book)) {
    bidsLine.textContent = closetPure.bookLine(row.book);
    bidsLine.hidden = false;
  }
  bidBtn.onclick = () => { bidForm.hidden = !bidForm.hidden; if (!bidForm.hidden) el('listing-bid-price').focus(); };
  el('listing-bid-confirm').onclick = () => {
    const price = el('listing-bid-price').value.trim();
    const checked = traitBid ? marketPure.validateBrixPrice(price) : marketPure.validatePrice(price);
    if (!checked.ok) { showError(checked.error); return; }
    (traitBid ? placeClosetBid(vm.slot, vm.value, price) : placeBid(row, price)).catch((e) => showError(e.message));
  };
```

`renderMineGroups`: replace the `closetEntries` block with:

```js
  const closetEntries = data.closet_assets.map((a) => ({
    imgSrc: mineTraitImgSrc(a.slot, a.value, a.image_url),
    label: closetPure.closetAssetLabel(a),
    // #443: with the Closet market on, Sell posts a free off-ledger ask; the
    // Extract -> List wizard stays reachable through Unlisted traits.
    payload: { slot: a.slot, value: a.value, label: `${a.slot}: ${a.value}`, wizard: !closetMarketEnabled, closetAsk: closetMarketEnabled },
  }));
  renderChipList(el('mine-closet'), el('mine-closet-empty'), closetEntries, 'Sell', openListForm);
```

`loadMarketMine`: as its last statement add `loadClosetMine().catch((e) => showError(e.message));`.

`openListForm`: set the title with `item.closetAsk ? 'List in the Closet market' : item.wizard ? 'Sell a trait' : 'List for sale'`, and the placeholder with `item.closetAsk || listFormIsTrait(item) ? 'Price in BRIX' : 'Price in XRP'`.

`updateListFormRoyaltyPreview`: first lines after `const raw = ...`:

```js
  if (marketPendingItem && marketPendingItem.closetAsk) {
    const check = marketPure.validateBrixPrice(raw);
    out.hidden = !check.ok;
    if (check.ok) out.textContent = closetPure.askDisclosure(raw, closetMarketFeeBps);
    return;
  }
```

`submitListForm`: first lines after `const price = ...`:

```js
  if (item.closetAsk) {
    const check = marketPure.validateBrixPrice(price);
    if (!check.ok) { showError(check.error); return; }
    const ok = await confirmDialog({ title: 'List this trait?', text: closetPure.askDisclosure(price, closetMarketFeeBps), confirmLabel: 'List it' });
    if (!ok) return;
    await postClosetAsk(item, price);
    return;
  }
```

`main()`: after `applyShopVisibility(cfg);` add `applyClosetMarketVisibility(cfg);`. Beside the `market-list-confirm-btn` wiring:

```js
  el('closet-bid-new-btn').onclick = () => openClosetBidForm().catch((e) => showError(e.message));
  el('closet-bid-confirm-btn').onclick = () => submitClosetBidForm().catch((e) => showError(e.message));
  el('closet-bid-cancel-btn').onclick = () => showPanel('market-panel');
  el('closet-bid-price').oninput = () => {
    const raw = el('closet-bid-price').value.trim();
    el('closet-bid-note').textContent = closetPure.bidDisclosure(marketPure.validateBrixPrice(raw).ok ? raw : null, closetBidTtlDays);
  };
```

- [ ] **Step 8: Run tests**

Run: `.venv/bin/python -m pytest tests/test_closet_market_dom.py tests/test_market_panel_dom.py tests/test_harvest_pure_js.py tests/test_app_js_deeplink.py tests/test_brix_card_dom.py tests/test_closet_market_pure_js.py -v`
Expected: PASS

- [ ] **Step 9: Syntax check the module graph under Node**

Run: `node --check webapp/client/app.js && node --input-type=module -e "import('./webapp/client/closet_market_pure.js').then(() => console.log('ok'))"`
Expected: no output from `--check`, then `ok`

- [ ] **Step 10: Commit**

```bash
git add webapp/client/ tests/test_closet_market_dom.py tests/test_harvest_pure_js.py tests/test_app_js_deeplink.py
git commit -m "feat(closet-market): Wanted tab, trait bids from cards, Closet orders in Mine (#443, #496)"
```

---

### Task 16: Testnet end-to-end, full verification, PR

**Files:**
- Create: `scripts/closet_market_e2e.py`

**Interfaces:**
- Consumes: everything above, against the real testnet ledger

- [ ] **Step 1: Write the e2e script**

```python
#!/usr/bin/env python3
"""Testnet end-to-end for the Closet Market ledger path (#443).

Covers what unit tests cannot: a real BRIX TokenEscrow created with our
PREIMAGE-SHA-256 condition, EscrowFinish with the sealed fulfillment, app
forward/refund Payments, memo lookups, EscrowCancel after CancelAfter.

The Xaman signature is replaced by signing the bidder's EscrowCreate locally,
the Closet mirror is a no-op (no real Closet token is touched), and orders and
fills live in a throwaway sqlite file — the staging order book is never used.

Prereqs: XRPL_NETWORK=testnet ECONOMY_NETWORK=testnet, run from a staging
checkout (SEED = testnet BRIX issuer = app wallet), and the issuer flag set:
  .venv/bin/python scripts/closet_market_setup.py --network testnet --apply-issuer-flag
Usage:
  .venv/bin/python scripts/closet_market_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from cryptography.fernet import Fernet  # noqa: E402
from xrpl.clients import JsonRpcClient  # noqa: E402
from xrpl.models import IssuedCurrencyAmount  # noqa: E402
from xrpl.models.transactions import EscrowCreate, Payment, TrustSet  # noqa: E402
from xrpl.transaction import submit_and_wait  # noqa: E402
from xrpl.wallet import Wallet, generate_faucet_wallet  # noqa: E402

from lfg_core import closet_market_flow as cmf  # noqa: E402
from lfg_core import closet_market_store as cms  # noqa: E402
from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config, crypto_condition, economy_store, memos, xrpl_ops  # noqa: E402

PRICE = "10"


def _brix(value: str) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(currency=config.BRIX_CURRENCY_HEX, issuer=config.BRIX_ISSUER, value=value)


def _fund_with_brix(client: JsonRpcClient, wallet: Wallet, amount: str) -> None:
    submit_and_wait(TrustSet(account=wallet.classic_address, limit_amount=_brix("1000000")), client, wallet)
    if Decimal(amount) > 0:
        issuer = Wallet.from_seed(config.SEED)
        submit_and_wait(
            Payment(account=config.SIGNING_ACCOUNT, destination=wallet.classic_address, amount=_brix(amount),
                    source_tag=config.SOURCE_TAG,
                    memos=memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_PAYMENT)),
            client, issuer,
        )


def _balance(address: str) -> Decimal:
    value = asyncio.run(xrpl_ops.get_trustline_balance(address, config.BRIX_CURRENCY_HEX, config.BRIX_ISSUER))
    return value if value is not None else Decimal(0)


def _create_escrow(client: JsonRpcClient, bidder: Wallet, condition: str, cancel_after: int) -> dict:
    tx = EscrowCreate(
        account=bidder.classic_address, destination=config.SIGNING_ACCOUNT, amount=_brix(PRICE),
        condition=condition, cancel_after=cancel_after, source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(memos.INITIATOR_USER, memos.PLATFORM_BACKEND, memos.ACTION_CLOSET_BID),
    )
    result = submit_and_wait(tx, client, bidder).result
    assert result["meta"]["TransactionResult"] == "tesSUCCESS", result["meta"]["TransactionResult"]
    return result


def _deps(path: str, statuses: dict) -> cmf.ClosetMarketDeps:
    async def payload_status(uuid):
        return statuses.get(uuid)

    async def no_mirror(conn, owner):
        return None

    def factory():
        conn = sqlite3.connect(path)
        economy_store.init_economy_schema(conn)
        return conn

    return cmf.ClosetMarketDeps(
        conn_factory=factory, app_account=config.SIGNING_ACCOUNT, fee_bps=700,
        payload_status_fn=payload_status, get_tx_fn=xrpl_ops.get_tx, get_escrow_fn=xrpl_ops.get_escrow,
        escrow_finish_fn=xrpl_ops.escrow_finish, escrow_cancel_fn=xrpl_ops.escrow_cancel,
        payment_fn=xrpl_ops.app_brix_payment, find_txs_fn=xrpl_ops.find_app_txs_by_memo,
        ledger_index_fn=xrpl_ops.current_validated_ledger_index, mirror_fn=no_mirror,
        unseal_fn=crypto_condition.unseal, records_dir=os.path.dirname(path),
    )


def _open_bid(client, path, statuses, bidder: Wallet, cancel_after: int) -> str:
    pre = crypto_condition.new_preimage()
    condition = crypto_condition.condition_hex(pre)
    conn = sqlite3.connect(path)
    economy_store.init_economy_schema(conn)
    order = cms.create_pending_bid(
        conn, owner=bidder.classic_address, slot="Head", value="Crown", price_brix=PRICE, platform=None,
        condition=condition, fulfillment_enc=crypto_condition.seal(crypto_condition.fulfillment_hex(pre)),
        cancel_after=cancel_after, payload_uuid=f"e2e-{time.time_ns()}", xumm_url=None, qr_url=None, push=None,
    )
    conn.close()
    created = _create_escrow(client, bidder, condition, cancel_after)
    statuses[order["payload_uuid"]] = {"signed": True, "account": bidder.classic_address, "txid": created["hash"]}
    asyncio.run(cmf.advance_bid(order["id"], _deps(path, statuses)))
    conn = sqlite3.connect(path)
    state = cms.get_order(conn, order["id"])["state"]
    conn.close()
    assert state == cms.OPEN, f"bid did not open: {state}"
    return str(order["id"])


def main() -> int:
    if config.XRPL_NETWORK != "testnet" or config.ECONOMY_NETWORK != "testnet":
        print("refusing: testnet only")
        return 2
    config.CLOSET_MARKET_ENC_KEY = Fernet.generate_key().decode()
    client = JsonRpcClient(config.JSON_RPC_URL)
    bidder, seller = generate_faucet_wallet(client), generate_faucet_wallet(client)
    _fund_with_brix(client, bidder, "100")
    _fund_with_brix(client, seller, "0")
    path = os.path.join(tempfile.mkdtemp(prefix="closet-e2e-"), "onchain_e2e.db")
    conn = sqlite3.connect(path)
    economy_store.init_economy_schema(conn)
    for w in (bidder, seller):
        economy_store.set_closet_token(conn, w.classic_address, f"E2E-{w.classic_address}", "00", status=ct.ACTIVE)
    economy_store.set_closet_contents(conn, seller.classic_address, [("Head", "Crown", 1)], [])
    conn.commit()
    conn.close()
    statuses: dict = {}
    ripple_now = int(time.time()) - xrpl_ops.RIPPLE_EPOCH_OFFSET
    ok = True

    # 1. holder fill: escrow finish -> move -> forward 9.3 -> mirrored
    bid_id = _open_bid(client, path, statuses, bidder, ripple_now + 3600)
    before_b, before_s = _balance(bidder.classic_address), _balance(seller.classic_address)
    conn = sqlite3.connect(path)
    fill = cms.fill_bid(conn, bid_id, seller.classic_address, fee_bps=700, platform=None)
    conn.close()
    state = asyncio.run(cmf.settle_fill(fill["id"], _deps(path, statuses)))
    got_s = _balance(seller.classic_address) - before_s
    print(f"[fill] state={state} seller +{got_s} bidder {_balance(bidder.classic_address) - before_b}")
    ok &= state == cms.MIRRORED and got_s == Decimal("9.3")

    # 2. user cancel before expiry: EscrowFinish + refund Payment
    bid_id = _open_bid(client, path, statuses, bidder, ripple_now + 3600)
    before_b = _balance(bidder.classic_address)
    conn = sqlite3.connect(path)
    cms.begin_cancel_bid(conn, bid_id, owner=bidder.classic_address, reason="user")
    conn.close()
    asyncio.run(cmf.advance_bid(bid_id, _deps(path, statuses)))
    conn = sqlite3.connect(path)
    state = cms.get_order(conn, bid_id)["state"]
    conn.close()
    print(f"[cancel] state={state} bidder +{_balance(bidder.classic_address) - before_b}")
    ok &= state == cms.CANCELLED and _balance(bidder.classic_address) - before_b == Decimal(PRICE)

    # 3. expiry: EscrowCancel after CancelAfter
    bid_id = _open_bid(client, path, statuses, bidder, int(time.time()) - xrpl_ops.RIPPLE_EPOCH_OFFSET + 30)
    before_b = _balance(bidder.classic_address)
    time.sleep(30 + cmf.CANCEL_SLACK_SECONDS + 10)
    asyncio.run(cmf.advance_bid(bid_id, _deps(path, statuses)))
    conn = sqlite3.connect(path)
    state = cms.get_order(conn, bid_id)["state"]
    conn.close()
    print(f"[expire] state={state} bidder +{_balance(bidder.classic_address) - before_b}")
    ok &= state == cms.EXPIRED and _balance(bidder.classic_address) - before_b == Decimal(PRICE)

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Lint the script**

Run: `.venv/bin/ruff check scripts/closet_market_e2e.py scripts/audit_closet_market.py scripts/closet_market_setup.py && .venv/bin/ruff format --check scripts/closet_market_e2e.py`
Expected: clean.

- [ ] **Step 3: Full local gate**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service webapp`
(`scripts/` is excluded from mypy in `pyproject.toml`; do not pass script paths explicitly, or the exclusion is bypassed.)
Expected: all green. Record the pytest pass count in the PR body.

- [ ] **Step 4: Testnet e2e on the staging checkout**

The staging box's `.env` has the testnet SEED; never run this against mainnet.

```bash
cd ~/LFG-staging   # after this branch is checked out there, or run from the worktree with the staging .env exported
.venv/bin/python scripts/closet_market_setup.py --network testnet --apply-issuer-flag
.venv/bin/python scripts/closet_market_e2e.py
```

Expected: `[fill] state=mirrored seller +9.3`, `[cancel] state=cancelled bidder +10`, `[expire] state=expired bidder +10`, `PASS`.
If `[fill]` fails with `tecNO_PERMISSION`, the issuer flag is not set. If it fails with `tecCRYPTOCONDITION_ERROR`, the encoding in Task 2 is wrong (re-check the published vector test).

- [ ] **Step 5: Commit and push**

```bash
git add scripts/closet_market_e2e.py
git commit -m "test(closet-market): testnet end-to-end for escrow fill, cancel and expiry (#443)"
git push -u origin HEAD
```

- [ ] **Step 6: Open the PR (ready, not draft; no AI attribution per the user's global rules)**

```bash
gh pr create --repo Team-Hamsa/LFG --base main --title "feat(closet-market): off-ledger trait asks + TokenEscrow bids (#443, #496)" --body "Closes #443. Folds in #496.

Plan: docs/superpowers/plans/2026-09-14-closet-market.md (incl. spec deltas)
Ops: docs/ops/closet-market.md — ships dark (CLOSET_MARKET_ENABLED=0).

Verification: pytest <N> passed; testnet e2e PASS (fill / cancel / expire)."
```

Then wait for BOTH Greptile and CodeRabbit. Fix every actionable finding and reply on its thread naming the fixing commit. Re-trigger with `@greptile-apps please re-review` and `@coderabbitai review` until clean. Check Greptile's verdict in the check-run `output.summary`, not the reviews list.

- [ ] **Step 7: Post-merge ops checklist (comment on #443; the user runs the mainnet steps)**

```bash
gh issue comment 443 --repo Team-Hamsa/LFG --body "Merged. Go-live (mainnet, operator-run — docs/ops/closet-market.md):
- [ ] closet_market_setup.py --network mainnet (check)
- [ ] --apply-issuer-flag (rLfgoBriX…, signed by BRIX_DISTRIBUTOR_SEED; one-way)
- [ ] --apply-app-limit (rLfgoMint… BRIX line 258,055 -> BRIX_TRUSTLINE_LIMIT)
- [ ] CLOSET_MARKET_ENC_KEY generated + backed up; env set on prod
- [ ] pm2 start ecosystem.prod.config.js --only lfg-closet-market-audit && pm2 save
- [ ] promote.sh, then CLOSET_MARKET_ENABLED=1
- [ ] real-Xaman smoke: ask, buy (BRIX + XRP path), bid, fill, deposit&fill, cancel"
```

---

## Self-review notes (for the executor)

- **Spec coverage:**
  - Data model → Tasks 3–4. Encumbrance → 3, 5.
  - API table → 10–11 (the one fill-status endpoint and `/keys` are deltas 6).
  - Settlement steps 1–4 → 9. Auto-cross → 4, with re-cross on sweep → 8.
  - Races/refunds → 4, 9. Bid cancel/expiry → 8.
  - Ops/config/audit → 1, 13.
  - Client → 14–15. Testing section → the tests in each task, plus 16.
  - #496 additions → 4 (`bids_on_holdings`), 12 (`book`), 15 (card badge, trait bid in detail, Deposit & fill).
- **Ordering risk:** Task 5 imports `closet_market_store` into `economy_flow`, and Task 3 imports it into `economy_store`. `closet_market_store` must stay free of `lfg_core` imports (it is). `closet_token` imports it lazily inside `_orders_for_meta`.
- **Names used across tasks:**
  - Store: `cross_incoming`, `fill_bid`, `create_take_fill`, `claim_ask_for_payment`, `move_asset`, `mark_side_mirrored`, `has_unmirrored_fill`, `bids_on_holdings`, `begin_cancel_bid`, `mark_bid_open`.
  - Flow: `advance_bid`, `settle_fill`.
  - Service: `_schedule_closet_settle`, `_schedule_closet_bid`, `_closet_market_deps`, `_closet_db`, `_CLOSET_BOOK_CACHE`.
  - Client: `closetPure.*`.

  Grep for each before implementing a dependent task.
