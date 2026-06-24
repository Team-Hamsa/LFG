# Minter Burnable Flag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every NFT minted from the live minter burnable+transferable+mutable (`flags = 25`) so it is harvestable by the trait economy while still swappable in place.

**Architecture:** Bump the `NFT_FLAGS` default from `24` to `25` (add the `lsfBurnable` bit) in the two places that build mint transactions — `lfg_core/config.py` (used by `xrpl_ops.mint_nft`) and `main.py` (the `/letsgo` inline mint) — plus the `.env` runtime value. The swap flow is untouched because it classifies tokens by mutability, not burnability. Add named flag-bit constants so the value is self-documenting.

**Tech Stack:** Python 3, `xrpl-py` (`NFTokenMint`), `pytest`, `python-dotenv`.

## Global Constraints

- `SourceTag = 2606160021` must be set on every XRPL tx/payload (already handled by `xrpl_ops`; do not regress).
- Flag values (XLS-20 / Dynamic NFTs): `lsfBurnable = 0x0001`, `tfTransferable = 0x0008`, `tfMutable = 0x0010`. Target `NFT_FLAGS = 25 = 0x0001 | 0x0008 | 0x0010`.
- Forward-only: do NOT add migration/re-mint of existing flag-24 tokens.
- Do NOT change `ECONOMY_NFT_FLAGS` (stays `25`), `BUCKET_NFT_FLAGS` (`16`), or `lfg_core/swap_flow.py`.
- `lfg_core/config.py` calls `load_dotenv()` at import, so tests read the repo `.env`; keep `.env` and code defaults in agreement.

---

### Task 1: Named flag-bit constants + `config.NFT_FLAGS` default → 25

**Files:**
- Modify: `lfg_core/config.py:65-68`
- Modify: `.env` (local, gitignored) — set `NFT_FLAGS=25`
- Test: `tests/test_nft_flags.py` (create)

**Interfaces:**
- Produces: `config.NFT_FLAG_BURNABLE = 0x0001`, `config.NFT_FLAG_TRANSFERABLE = 0x0008`, `config.NFT_FLAG_MUTABLE = 0x0010`, and `config.NFT_FLAGS` (int, default `25`, env-overridable).

- [ ] **Step 1: Write the failing test**

Create `tests/test_nft_flags.py`:

```python
# New mints must be burnable (so the trait economy can harvest them) while
# remaining transferable + mutable (so trait swaps modify in place).
from lfg_core import config


def test_flag_bit_constants():
    assert config.NFT_FLAG_BURNABLE == 0x0001
    assert config.NFT_FLAG_TRANSFERABLE == 0x0008
    assert config.NFT_FLAG_MUTABLE == 0x0010


def test_default_nft_flags_compose_to_25():
    expected = (
        config.NFT_FLAG_BURNABLE
        | config.NFT_FLAG_TRANSFERABLE
        | config.NFT_FLAG_MUTABLE
    )
    assert expected == 25


def test_live_nft_flags_are_burnable_and_mutable():
    assert config.NFT_FLAGS & config.NFT_FLAG_BURNABLE, "mints must be burnable"
    assert config.NFT_FLAGS & config.NFT_FLAG_MUTABLE, "mints must stay mutable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nft_flags.py -v`
Expected: FAIL — `AttributeError: module 'lfg_core.config' has no attribute 'NFT_FLAG_BURNABLE'` (and `test_live_...` fails if `.env` still has `NFT_FLAGS=24`).

- [ ] **Step 3: Update `.env`**

Set the runtime value (add the line if absent):

```
NFT_FLAGS=25
```

- [ ] **Step 4: Add constants and recompose the default in `lfg_core/config.py`**

Replace the current block at `lfg_core/config.py:65-68`:

```python
# 24 = tfTransferable (8) + tfMutable (16): since the Dynamic NFTs amendment,
# new mints are NOT burnable — trait swaps update them in place via
# NFTokenModify instead of burn-and-remint.
NFT_FLAGS = int(os.getenv("NFT_FLAGS", "24"))
```

with:

