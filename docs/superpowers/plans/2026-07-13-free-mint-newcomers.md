# Free Mint for New Users — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give one free mint per platform-identity to newcomers who have never claimed and whose linked wallets own no live LFG character, on mainnet, with no per-surface tx code.

**Architecture:** A new append-only `wallet_links` history (so wallet switches no longer erase a user's past wallets) plus a `free_mint_claims` ledger (one reserve→confirm/release row per identity per network) live in the shared SQLite DB. A new `lfg_core/free_mint.py` computes eligibility and drives the claim lifecycle. `MintSession` gains a `free` path that skips the XUMM payment payload entirely (wallet control is already proven at connect) and records the claim atomically at mint success. All three surfaces inherit it through the shared spine.

**Tech Stack:** Python 3, aiohttp, sqlite3, xrpl-py, pytest. Follows the repo's self-migrating `CREATE TABLE IF NOT EXISTS` / forward-only `ADD COLUMN` boot pattern.

## Global Constraints

- **SourceTag** `2606160021` and provenance **Memos** must remain on every tx — the free path changes *nothing* about the mint tx except adding `campaign="free-mint"` to the memo. Never omit either.
- **Network-aware:** all claim rows carry `network`; ownership reads go through the per-network `onchain_<net>.db` (`lfg_core.nft_index.index_db_path(network)`). Feature is mainnet-scoped but schema is network-generic.
- **DB path:** the identity/claim DB is `lfg_core.user_db.DATABASE` (single source of truth). Do not hardcode a path.
- **`lfg_core` must not import `lfg_service`.** `free_mint.py` reads `identities`/`wallet_links` via raw SQL, never by importing the identity module.
- **Fail closed:** if eligibility cannot be computed (missing index, DB error), treat the identity as NOT eligible (charge normally). A free mint must never be handed out on an error.
- **Pre-push gate is blocking:** ruff, ruff-format, mypy (real types), pytest all run at pre-push. Every task ends green. Never `--no-verify`.

---

### Task 1: Append-only wallet history (`wallet_links`)

Stop losing wallets when a user re-links. Keep `identities.wallet` as the active pointer; additionally append every wallet ever linked.

**Files:**
- Modify: `lfg_service/identity.py` (`ensure_identities_table`, `link`)
- Test: `tests/test_wallet_links.py` (create)

**Interfaces:**
- Produces: table `wallet_links(platform, platform_user_id, wallet, linked_at, PK(platform,platform_user_id,wallet))`; `identity.link(...)` now also `INSERT OR IGNORE`s into it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wallet_links.py
import sqlite3
import lfg_core.user_db as user_db
import lfg_service.identity as identity


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    return str(db)


def test_link_appends_history_without_clobbering(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rWALLET_A")
    identity.link("discord", "u1", "alice", "rWALLET_B")  # switch wallets
    conn = sqlite3.connect(db)
    # active pointer updated
    active = conn.execute(
        "SELECT wallet FROM identities WHERE platform='discord' AND platform_user_id='u1'"
    ).fetchone()[0]
    assert active == "rWALLET_B"
    # both wallets retained in history
    hist = {r[0] for r in conn.execute(
        "SELECT wallet FROM wallet_links WHERE platform='discord' AND platform_user_id='u1'"
    )}
    assert hist == {"rWALLET_A", "rWALLET_B"}


def test_relinking_seen_wallet_is_noop(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rWALLET_A")
    identity.link("discord", "u1", "alice", "rWALLET_A")
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM wallet_links WHERE platform='discord' AND platform_user_id='u1'"
    ).fetchone()[0]
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_wallet_links.py -v`
Expected: FAIL — no `wallet_links` table.

- [ ] **Step 3: Implement**

In `ensure_identities_table()`, after the existing `identities` DDL/migrations and before `conn.commit()`, add:

```python
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_links (
                platform          TEXT NOT NULL,
                platform_user_id  TEXT NOT NULL,
                wallet            TEXT NOT NULL,
                linked_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, platform_user_id, wallet)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wallet_links_identity "
            "ON wallet_links(platform, platform_user_id)"
        )
```

In `link()`, inside the existing `try` on the same `conn`, after the `identities` INSERT and before `conn.commit()`:

```python
        conn.execute(
            "INSERT OR IGNORE INTO wallet_links (platform, platform_user_id, wallet) "
            "VALUES (?, ?, ?)",
            (platform, platform_user_id, wallet),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_wallet_links.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add lfg_service/identity.py tests/test_wallet_links.py
git commit -m "feat(identity): append-only wallet_links history"
```

---

### Task 2: Claim ledger + eligibility (`lfg_core/free_mint.py`)

**Files:**
- Create: `lfg_core/free_mint.py`
- Test: `tests/test_free_mint_eligibility.py` (create)

**Interfaces:**
- Consumes: `wallet_links` + `identities` (Task 1), `lfg_core.nft_index.index_db_path(network)` → `onchain_nfts(owner, is_burned)`.
- Produces:
  - `ensure_tables() -> None`
  - `wallets_for_identity(platform, platform_user_id) -> set[str]`
  - `is_eligible(platform, platform_user_id, network) -> bool`
  - `reserve_claim(platform, platform_user_id, network, wallet) -> bool` (True if this call won the row)
  - `confirm_claim(platform, platform_user_id, network, wallet, nft_number) -> None`
  - `release_claim(platform, platform_user_id, network) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_free_mint_eligibility.py
import sqlite3
import lfg_core.user_db as user_db
import lfg_core.free_mint as free_mint
import lfg_core.nft_index as nft_index
import lfg_service.identity as identity


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    free_mint.ensure_tables()
    # point ownership lookups at a controlled index db
    idx = tmp_path / "onchain_testnet.db"
    monkeypatch.setattr(nft_index, "index_db_path", lambda network: str(idx))
    conn = nft_index.init_db(str(idx))
    conn.close()
    return str(db), str(idx)


def _own(idx, owner, nft_id="00A", burned=0):
    conn = sqlite3.connect(idx)
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, owner, is_burned) VALUES (?, ?, ?)",
        (nft_id, owner, burned),
    )
    conn.commit()
    conn.close()


