# Testnet BRIX/XRP AMM Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable, idempotent script that stands up an XRP/BRIX AMM pool on XRPL testnet, verifies a swap clears through it, and document the pool — satisfying issue #26.

**Architecture:** A single standalone ops script (`scripts/testnet_amm_setup.py`) signed by the SEED account (the testnet BRIX issuer). Pure, branch-free helpers (flag check, fee calc, summary formatting) are unit-tested with TDD; the live orchestration (`main()`) is verified by an actual run against testnet, which itself produces the acceptance evidence (AC #1 + #2). No application code changes — the existing swap flow already reads the pool via `AMMInfo`.

**Tech Stack:** Python 3.10, xrpl-py 5.0.0, pytest, the existing `lfg_core.config` / `lfg_core.xrpl_ops` modules.

## Global Constraints

- **Testnet only.** The script MUST abort unless `lfg_core.config.IS_TESTNET` is true. Never touch mainnet.
- **Pool params (verbatim):** Amount = 50 XRP (`xrp_to_drops(50)` = `"50000000"` drops); Amount2 = `"5000"` BRIX; `trading_fee = 500` (0.5%).
- **BRIX identity:** currency = `config.SWAP_OFFER_CURRENCY_HEX` (`"4252495800000000000000000000000000000000"`); issuer = `config.SWAP_OFFER_ISSUER` (on testnet = the SEED account address).
- **AMMCreate special fee:** at least the network's incremental owner reserve (currently 0.2 XRP). Read `reserve_inc_xrp` live from `ServerInfo` and pass it as the explicit `fee`; autofill does NOT set the AMMCreate special fee.
- **Default Ripple:** `lsfDefaultRipple = 0x00800000`; `AccountSetAsfFlag.ASF_DEFAULT_RIPPLE = 8`.
- **Quality gate (pre-push, blocking):** code must pass `ruff`, `mypy` (strict), and `pytest`. All functions fully type-annotated. Run via `.venv/bin/python` / `.venv/bin/pytest`.
- **No DB writes, no CDN, no edits to `main.py` / webapp / swap flow.**

---

### Task 1: Pure helpers + unit tests

Build the branch-free, network-free logic first so it's unit-tested in isolation. These functions are imported by `main()` in Task 2.

**Files:**
- Create: `scripts/testnet_amm_setup.py`
- Test: `tests/test_testnet_amm_setup.py`

**Interfaces:**
- Produces:
  - `default_ripple_enabled(flags: int) -> bool` — True iff the `lsfDefaultRipple` bit is set.
  - `amm_create_fee_drops(reserve_inc_xrp: float) -> str` — the AMMCreate special `fee`, in drops, as a string (the incremental owner reserve converted to drops).
  - `format_pool_summary(amm_account: str, xrp_amount: str, brix_amount: str, trading_fee: int) -> str` — a human-readable multi-line summary block for documentation/console.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_testnet_amm_setup.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import testnet_amm_setup as amm  # noqa: E402


def test_default_ripple_enabled_true_when_bit_set() -> None:
    assert amm.default_ripple_enabled(0x00800000) is True
    assert amm.default_ripple_enabled(0x00800000 | 0x00010000) is True


def test_default_ripple_enabled_false_when_unset() -> None:
    assert amm.default_ripple_enabled(0) is False
    assert amm.default_ripple_enabled(0x00010000) is False


def test_amm_create_fee_drops_converts_reserve_increment() -> None:
    # 0.2 XRP increment -> 200000 drops
    assert amm.amm_create_fee_drops(0.2) == "200000"
    # 2 XRP increment -> 2000000 drops
    assert amm.amm_create_fee_drops(2) == "2000000"


def test_format_pool_summary_contains_key_facts() -> None:
    out = amm.format_pool_summary("rAMMxxxxxxxxxxxxxxxxxxxxxxxxxx", "50000000", "5000", 500)
    assert "rAMMxxxxxxxxxxxxxxxxxxxxxxxxxx" in out
    assert "5000" in out
    assert "0.5%" in out  # trading_fee 500 -> 0.5%
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_testnet_amm_setup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'testnet_amm_setup'` (file not created yet).

- [ ] **Step 3: Write the script skeleton with the pure helpers**

```python
# scripts/testnet_amm_setup.py
"""Stand up (idempotently) an XRP/BRIX AMM pool on XRPL testnet for swap testing.

Issue: https://github.com/Team-Hamsa/LFG/issues/26
Recreate after a testnet reset with:  .venv/bin/python scripts/testnet_amm_setup.py

Signed by the SEED account, which on testnet is the BRIX issuer. Safe to re-run:
skips Default-Ripple if already set and skips AMMCreate if the pool already exists.
"""

from __future__ import annotations

from decimal import Decimal

LSF_DEFAULT_RIPPLE = 0x00800000

# Pool parameters (see docs/superpowers/specs/2026-06-17-testnet-brix-amm-design.md)
XRP_AMOUNT_DROPS = "50000000"  # 50 XRP
BRIX_AMOUNT = "5000"
TRADING_FEE = 500  # 0.5% (units of 1/100000)
SWAP_TEST_BRIX = "10"  # 10 BRIX -> exercises the trait-swap fee path


def default_ripple_enabled(flags: int) -> bool:
    """True iff the account's lsfDefaultRipple flag bit is set."""
    return bool(flags & LSF_DEFAULT_RIPPLE)


def amm_create_fee_drops(reserve_inc_xrp: float) -> str:
    """AMMCreate special fee (in drops) = the network's incremental owner reserve.

    AMMCreate must destroy at least one incremental owner reserve; autofill does
    not set this, so it is passed explicitly as the transaction `fee`.
    """
    return str(int(Decimal(str(reserve_inc_xrp)) * 1_000_000))


def format_pool_summary(
    amm_account: str, xrp_amount: str, brix_amount: str, trading_fee: int
) -> str:
    """Human-readable summary block for the console and CLAUDE.md."""
    xrp = Decimal(xrp_amount) / 1_000_000
    fee_pct = Decimal(trading_fee) / 1000  # 500 -> 0.5
    return (
        "=== Testnet XRP/BRIX AMM ===\n"
        f"AMM account (pool ID): {amm_account}\n"
        f"Pair: {xrp} XRP : {brix_amount} BRIX\n"
        f"Price: {(xrp / Decimal(brix_amount)).normalize()} XRP/BRIX\n"
        f"Trading fee: {fee_pct}%"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_testnet_amm_setup.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint & type-check**

Run: `.venv/bin/ruff check scripts/testnet_amm_setup.py tests/test_testnet_amm_setup.py && .venv/bin/mypy scripts/testnet_amm_setup.py`
Expected: no errors. (Fix `mypy`/`ruff` findings inline if any.)

- [ ] **Step 6: Commit**

```bash
git add scripts/testnet_amm_setup.py tests/test_testnet_amm_setup.py
git commit -m "feat: testnet AMM setup helpers with unit tests (#26)"
```

---

### Task 2: Live orchestration (`main()`) + testnet run

Wire the helpers and live RPC calls into the idempotent flow, then run it against testnet. The run is the integration test and produces the acceptance evidence (AC #1: pool created; AC #2: swap clears).

**Files:**
- Modify: `scripts/testnet_amm_setup.py` (add imports + `main()` + `__main__` guard)

**Interfaces:**
- Consumes: `default_ripple_enabled`, `amm_create_fee_drops`, `format_pool_summary` (Task 1); `lfg_core.config` (`IS_TESTNET`, `SEED`, `JSON_RPC_URL`, `SWAP_OFFER_CURRENCY_HEX`, `SWAP_OFFER_ISSUER`, `SWAP_XRP_FEE_BUFFER`); `lfg_core.xrpl_ops.get_amm_xrp_cost`, `lfg_core.xrpl_ops.buy_and_burn`.
- Produces: `async def main() -> int` — returns process exit code (0 success, non-zero abort).

- [ ] **Step 1: Add imports and `main()` to `scripts/testnet_amm_setup.py`**

Insert after the helper functions (keep the existing helpers and constants):

```python
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xrpl.asyncio.clients import AsyncJsonRpcClient  # noqa: E402
from xrpl.asyncio.transaction import submit_and_wait  # noqa: E402
from xrpl.models.currencies import XRP, IssuedCurrency  # noqa: E402
from xrpl.models.requests import AccountInfo, AMMInfo, ServerInfo  # noqa: E402
from xrpl.models.transactions import (  # noqa: E402
    AccountSet,
    AccountSetAsfFlag,
    AMMCreate,
)
from xrpl.models.amounts import IssuedCurrencyAmount  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

from lfg_core import config, xrpl_ops  # noqa: E402


def _tx_result(response: object) -> str:
    """Pull the engine result string out of a submit_and_wait response."""
    return response.result["meta"]["TransactionResult"]  # type: ignore[attr-defined,index,no-any-return]


async def main() -> int:
    if not config.IS_TESTNET:
        print("ABORT: XRPL_NETWORK is not 'testnet'. Refusing to run.", file=sys.stderr)
        return 1

    wallet = Wallet.from_seed(config.SEED)
    client = AsyncJsonRpcClient(config.JSON_RPC_URL)
    issuer = config.SWAP_OFFER_ISSUER
    currency = config.SWAP_OFFER_CURRENCY_HEX
    print(f"Network: testnet | Account/issuer: {wallet.classic_address}")

    # 1. Default Ripple (required for the token to be holdable / AMM-eligible)
    info = await client.request(AccountInfo(account=wallet.classic_address, ledger_index="validated"))
    flags = int(info.result["account_data"].get("Flags", 0))
    if default_ripple_enabled(flags):
        print("Default Ripple: already enabled.")
    else:
        print("Default Ripple: enabling...")
        resp = await submit_and_wait(
            AccountSet(account=wallet.classic_address, set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE),
            client,
            wallet,
        )
        result = _tx_result(resp)
        if result != "tesSUCCESS":
            print(f"ABORT: AccountSet failed: {result}", file=sys.stderr)
            return 1
        print("Default Ripple: enabled.")

    # 2. Idempotency: skip if the pool already exists
    asset2 = IssuedCurrency(currency=currency, issuer=issuer)
    try:
        existing = await client.request(AMMInfo(asset=XRP(), asset2=asset2))
        amm = existing.result.get("amm")
    except Exception:
        amm = None
    if amm:
        print("AMM already exists — skipping creation.")
        print(format_pool_summary(amm["account"], XRP_AMOUNT_DROPS, BRIX_AMOUNT, TRADING_FEE))
        return await _verify_swap(currency, issuer)

    # 3. Create the pool. Fee = incremental owner reserve (read live).
    si = await client.request(ServerInfo())
    reserve_inc = float(si.result["info"]["validated_ledger"]["reserve_inc_xrp"])
    fee = amm_create_fee_drops(reserve_inc)
    print(f"Creating AMM (fee {fee} drops)...")
    resp = await submit_and_wait(
        AMMCreate(
            account=wallet.classic_address,
            amount=XRP_AMOUNT_DROPS,
            amount2=IssuedCurrencyAmount(currency=currency, issuer=issuer, value=BRIX_AMOUNT),
            trading_fee=TRADING_FEE,
            fee=fee,
        ),
        client,
        wallet,
    )
    result = _tx_result(resp)
    if result != "tesSUCCESS":
        print(f"ABORT: AMMCreate failed: {result}", file=sys.stderr)
        return 1
    print("AMMCreate: tesSUCCESS")

    # 4. Verify pool exists and print summary
    confirmed = await client.request(AMMInfo(asset=XRP(), asset2=asset2))
    amm = confirmed.result["amm"]
    print(format_pool_summary(amm["account"], XRP_AMOUNT_DROPS, BRIX_AMOUNT, TRADING_FEE))

    # 5. Verify a swap clears through the pool (AC #2)
    return await _verify_swap(currency, issuer)


async def _verify_swap(currency: str, issuer: str) -> int:
    """Run the production XRP->BRIX path (quote + buy_and_burn) to prove the pool
    clears a swap. Mirrors the trait-swap XRP-fee path exactly."""
    print(f"Verifying swap: quoting {SWAP_TEST_BRIX} BRIX...")
    quote = await xrpl_ops.get_amm_xrp_cost(currency, issuer, Decimal(SWAP_TEST_BRIX))
    if quote is None:
        print("ABORT: AMM quote unavailable after creation.", file=sys.stderr)
        return 1
    max_xrp = str((quote * Decimal(config.SWAP_XRP_FEE_BUFFER)).quantize(Decimal("0.000001")))
    print(f"Quote: {quote} XRP (max_xrp {max_xrp}). Running buy_and_burn...")
    tx_hash = await xrpl_ops.buy_and_burn(currency, issuer, SWAP_TEST_BRIX, max_xrp=max_xrp)
    if tx_hash is None:
        print("ABORT: buy_and_burn through AMM failed.", file=sys.stderr)
        return 1
    print(f"Swap verified through AMM. tx: {tx_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Lint & type-check**

Run: `.venv/bin/ruff check scripts/testnet_amm_setup.py && .venv/bin/mypy scripts/testnet_amm_setup.py`
Expected: no errors. Fix inline (e.g., adjust `# type: ignore` comments if mypy points elsewhere).

- [ ] **Step 3: Confirm unit tests still pass**

Run: `.venv/bin/pytest tests/test_testnet_amm_setup.py -v`
Expected: PASS (4 tests) — adding `main()` must not break the helpers.

- [ ] **Step 4: Run against testnet (the integration test / acceptance evidence)**

Run: `.venv/bin/python scripts/testnet_amm_setup.py`
Expected output (in order): account line → "Default Ripple: enabling..." then "enabled." → "Creating AMM..." → "AMMCreate: tesSUCCESS" → the summary block with a real `rAMM…` account → "Quote: … XRP" → "Swap verified through AMM. tx: …". Process exits 0.

If `AMMCreate` returns a non-`tesSUCCESS` (e.g., the ledger rejects the issuer self-AMM), STOP and report the exact engine result — do not paper over it (see spec Risks).

- [ ] **Step 5: Confirm idempotency**

Run: `.venv/bin/python scripts/testnet_amm_setup.py` (second time)
Expected: "Default Ripple: already enabled." → "AMM already exists — skipping creation." → summary → swap verified → exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/testnet_amm_setup.py
git commit -m "feat: live AMMCreate + swap verification for testnet pool (#26)"
```

---

### Task 3: Document the pool + link spec/plan to issue

**Files:**
- Modify: `CLAUDE.md` (add a "Testnet AMM" subsection under "XRPL Integration")

**Interfaces:** none (documentation).

- [ ] **Step 1: Add the Testnet AMM subsection to `CLAUDE.md`**

Under the `## XRPL Integration` section, append (replace `<AMM_ACCOUNT>` with the real `rAMM…` address printed by the Task 2 run):

```markdown
### Testnet AMM (BRIX/XRP)

- **AMM account (pool ID):** `<AMM_ACCOUNT>`
- **Pair / ratio:** 50 XRP : 5,000 BRIX (BRIX issuer = SEED account on testnet)
- **Starting price:** 0.01 XRP/BRIX · **Trading fee:** 0.5%
- **Purpose:** lets the trait-swap XRP-fee path (`get_amm_xrp_cost` / `buy_and_burn`) quote and clear on testnet.
- **Recreate after a testnet reset:** `.venv/bin/python scripts/testnet_amm_setup.py` (idempotent).
```

- [ ] **Step 2: Commit the documentation**

```bash
git add CLAUDE.md
git commit -m "docs: record testnet BRIX/XRP AMM pool (#26)"
```

- [ ] **Step 3: Link spec + plan to issue #26 (project workflow requirement)**

Get the current commit SHA and post permalinks (per repo CLAUDE.md, brainstorm-from-issue sessions must link spec & plan):

```bash
SHA=$(git rev-parse HEAD)
gh issue comment 26 --repo Team-Hamsa/LFG --body "Spec: https://github.com/Team-Hamsa/LFG/blob/$SHA/docs/superpowers/specs/2026-06-17-testnet-brix-amm-design.md
Plan: https://github.com/Team-Hamsa/LFG/blob/$SHA/docs/superpowers/plans/2026-06-17-testnet-brix-amm.md"
```

- [ ] **Step 4: Verify acceptance criteria on the issue**

Confirm all three #26 boxes are satisfied by the run + docs:
- AMM pool created (Task 2 run, `tesSUCCESS` + summary).
- Swap verified end-to-end (Task 2 `_verify_swap`, tx hash).
- Pool documented in `CLAUDE.md` (Task 3).

Report the AMM account address and the swap tx hash to the user.

---

## Self-Review

- **Spec coverage:** AC #1 (create pool) → Task 2 steps 3–4. AC #2 (swap E2E) → Task 2 `_verify_swap` step 5/run. AC #3 (document) → Task 3. Default-Ripple prerequisite → Task 2 step 1. Idempotency → Task 2 steps 2 & 5. Reusable script → Tasks 1–2. Testnet guard → Task 2 `main()` first check.
- **Placeholder scan:** the only intentional placeholder is `<AMM_ACCOUNT>` / `<AMM_ACCOUNT>` in Task 3, filled from the live run — flagged as such. No TBD/TODO elsewhere; all code is complete.
- **Type consistency:** helper names (`default_ripple_enabled`, `amm_create_fee_drops`, `format_pool_summary`) match between Task 1 definitions, the Task 1 tests, and the Task 2 consumers. `main() -> int` and `_verify_swap(currency, issuer) -> int` return exit codes consistently; `_tx_result` used for both AccountSet and AMMCreate.
- **Note for implementer:** if `mypy` strict flags the xrpl-py response indexing, prefer targeted `# type: ignore[...]` over loosening types — the existing `lfg_core/xrpl_ops.py` uses the same pattern.
```