```python
# XLS-20 / Dynamic NFTs NFToken flag bits.
NFT_FLAG_BURNABLE = 0x0001      # lsfBurnable — issuer may burn (required for Harvest)
NFT_FLAG_TRANSFERABLE = 0x0008  # tfTransferable
NFT_FLAG_MUTABLE = 0x0010       # tfMutable — Dynamic NFT, in-place NFTokenModify

# 25 = burnable + transferable + mutable. Burnable so the trait economy can
# harvest (issuer-burn) characters; mutable so trait swaps update in place
# (mutability, not burnability, selects the swap path — see swap_flow.py).
NFT_FLAGS = int(
    os.getenv(
        "NFT_FLAGS",
        str(NFT_FLAG_BURNABLE | NFT_FLAG_TRANSFERABLE | NFT_FLAG_MUTABLE),
    )
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nft_flags.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add tests/test_nft_flags.py lfg_core/config.py
git commit -m "feat(mint): make NFT_FLAGS burnable+transferable+mutable (25)"
```

(`.env` is gitignored — not staged; it is a local runtime change.)

---

### Task 2: Prove the mint transaction carries the burnable bit

**Files:**
- Test: `tests/test_nft_flags.py` (append)

**Interfaces:**
- Consumes: `config.NFT_FLAGS` (Task 1); `xrpl_ops.mint_nft(metadata_cdn_url, taxon, issuer, flags=None)` which defaults `flags` to `config.NFT_FLAGS` and builds an `NFTokenMint`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_nft_flags.py` (capture pattern mirrors `tests/test_xrpl_source_tag.py`):

```python
import asyncio

import lfg_core.xrpl_ops as xrpl_ops


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, result):
        self.result = result