def test_newcomer_is_eligible(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    assert free_mint.is_eligible("discord", "u1", "testnet") is True


def test_owner_not_eligible(tmp_path, monkeypatch):
    _db, idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    _own(idx, "rA")
    assert free_mint.is_eligible("discord", "u1", "testnet") is False


def test_owner_under_historical_wallet_not_eligible(tmp_path, monkeypatch):
    _db, idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rOLD")
    identity.link("discord", "u1", "alice", "rNEW")  # switched; still owns via rOLD
    _own(idx, "rOLD")
    assert free_mint.is_eligible("discord", "u1", "testnet") is False


def test_reserved_or_claimed_not_eligible(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    assert free_mint.reserve_claim("discord", "u1", "testnet", "rA") is True
    assert free_mint.is_eligible("discord", "u1", "testnet") is False


def test_reserve_is_single_winner(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    first = free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    second = free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    assert (first, second) == (True, False)


def test_release_restores_eligibility(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    free_mint.release_claim("discord", "u1", "testnet")
    assert free_mint.is_eligible("discord", "u1", "testnet") is True


def test_confirm_blocks_and_records(tmp_path, monkeypatch):
    db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    free_mint.confirm_claim("discord", "u1", "testnet", "rA", 4242)
    assert free_mint.is_eligible("discord", "u1", "testnet") is False
    row = sqlite3.connect(db).execute(
        "SELECT status, nft_number FROM free_mint_claims "
        "WHERE platform='discord' AND platform_user_id='u1'"
    ).fetchone()
    assert row == ("claimed", 4242)


def test_missing_index_fails_closed(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    monkeypatch.setattr(nft_index, "index_db_path", lambda network: str(tmp_path / "nope.db"))
    assert free_mint.is_eligible("discord", "u1", "testnet") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_free_mint_eligibility.py -v`
Expected: FAIL — module `lfg_core.free_mint` missing.

- [ ] **Step 3: Implement `lfg_core/free_mint.py`**

```python
# lfg_core/free_mint.py
# One free mint per platform-identity (see
# docs/superpowers/specs/2026-07-13-free-mint-newcomers-design.md).
# Reserve -> confirm/release claim ledger + newcomer eligibility. Reads
# identity/wallet history and the on-chain ownership index via raw SQL so
# lfg_core never imports lfg_service.

import logging
import sqlite3

from lfg_core import nft_index
from lfg_core.user_db import DATABASE

_ACTIVE = ("reserved", "claimed")


def ensure_tables() -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS free_mint_claims (
                platform          TEXT NOT NULL,
                platform_user_id  TEXT NOT NULL,
                network           TEXT NOT NULL,
                wallet            TEXT NOT NULL,
                nft_number        INTEGER,
                status            TEXT NOT NULL,
                claimed_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, platform_user_id, network)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def wallets_for_identity(platform: str, platform_user_id: str) -> set[str]:
    conn = sqlite3.connect(DATABASE)
    try:
        wallets = {
            r[0]
            for r in conn.execute(
                "SELECT wallet FROM wallet_links "
                "WHERE platform = ? AND platform_user_id = ?",
                (platform, platform_user_id),
            )
        }
        row = conn.execute(
            "SELECT wallet FROM identities "
            "WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        ).fetchone()
        if row and row[0]:
            wallets.add(row[0])
        return wallets
    finally:
        conn.close()


def _has_active_claim(conn: sqlite3.Connection, platform: str, uid: str, network: str) -> bool:
    row = conn.execute(
        "SELECT status FROM free_mint_claims "
        "WHERE platform = ? AND platform_user_id = ? AND network = ?",
        (platform, uid, network),
    ).fetchone()
    return bool(row) and row[0] in _ACTIVE


def _owns_live_character(wallets: set[str], network: str) -> bool:
    if not wallets:
        return False
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        placeholders = ",".join("?" for _ in wallets)
        n = conn.execute(
            f"SELECT COUNT(*) FROM onchain_nfts "
            f"WHERE is_burned = 0 AND owner IN ({placeholders})",
            tuple(wallets),
        ).fetchone()[0]
        return n > 0
    finally:
        conn.close()


def is_eligible(platform: str, platform_user_id: str, network: str) -> bool:
    """Fail closed: any error (missing index, DB fault) -> not eligible."""
    try:
        conn = sqlite3.connect(DATABASE)
        try:
            if _has_active_claim(conn, platform, platform_user_id, network):
                return False
        finally:
            conn.close()
        wallets = wallets_for_identity(platform, platform_user_id)
        return not _owns_live_character(wallets, network)
    except Exception as e:
        logging.warning(f"free_mint.is_eligible fail-closed for {platform}/{platform_user_id}: {e}")
        return False


def reserve_claim(platform: str, platform_user_id: str, network: str, wallet: str) -> bool:
    """Atomically reserve the single claim row. True iff this call created it."""
    conn = sqlite3.connect(DATABASE)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO free_mint_claims "
            "(platform, platform_user_id, network, wallet, status) "
            "VALUES (?, ?, ?, ?, 'reserved')",
            (platform, platform_user_id, network, wallet),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def confirm_claim(
    platform: str, platform_user_id: str, network: str, wallet: str, nft_number: int
) -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            "UPDATE free_mint_claims SET status='claimed', wallet=?, nft_number=?, "
            "claimed_at=CURRENT_TIMESTAMP "
            "WHERE platform=? AND platform_user_id=? AND network=?",
            (wallet, nft_number, platform, platform_user_id, network),
        )
        conn.commit()
    finally:
        conn.close()


def release_claim(platform: str, platform_user_id: str, network: str) -> None:
    """Free a reserved claim so the identity can retry. Only releases a still-
    reserved row; a confirmed claim is permanent."""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            "DELETE FROM free_mint_claims "
            "WHERE platform=? AND platform_user_id=? AND network=? AND status='reserved'",
            (platform, platform_user_id, network),
        )
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_free_mint_eligibility.py -v`
Expected: PASS (all 8).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/free_mint.py tests/test_free_mint_eligibility.py
git commit -m "feat(free-mint): claim ledger + newcomer eligibility"
```

---

### Task 3: Free path in the mint flow

**Files:**
- Modify: `lfg_core/mint_flow.py` (`MintSession.__init__`, `to_dict`, `prepare_payment`, `run_mint_session`)
- Test: `tests/test_mint_flow_free.py` (create)

**Interfaces:**
- Consumes: `free_mint.is_eligible/reserve_claim/confirm_claim/release_claim` (Task 2).
- Produces: `MintSession.free: bool`, `MintSession.network: str`; `to_dict()["free"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mint_flow_free.py
import asyncio
import lfg_core.mint_flow as mint_flow
import lfg_core.free_mint as free_mint


def _session(monkeypatch, eligible, reserve=True):
    monkeypatch.setattr(free_mint, "is_eligible", lambda *a, **k: eligible)
    monkeypatch.setattr(free_mint, "reserve_claim", lambda *a, **k: reserve)
    s = mint_flow.MintSession(
        discord_id="u1", wallet_address="rA", platform="discord", network="testnet"
    )
    return s


def test_eligible_session_goes_free_and_skips_payment(monkeypatch):
    called = {"payload": False}

    async def _fake_payload(*a, **k):
        called["payload"] = True
        return None

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", _fake_payload)
    # LFGO balance check must not decide the path when free
    monkeypatch.setattr(
        mint_flow.xrpl_ops, "get_trustline_balance", lambda *a, **k: None
    )
    s = _session(monkeypatch, eligible=True)
    asyncio.run(s.prepare_payment())
    assert s.free is True
    assert s.to_dict()["free"] is True
    assert called["payload"] is False  # no XUMM payment payload built


def test_ineligible_session_uses_paid_path(monkeypatch):
    async def _bal(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_balance", _bal)

    async def _payload(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", _payload)
    s = _session(monkeypatch, eligible=False)
    asyncio.run(s.prepare_payment())
    assert s.free is False
    assert s.pay_with == "XRP"


def test_lost_reserve_race_falls_back_to_paid(monkeypatch):
    async def _bal(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_balance", _bal)

    async def _payload(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", _payload)
    s = _session(monkeypatch, eligible=True, reserve=False)  # lost the race
    asyncio.run(s.prepare_payment())
    assert s.free is False
    assert s.pay_with == "XRP"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mint_flow_free.py -v`
Expected: FAIL — `MintSession()` has no `network`/`free`.

- [ ] **Step 3: Implement**

Add `from lfg_core import ... free_mint` to the existing `from lfg_core import (...)` block in `mint_flow.py`.

In `MintSession.__init__`, add a `network` param (default from config) and `free` state. Change the signature to include `network: str | None = None` after `push_user_token`, and add:

```python
        self.network = network or config.XRPL_NETWORK
        self.free = False  # set by prepare_payment when the newcomer gate opens
```

At the **top** of `prepare_payment()`, before the trustline balance check, add the free-gate short-circuit:

```python
        # Newcomer free mint (#20x): eligible identity with no LFG NFT and no
        # prior claim mints free. Wallet control is already proven at connect,
        # so no payment payload is built. reserve_claim atomically wins the
        # single claim row; losing the race falls through to the paid path.
        if free_mint.is_eligible(self.platform, self.discord_id, self.network) and \
                free_mint.reserve_claim(
                    self.platform, self.discord_id, self.network, self.wallet_address
                ):
            self.free = True
            self.pay_with, self.pay_amount = "FREE", "0"
            return
```

Add `"free": self.free` to the dict returned by `to_dict()`.

In `run_mint_session()`, wrap the payment wait so a free session skips it. Replace the block from `session.ensure_payment_fallback()` through the `session.state = GENERATING` line (the initial payment-wait section, up to but not including the `if session.pay_with == "XRP":` line) with:

```python
        if session.free:
            # Free path: no payment to wait for; go straight to generation.
            session.state = GENERATING
        else:
            session.ensure_payment_fallback()
            p = session._payment_params()
            paid = await xrpl_ops.wait_for_payment(
                destination=p["destination"],
                expected_sender=session.wallet_address,
                expected_amount=p["value"],
                not_before=session.created_at - 10,
                currency=p["currency"],
            )
            if not paid:
                session.state = PAYMENT_TIMEOUT
                return
            session.state = GENERATING
```

Guard the buy-and-burn so it never runs free: change `if session.pay_with == "XRP":` to `if not session.free and session.pay_with == "XRP":`.

Pass the campaign tag on the mint call (line ~351): add `campaign="free-mint" if session.free else None` to the `xrpl_ops.mint_nft(...)` keyword args.

Reconcile the claim at the end. Wrap the existing `try:` body of `run_mint_session` so that after it finishes you settle the claim based on terminal state. Add a `finally` to the outermost try (the one whose `except Exception as e:` sets `FAILED`):

```python
    finally:
        if session.free:
            if session.state == OFFER_READY and session.nft_number is not None:
                await asyncio.to_thread(
                    free_mint.confirm_claim,
                    session.platform, session.discord_id, session.network,
                    session.wallet_address, session.nft_number,
                )
            else:
                await asyncio.to_thread(
                    free_mint.release_claim,
                    session.platform, session.discord_id, session.network,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mint_flow_free.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Full mint-flow regression**

Run: `.venv/bin/pytest tests/ -k "mint" -v`
Expected: PASS (no existing mint test regressed).

- [ ] **Step 6: Commit**

```bash
git add lfg_core/mint_flow.py tests/test_mint_flow_free.py
git commit -m "feat(mint): free newcomer path skips payment, records claim"
```

---

### Task 4: Boot wiring

Create the claim table at startup and make sure the service passes the identity/network the session needs (it already passes `discord_id`, `platform`; `network` defaults inside `MintSession`, so no service change to the constructor is required — this task only wires table creation).

**Files:**
- Modify: `lfg_service/app.py` (near `identity_store.ensure_identities_table()` at ~line 2774)
- Test: `tests/test_free_mint_boot.py` (create)

**Interfaces:**
- Consumes: `free_mint.ensure_tables` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_free_mint_boot.py
import sqlite3
import lfg_core.user_db as user_db
import lfg_core.free_mint as free_mint


def test_ensure_tables_creates_claims(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    free_mint.ensure_tables()
    cols = {
        r[1]
        for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(free_mint_claims)")
    }
    assert {"platform", "platform_user_id", "network", "wallet", "nft_number", "status"} <= cols
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `.venv/bin/pytest tests/test_free_mint_boot.py -v`
Expected: PASS already (ensure_tables exists from Task 2) — this test locks the contract. If it fails, fix Task 2.

- [ ] **Step 3: Wire boot**

In `lfg_service/app.py`, immediately after the `identity_store.ensure_identities_table()` call, add:

```python
    from lfg_core import free_mint
    free_mint.ensure_tables()
```

(Match the existing import style at that call site — if `free_mint` is already imported at module top, drop the local import.)

- [ ] **Step 4: Verify the app module imports cleanly**

Run: `.venv/bin/python -c "import lfg_service.app"`
Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_free_mint_boot.py
git commit -m "feat(free-mint): create claim table at service boot"
```

---

### Task 5: Admin CLI (`scripts/free_mint_admin.py`)

Loopback ops tool: list / revoke / grant. Seed of the Approach-2 credit tooling.

**Files:**
- Create: `scripts/free_mint_admin.py`
- Test: `tests/test_free_mint_admin.py` (create)

**Interfaces:**
- Consumes: `free_mint` claim helpers (Task 2).
- Produces: functions `list_claims(network)`, `revoke(platform, uid, network)`, `grant(platform, uid, network, wallet)` + an `argparse` CLI dispatching to them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_free_mint_admin.py
import lfg_core.user_db as user_db
import lfg_core.free_mint as free_mint
import scripts.free_mint_admin as admin


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    free_mint.ensure_tables()


def test_grant_then_revoke(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    admin.grant("discord", "u1", "testnet", "rA")
    assert free_mint.is_eligible("discord", "u1", "testnet") is False
    admin.revoke("discord", "u1", "testnet")
    # revoke clears any claim (reserved or claimed) so the identity can re-claim
    rows = admin.list_claims("testnet")
    assert all(r["platform_user_id"] != "u1" for r in rows)


def test_grant_is_claimed_status(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    admin.grant("discord", "u2", "testnet", "rB")
    rows = admin.list_claims("testnet")
    assert any(r["platform_user_id"] == "u2" and r["status"] == "claimed" for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_free_mint_admin.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `scripts/free_mint_admin.py`**

```python
#!/usr/bin/env python3
# Loopback ops CLI for the newcomer free-mint claim ledger. Not wired into any
# surface. See docs/superpowers/specs/2026-07-13-free-mint-newcomers-design.md.

import argparse
import sqlite3
from typing import Any

from lfg_core import free_mint
from lfg_core.user_db import DATABASE


def list_claims(network: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "SELECT platform, platform_user_id, network, wallet, nft_number, "
                "status, claimed_at FROM free_mint_claims WHERE network = ? "
                "ORDER BY claimed_at DESC",
                (network,),
            )
        ]
    finally:
        conn.close()


def revoke(platform: str, uid: str, network: str) -> None:
    """Delete any claim (reserved OR claimed) so the identity can claim again."""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            "DELETE FROM free_mint_claims "
            "WHERE platform=? AND platform_user_id=? AND network=?",
            (platform, uid, network),
        )
        conn.commit()
    finally:
        conn.close()


def grant(platform: str, uid: str, network: str, wallet: str) -> None:
    """Pre-authorize a claim, bypassing the eligibility scan. Idempotent."""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            "INSERT INTO free_mint_claims "
            "(platform, platform_user_id, network, wallet, status) "
            "VALUES (?, ?, ?, ?, 'claimed') "
            "ON CONFLICT(platform, platform_user_id, network) "
            "DO UPDATE SET status='claimed', wallet=excluded.wallet",
            (platform, uid, network, wallet),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    free_mint.ensure_tables()
    ap = argparse.ArgumentParser(description="Free-mint claim admin")
    ap.add_argument("--network", default="mainnet")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("platform")
    r.add_argument("uid")
    g = sub.add_parser("grant")
    g.add_argument("platform")
    g.add_argument("uid")
    g.add_argument("wallet")
    args = ap.parse_args()
    if args.cmd == "list":
        for row in list_claims(args.network):
            print(row)
    elif args.cmd == "revoke":
        revoke(args.platform, args.uid, args.network)
        print(f"revoked {args.platform}/{args.uid} on {args.network}")
    elif args.cmd == "grant":
        grant(args.platform, args.uid, args.network, args.wallet)
        print(f"granted {args.platform}/{args.uid} -> {args.wallet} on {args.network}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_free_mint_admin.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add scripts/free_mint_admin.py tests/test_free_mint_admin.py
git commit -m "feat(free-mint): loopback admin CLI (list/revoke/grant)"
```

---

### Task 6: Surface the free flag in the pay screen

Minimal client copy: when the mint status reports `free: true`, show a "Free mint 🎉" confirmation instead of the pay pill. The Activity client is vanilla JS (no build). Discord bot / Telegram render from the same session dict; if they show a pay amount, branch on `free`.

**Files:**
- Modify: `webapp/client/` mint view (grep for where `pay_amount`/`payment_link` is rendered)
- Modify: `surfaces/discord_bot/mint_view.py` and/or `surfaces/telegram_bot/` if they render a pay amount string
- Test: `webapp/` smoke test if one covers the mint pay screen; otherwise manual note

**Interfaces:**
- Consumes: `to_dict()["free"]` (Task 3).

- [ ] **Step 1: Locate render sites**

Run: `grep -rn "pay_amount\|payment_link\|pay_with" webapp/client surfaces/discord_bot surfaces/telegram_bot`
Read each hit; identify where the pay pill/amount is shown to the user.

- [ ] **Step 2: Branch on `free`**

At each render site, when the session dict has `free === true` (JS) / `session["free"]` (Python), render "Free mint 🎉 — no payment needed" and hide the pay pill / QR-to-pay. Keep all other states identical. Show the actual code diff you apply here (do not leave it abstract).

- [ ] **Step 3: Verify**

If a `webapp/` smoke test renders the pay screen, extend it to assert the free branch; run `.venv/bin/pytest webapp/ -v`. Otherwise, load the mock harness (`WEBAPP_DEV_MODE=1`) and confirm the free copy renders. Record what you did.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat(mint-ui): show free-mint confirmation when session.free"
```

---

### Task 7: Full-suite gate

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/pytest tests/ webapp/ -q`
Expected: all pass (mind the env-guard preamble convention — new test files importing `lfg_core` at module top may need the `BUNNY_PULL_ZONE`/`LAYER_SOURCE` guard; if a full-suite-order failure appears that passes in isolation, add the preamble rather than assuming a flake).

- [ ] **Step 2: Run the pre-push gate locally**

Run: `.venv/bin/pre-commit run --hook-stage pre-push --all-files` (or push to trigger it)
Expected: ruff, ruff-format, mypy, pytest all green.

- [ ] **Step 3: Final commit if the gate auto-fixed anything**

```bash
git add -A && git commit -m "chore: pre-push gate fixes" || true
```

## Self-Review notes

- **Spec coverage:** wallet_links (§1) → T1; free_mint_claims + is_eligible + lifecycle (§2/§3) → T2; mint free path + campaign memo + fail-closed (§4) → T3; boot (§5-ish) → T4; admin CLI (§6) → T5; surfaces (§5) → T6; testing (§Testing) spread across T1–T3, T7. Fail-closed on missing index → T2 test `test_missing_index_fails_closed`.
- **Type consistency:** claim helper signatures identical across T2 (definition), T3 (caller), T5 (admin). `network` param present everywhere. `MintSession(network=...)` used in T3 tests matches the `__init__` change.
- **No placeholders:** T6 is the only task with a locate-then-edit step because the render sites aren't known without grepping; its steps require showing the real diff, not abstract instructions.