def _capture_mint(monkeypatch, captured):
    def fake_submit(tx, client, wallet):
        captured["tx"] = tx
        return _Resp({"hash": "HASH", "meta": {"TransactionResult": "tesSUCCESS",
                                               "nftoken_id": "NFTID"}})

    def fake_request(self, req):
        return _Resp({"meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "NFTID"}})

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request)


def test_default_mint_is_burnable(monkeypatch):
    captured = {}
    _capture_mint(monkeypatch, captured)
    _run(xrpl_ops.mint_nft("https://x/m.json", taxon=1,
                           issuer=config.SWAP_ISSUER_ADDRESS))
    assert captured["tx"].flags & config.NFT_FLAG_BURNABLE, "mint tx must be burnable"
    assert captured["tx"].flags & config.NFT_FLAG_MUTABLE, "mint tx must stay mutable"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nft_flags.py::test_default_mint_is_burnable -v`
Expected: PASS (`config.NFT_FLAGS` already routes through `mint_nft`'s `eff_flags` default after Task 1 — this test guards against regression).

> Note: this is a guard test (green immediately after Task 1) — there is no separate implementation step; the wiring already exists in `xrpl_ops.mint_nft`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_nft_flags.py
git commit -m "test(mint): guard that mint tx carries the burnable bit"
```

---

### Task 3: Guard that flag-25 tokens still take the in-place swap path

**Files:**
- Test: `tests/test_nft_flags.py` (append)

**Interfaces:**
- Consumes: `swap_meta.normalize_nft(nft_id, metadata, flags=0, uri_hex="")` → dict with `"mutable": bool(flags & NFT_FLAG_MUTABLE)`; `swap_flow` selects `modify_items` when `nft["mutable"]` is truthy (`swap_flow.py:366`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_nft_flags.py`:

```python
from lfg_core import swap_meta


def test_flag25_token_is_mutable_so_swap_modifies_in_place():
    # A burnable+transferable+mutable (25) token must still report mutable, so
    # swap_flow routes it to modify_items (NFTokenModify), never burn-and-remint.
    rec = swap_meta.normalize_nft(
        "NFTID",
        {"name": "Let's Effing Go! #3540", "attributes": []},
        flags=25,
    )
    assert rec is not None
    assert rec["mutable"] is True
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nft_flags.py::test_flag25_token_is_mutable_so_swap_modifies_in_place -v`
Expected: PASS (`25 & 0x10 != 0`). Guard test — no implementation change.

> If this fails, the swap classification assumption in the spec is wrong — STOP and re-check `swap_flow.py:366` before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/test_nft_flags.py
git commit -m "test(swap): guard flag-25 tokens still swap in place (mutable)"
```

---

### Task 4: Bump the `/letsgo` inline mint default + update docs

**Files:**
- Modify: `main.py:142`
- Modify: `CLAUDE.md` (XRPL Integration section + `.env` template)

**Interfaces:**
- Consumes: nothing new. `main.py` builds its own `NFTokenMint(..., flags=NFT_FLAGS)` at `main.py:367` and `main.py:377` using the module-local `NFT_FLAGS` defined at `main.py:142`.

- [ ] **Step 1: Update the `main.py` default**

Replace `main.py:142`:

```python
NFT_FLAGS = int(os.getenv("NFT_FLAGS", "24"))
```

with:

```python
# 25 = burnable + transferable + mutable (see lfg_core/config.py). Burnable so
# the trait economy can harvest minted characters; mutable so swaps modify in
# place. Env (NFT_FLAGS) still overrides.
NFT_FLAGS = int(os.getenv("NFT_FLAGS", "25"))
```

- [ ] **Step 2: Verify both mint defaults now read 25**

Run: `grep -n 'os.getenv("NFT_FLAGS"' main.py lfg_core/config.py`
Expected: `main.py` shows `"25"`; `config.py` shows the composed `NFT_FLAG_* | ...` expression. No remaining `"24"` mint default.

- [ ] **Step 3: Update `CLAUDE.md`**

In the **XRPL Integration** section, replace the sentence that reads
`NFT flags = 24 (transferable + mutable — Dynamic NFTs amendment). New mints are NOT burnable; trait swaps update them in place via NFTokenModify ...`
with:

```
NFT flags = 25 (burnable + transferable + mutable — Dynamic NFTs amendment).
New mints ARE burnable so the dress-up trait economy can harvest (issuer-burn)
them. Trait swaps still update them in place via NFTokenModify — the swap path
is selected by mutability, not burnability (lfg_core/swap_flow.py). Legacy
non-mutable NFTs are still burned and reminted (now as burnable+mutable, per
NFT_FLAGS). NFTs minted before this change at flag 24 remain non-harvestable.
```

In the `.env` template block near the top of `CLAUDE.md`, change `NFT_FLAGS=24` to `NFT_FLAGS=25`.

- [ ] **Step 4: Run the full suite to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/test_nft_flags.py tests/test_xrpl_source_tag.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py CLAUDE.md
git commit -m "feat(mint): burnable /letsgo mints + docs; harvestable going forward"
```

---

## Verification (manual, testnet — post-merge or pre-merge on a testnet bot)

1. Mint one NFT via `/letsgo` (or `xrpl_ops.mint_nft`).
2. `account_nfts` on the issuer / check the on-chain index: confirm the new token's `Flags` includes `0x0001` (burnable) — i.e. `Flags & 1 == 1` and `Flags == 25`.
3. Run a trait swap on it; confirm via the swap journal it took the **modify** path (`"mode": "modify"`), not remint.
4. Run Harvest (`scripts/economy_harvest.py`) on it; confirm `can_harvest` passes and the assets land in the Bucket.

## Self-Review

- **Spec coverage:** flag-bit constants + `NFT_FLAGS=25` (Task 1); mint-path carries burnable bit (Task 2); swap-safety guard / mutability routing (Task 3); `main.py` `/letsgo` default + `CLAUDE.md` + `.env` docs (Task 1 §.env, Task 4); forward-only / no migration (Global Constraints + verification); `ECONOMY_NFT_FLAGS` untouched (Global Constraints). All spec sections mapped.
- **Placeholder scan:** none — every code/doc step shows exact content.
- **Type consistency:** `NFT_FLAG_BURNABLE` / `NFT_FLAG_TRANSFERABLE` / `NFT_FLAG_MUTABLE` and `NFT_FLAGS` named identically across Tasks 1–3; `mint_nft` and `normalize_nft` signatures match the live source.
