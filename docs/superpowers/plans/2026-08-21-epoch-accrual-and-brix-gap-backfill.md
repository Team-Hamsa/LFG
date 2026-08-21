# Epoch-accurate BRIX accrual + gap reimbursement (#411 opt 2 / #412) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the nightly drip's per-token live RPC sweep with an archive replay that knows owner-of-record and listed-state *as of each epoch's close*, then use that same replay to write the ~340-day (2025-09-15 → yesterday) historical accruals as ordinary claimable `brix_accruals` rows.

**Architecture:** `history_events` starts recording each sell offer's `offer_index` + `offer_flags` on `nft_events` (self-migrating columns, rebuilt by `derive_history_events.py`). A new pure module `lfg_core/epoch_state.py` replays `nft_events` in ledger order and yields, per `nft_id`, `(owner, listed, live)` at any epoch close, plus a certification gate over `archive_state`. `brix_drip.run_archive_accrual` drives `evaluate_accruals` from that state (zero RPCs, deferring uncertified epochs without advancing the cursor), and `scripts/backfill_brix_gap.py` walks a historical window through the same code with `--apply`, never touching the cursor.

**Tech Stack:** Python 3.10+, sqlite3, pytest, xrpl-py (only for the retained chain-identity check). No new dependencies.

**Specs:** `docs/superpowers/specs/2026-08-20-epoch-accurate-accrual-design.md` (#411) and `docs/superpowers/specs/2026-08-20-brix-gap-reimbursement-design.md` (#412). Both are on `origin/main` at 69fddd0.

## Global Constraints

- Base branch: `origin/main` (NOT the `~/LFG` `deploy` checkout, which is prod and has uncommitted hotfix files). Work in a worktree, branch `feat/411-412-epoch-accrual`.
- Pre-push gate runs ruff/ruff-format/mypy/gitleaks/pytest/validate-trait-config/check-repo-layout. Never `--no-verify`. The worktree needs `.venv` → symlink it: `ln -s /home/hamsa/LFG/.venv <worktree>/.venv` (otherwise the gate silently skips mypy/pytest).
- Run tests with the project venv: `/home/hamsa/LFG/.venv/bin/python -m pytest …`. The root `conftest.py` pins env; new test files need NO env preamble.
- **Fail-closed everywhere:** an unknown listed-state (`None`) pays nothing; an uncertified epoch writes nothing and never advances `brix_meta.last_accrued_epoch`; the backfill script NEVER writes `brix_meta`.
- Amounts stay INTEGER whole BRIX (`brix_drip.DRIP_AMOUNT == 1`).
- No Claude/AI attribution in commits or PR bodies (global CLAUDE.md).
- `nft_events` / `brix_events` are derived, droppable, rebuildable from `xrpl_txs` — populating new columns for history is a `derive_history_events.py` rerun, never a chain scrape.
- `_LSF_SELL = 1` (already defined in `lfg_core/history_events.py`): bit 0 of an NFTokenOffer's `Flags` = sell offer. Buy offers (bit clear) never count as listings.
- Epoch = UTC date `YYYY-MM-DD`; "close of epoch D" = `D+1 00:00:00Z` exclusive upper bound on event `ts` (unix seconds — `nft_events.ts` is already unix, NOT ripple epoch).

---

## File structure

| Path | Responsibility |
|---|---|
| `lfg_core/history_events.py` (modify) | derive `offer_index` + `offer_flags` on `offer_create`, `offer_cancel`, `sale`, `transfer` events |
| `lfg_core/history_store.py` (modify) | two new `nft_events` columns, self-migration, `_NFT_EV_COLS` |
| `lfg_core/epoch_state.py` (create) | `EpochToken`, `EpochReplay` (incremental replay), `state_at_epoch`, `epoch_close_ts`, `certify_epoch` |
| `lfg_core/brix_drip.py` (modify) | `TokenLike` protocol, `EpochReport.deferred`, `accrue_epoch`, `run_archive_accrual` (replaces `run_accrual`) |
| `lfg_core/brix_backfill.py` (create) | `plan_gap_backfill` — pure window walk + report aggregation shared by the script and its tests |
| `scripts/accrue_brix.py` (modify) | nightly CLI: archive-driven, no per-token sweep |
| `scripts/backfill_brix_gap.py` (create) | #412 CLI: dry-run report / `--apply` |
| `tests/test_history_events.py`, `tests/test_history_store.py` (modify) | new-column derivation + migration |
| `tests/test_epoch_state.py` (create) | replay rules + certification gate |
| `tests/test_brix_drip.py` (modify) | migrate `run_accrual` tests to `run_archive_accrual` |
| `tests/test_brix_backfill.py` (create) | #412 reconstruction fixtures, exploit regression, idempotence, cursor safety, claim integration |
| `CLAUDE.md`, `docs/ops/brix-gap-backfill.md` (create), both specs' Status lines | docs |

---

### Task 0: Worktree + branch

**Files:** none (setup)

- [ ] **Step 1: Create the worktree off origin/main**

```bash
git fetch -q origin
WT=/tmp/claude-1000/-home-hamsa-LFG/bcdc50ad-5085-42d3-9a01-beba84f922b2/scratchpad/wt-411
git worktree add -b feat/411-412-epoch-accrual "$WT" origin/main
ln -s /home/hamsa/LFG/.venv "$WT/.venv"
cd "$WT" && git log --oneline -1 && ls -la .venv
```
Expected: HEAD = `48613c9` (or newer origin/main), `.venv` symlink present.

- [ ] **Step 2: Baseline the suite subset you will touch**

Run: `cd "$WT" && .venv/bin/python -m pytest tests/test_history_events.py tests/test_history_store.py tests/test_brix_drip.py tests/test_derive_history_events.py -q`
Expected: all PASS (record the count).

---

### Task 1: Record `offer_index` + `offer_flags` on nft_events

**Files:**
- Modify: `lfg_core/history_events.py` (`_deleted_nft_offers` ~L64, `NFTokenAcceptOffer` branch ~L195-245, `NFTokenCreateOffer` ~L247, `NFTokenCancelOffer` ~L262)
- Modify: `lfg_core/history_store.py` (`_SCHEMA` nft_events DDL ~L32, `init_history_db` migration ~L153, `_NFT_EV_COLS` ~L595)
- Test: `tests/test_history_events.py`, `tests/test_history_store.py`

**Interfaces:**
- Produces: every `offer_create` event dict carries `offer_index: str|None` (the created `NFTokenOffer` ledger-object index) and `offer_flags: int` (the tx `Flags`, so `offer_flags & 1` = sell). Every `offer_cancel` event carries `offer_index` (deleted node `LedgerIndex`) and `offer_flags` (deleted node `FinalFields.Flags`). Every `sale`/`transfer` carries `offer_index` of the consumed **sell** offer (None when a buy offer was accepted with no sell side). `history_store.nft_events` has columns `offer_index TEXT`, `offer_flags INTEGER`.

- [ ] **Step 1: Write the failing derivation tests** (append to `tests/test_history_events.py`)

```python
def _created_offer(owner, amount, flags, index="AA" * 32, nft_id=None):
    return {
        "CreatedNode": {
            "LedgerEntryType": "NFTokenOffer",
            "LedgerIndex": index,
            "NewFields": {
                "Owner": owner, "Amount": amount, "Flags": flags,
                "NFTokenID": nft_id or fx.NFT_A,
            },
        }
    }


def test_offer_create_records_offer_index_and_flags():
    tx = {**fx.OFFER_CREATE, "meta": {
        "TransactionResult": "tesSUCCESS",
        "AffectedNodes": [_created_offer(fx.ALICE, "9000000", 1, index="CC" * 32)],
    }}
    (ev,) = _nft(tx)
    assert ev["event"] == "offer_create"
    assert ev["offer_index"] == "CC" * 32
    assert ev["offer_flags"] == 1


def test_offer_create_falls_back_to_meta_offer_id():
    # clio/rippled stamp the created offer's index as meta.offer_id too.
    tx = {**fx.OFFER_CREATE, "meta": {
        "TransactionResult": "tesSUCCESS", "AffectedNodes": [], "offer_id": "DD" * 32,
    }}
    (ev,) = _nft(tx)
    assert ev["offer_index"] == "DD" * 32


def test_offer_create_buy_offer_flags_zero():
    tx = {**fx.OFFER_CREATE, "Flags": 0, "Account": fx.BOB}
    (ev,) = _nft(tx)
    assert ev["offer_flags"] == 0


def test_offer_cancel_records_deleted_offer_index():
    node = fx._deleted_offer(fx.ALICE, "9000000", 1)
    node["DeletedNode"]["LedgerIndex"] = "EE" * 32
    tx = {**fx.OFFER_CANCEL, "meta": {"TransactionResult": "tesSUCCESS", "AffectedNodes": [node]}}
    (ev,) = _nft(tx)
    assert ev["event"] == "offer_cancel"
    assert ev["offer_index"] == "EE" * 32
    assert ev["offer_flags"] == 1


def test_sale_records_consumed_sell_offer_index():
    tx = json.loads(json.dumps(fx.SALE_XRP))
    for n in tx["meta"]["AffectedNodes"]:
        if "DeletedNode" in n and n["DeletedNode"]["LedgerEntryType"] == "NFTokenOffer":
            n["DeletedNode"]["LedgerIndex"] = "FF" * 32
    (ev,) = _nft(tx)
    assert ev["event"] == "sale"
    assert ev["offer_index"] == "FF" * 32


def test_brokered_sale_records_the_sell_side_index():
    tx = json.loads(json.dumps(fx.SALE_BROKERED))
    for n in tx["meta"]["AffectedNodes"]:
        d = n.get("DeletedNode") or {}
        if d.get("LedgerEntryType") == "NFTokenOffer":
            d["LedgerIndex"] = ("SELL" * 16) if int(d["FinalFields"]["Flags"]) & 1 else ("BUY0" * 16)
    (ev,) = _nft(tx)
    assert ev["offer_index"] == "SELL" * 16


def test_legacy_fixture_without_ledger_index_yields_none():
    (ev,) = _nft(fx.OFFER_CANCEL)   # fixture node has no LedgerIndex
    assert ev["offer_index"] is None and ev["offer_flags"] == 1
```
Add `import json` at the top of the test file if absent.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_history_events.py -q -k "offer_index or offer_flags or consumed_sell or sell_side or legacy_fixture"`
Expected: FAIL with `KeyError: 'offer_index'`.

- [ ] **Step 3: Implement in `lfg_core/history_events.py`**

Replace `_deleted_nft_offers`:
```python
def _deleted_nft_offers(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Deleted NFTokenOffer nodes as their FinalFields, plus the ledger-object
    index under the synthetic key ``LedgerIndex`` (never a FinalFields key)
    so callers can name WHICH offer closed — the epoch replay (#411) matches
    an ``offer_cancel``/accept to the ``offer_create`` that opened it."""
    out = []
    for node in meta.get("AffectedNodes", []):
        wrapper = node.get("DeletedNode") or {}
        if wrapper.get("LedgerEntryType") == "NFTokenOffer":
            out.append({**(wrapper.get("FinalFields") or {}), "LedgerIndex": wrapper.get("LedgerIndex")})
    return out


def _created_nft_offer_index(meta: dict[str, Any]) -> str | None:
    """Index of the NFTokenOffer an NFTokenCreateOffer created. Prefers the
    CreatedNode, falls back to clio's ``meta.offer_id`` convenience field."""
    for node in meta.get("AffectedNodes", []):
        wrapper = node.get("CreatedNode") or {}
        if wrapper.get("LedgerEntryType") == "NFTokenOffer" and wrapper.get("LedgerIndex"):
            return str(wrapper["LedgerIndex"])
    offer_id = meta.get("offer_id")
    return str(offer_id) if offer_id else None
```
In `derive_nft_events` add to `base`: `"offer_index": None, "offer_flags": None,`.
In the `NFTokenAcceptOffer` branch, after `chosen_offer = ...`, set on `out`: `"offer_index": sell.get("LedgerIndex") if sell is not None else None,`.
In the `NFTokenCreateOffer` branch add: `"offer_index": _created_nft_offer_index(meta), "offer_flags": int(tx.get("Flags") or 0),`.
In the `NFTokenCancelOffer` branch add per offer: `"offer_index": o.get("LedgerIndex"), "offer_flags": int(o.get("Flags") or 0),`.

- [ ] **Step 4: Write the failing store tests** (append to `tests/test_history_store.py`; look at the file's existing fixture for the `conn` pattern — it opens `history_store.init_history_db(tmp_path / ...)`)

```python
def test_nft_events_has_offer_columns(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nft_events)")}
    assert {"offer_index", "offer_flags"} <= cols


def test_pre_existing_db_self_migrates_offer_columns(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE nft_events (tx_hash TEXT, nft_id TEXT, nft_number INTEGER, event TEXT,"
        " from_addr TEXT, to_addr TEXT, price_drops INTEGER, price_token TEXT,"
        " ledger_index INTEGER, ts INTEGER, PRIMARY KEY (tx_hash, nft_id))"
    )
    raw.commit(); raw.close()
    conn = history_store.init_history_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nft_events)")}
    assert {"memo_action", "offer_index", "offer_flags"} <= cols


def test_insert_nft_event_persists_offer_columns(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.insert_nft_event(conn, {
        "tx_hash": "T1", "nft_id": "N1", "event": "offer_create", "from_addr": "rA",
        "ledger_index": 5, "ts": 100, "offer_index": "OI", "offer_flags": 1,
    })
    row = conn.execute("SELECT offer_index, offer_flags FROM nft_events").fetchone()
    assert tuple(row) == ("OI", 1)
```

- [ ] **Step 5: Implement in `lfg_core/history_store.py`**

DDL: after `memo_action  TEXT, ...` add
```sql
    offer_index  TEXT,    -- NFTokenOffer ledger-object index (#411); NULL pre-schema
    offer_flags  INTEGER, -- NFTokenOffer Flags (bit 0 = sell) (#411); NULL pre-schema
```
Migration in `init_history_db`, right after the `memo_action` ALTER:
```python
    for column, declaration in (("offer_index", "TEXT"), ("offer_flags", "INTEGER")):
        if column not in cols:
            conn.execute(f"ALTER TABLE nft_events ADD COLUMN {column} {declaration}")
```
`_NFT_EV_COLS`: append `"offer_index", "offer_flags"`.

- [ ] **Step 6: Run the three touched test files + derive tests**

Run: `.venv/bin/python -m pytest tests/test_history_events.py tests/test_history_store.py tests/test_derive_history_events.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lfg_core/history_events.py lfg_core/history_store.py tests/test_history_events.py tests/test_history_store.py
git commit -m "feat(history): record offer_index + offer_flags on nft_events (#411)"
```

---

### Task 2: `lfg_core/epoch_state.py` — replay owner/listed/live at an epoch close

**Files:**
- Create: `lfg_core/epoch_state.py`
- Test: `tests/test_epoch_state.py`

**Interfaces:**
- Consumes: `nft_events` rows with the Task 1 columns.
- Produces:
```python
@dataclass(frozen=True)
class EpochToken:
    nft_id: str
    owner: str | None      # owner-of-record at epoch close
    listed: bool | None    # True/False, or None = unknown (fail-closed)
    live: bool             # minted (seen) and not burned at close
    @property
    def is_burned(self) -> bool: return not self.live

def epoch_close_ts(epoch: str) -> int          # unix ts of (epoch + 1 day) 00:00:00Z — EXCLUSIVE bound
class EpochReplay:
    def __init__(self, hconn: sqlite3.Connection) -> None
    def advance_to(self, epoch: str) -> dict[str, EpochToken]   # epochs must be non-decreasing
def state_at_epoch(hconn: sqlite3.Connection, epoch: str) -> dict[str, EpochToken]
```

Rules (from the spec, §2):
- owner follows `mint` → (`to_addr`), `transfer`/`sale` → (`to_addr`); `burn` → `live=False`. A token whose first event isn't a mint is still known from that event (archive may predate a 2023 token's mint row) — owner from that event's `to_addr` if present.
- `offer_create` with `offer_flags & 1` opens a sell offer keyed by `offer_index` with `owner = from_addr`. **If `offer_index` is NULL (row predates Task 1 and the archive hasn't been re-derived) the token's `listed` becomes `None` permanently** — that's the fail-closed "rederive first" signal. Buy offers (`offer_flags & 1 == 0`) are ignored; an `offer_create` with `offer_flags` NULL (legacy row) also yields `listed=None` (we cannot tell a bid from a listing).
- `offer_cancel` / `sale` / `transfer` with `offer_index` close that offer. A cancel/sale with NULL `offer_index` for a token with open offers → `listed=None` (unknown which closed). With no open offers it's a no-op.
- `listed` at close = any open sell offer on the token whose `owner == current owner` (an offer left by a previous holder is unfillable; destination-locked offers DO count — `to_addr` is not inspected).
- Order: `ORDER BY ledger_index, ts, rowid`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_epoch_state.py`

```python
"""Epoch-state replay from the history archive (#411 option 2).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import sqlite3

import pytest

from lfg_core import epoch_state, history_store

ISSUER, ALICE, BOB, BROKER = "rIssuer", "rAlice", "rBob", "rBroker"
NFT = "N1"
D = {  # unix ts for 00:00Z of a few consecutive UTC days
    "2026-01-01": 1767225600, "2026-01-02": 1767312000,
    "2026-01-03": 1767398400, "2026-01-04": 1767484800,
}


@pytest.fixture()
def hconn(tmp_path):
    c = history_store.init_history_db(str(tmp_path / "h.db"))
    yield c
    c.close()


_seq = {"n": 0}


def ev(conn, event, *, ts, nft_id=NFT, from_addr=None, to_addr=None,
       offer_index=None, offer_flags=None, ledger_index=None):
    _seq["n"] += 1
    history_store.insert_nft_event(conn, {
        "tx_hash": f"T{_seq['n']}", "nft_id": nft_id, "event": event,
        "from_addr": from_addr, "to_addr": to_addr, "ts": ts,
        "ledger_index": ledger_index if ledger_index is not None else _seq["n"],
        "offer_index": offer_index, "offer_flags": offer_flags,
    })
    conn.commit()


def test_epoch_close_ts_is_next_midnight_exclusive():
    assert epoch_state.epoch_close_ts("2026-01-01") == D["2026-01-02"]


def test_mint_then_hold_is_live_unlisted_owned_by_minter(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 10, to_addr=ALICE)
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert (tok.owner, tok.listed, tok.live) == (ALICE, False, True)


def test_event_after_epoch_close_is_invisible(hconn):
    ev(hconn, "mint", ts=D["2026-01-02"], to_addr=ALICE)   # exactly at the bound → next epoch
    assert NFT not in epoch_state.state_at_epoch(hconn, "2026-01-01")
    assert NFT in epoch_state.state_at_epoch(hconn, "2026-01-02")


def test_transfer_credit_follows_holder_at_close(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "transfer", ts=D["2026-01-02"] + 5, from_addr=ALICE, to_addr=BOB)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].owner == ALICE
    assert epoch_state.state_at_epoch(hconn, "2026-01-02")[NFT].owner == BOB


def test_sale_moves_ownership(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "sale", ts=D["2026-01-01"] + 2, from_addr=ALICE, to_addr=BOB)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].owner == BOB


def test_burn_ends_life(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "burn", ts=D["2026-01-02"] + 1, from_addr=ALICE)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].live is True
    tok = epoch_state.state_at_epoch(hconn, "2026-01-02")[NFT]
    assert tok.live is False and tok.is_burned is True


def test_sell_offer_open_at_close_is_listed(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, offer_index="O1", offer_flags=1)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is True


def test_offer_opened_and_cancelled_inside_epoch_is_unlisted(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, offer_index="O1", offer_flags=1)
    ev(hconn, "offer_cancel", ts=D["2026-01-01"] + 3, from_addr=ALICE, offer_index="O1", offer_flags=1)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False


def test_offer_left_by_previous_owner_does_not_count(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, offer_index="O1", offer_flags=1)
    # Bob acquires via an accepted BUY offer (sell offer O1 survives on-ledger, unfillable)
    ev(hconn, "sale", ts=D["2026-01-01"] + 3, from_addr=ALICE, to_addr=BOB, offer_index=None)
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert tok.owner == BOB and tok.listed is False


def test_sale_through_sell_offer_closes_it(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, offer_index="O1", offer_flags=1)
    ev(hconn, "sale", ts=D["2026-01-01"] + 3, from_addr=ALICE, to_addr=BOB, offer_index="O1")
    ev(hconn, "transfer", ts=D["2026-01-01"] + 4, from_addr=BOB, to_addr=ALICE)  # back to Alice
    # O1 was consumed — Alice holding again must NOT look listed
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False


def test_destination_locked_sell_offer_counts(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, to_addr=BROKER,
       offer_index="O1", offer_flags=1)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is True


def test_buy_offer_never_counts(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=BOB, offer_index="B1", offer_flags=0)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False


def test_legacy_offer_create_without_index_is_unknown(hconn):
    """A pre-#411 row can't be matched to its cancel → fail closed (None)."""
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, offer_index=None, offer_flags=None)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is None


def test_cancel_without_index_while_offers_open_is_unknown(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, offer_index="O1", offer_flags=1)
    ev(hconn, "offer_cancel", ts=D["2026-01-01"] + 3, from_addr=ALICE, offer_index=None, offer_flags=1)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is None


def test_replay_is_incremental_and_matches_fresh_state(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "offer_create", ts=D["2026-01-02"] + 1, from_addr=ALICE, offer_index="O1", offer_flags=1)
    ev(hconn, "transfer", ts=D["2026-01-03"] + 1, from_addr=ALICE, to_addr=BOB)
    r = epoch_state.EpochReplay(hconn)
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        assert r.advance_to(d) == epoch_state.state_at_epoch(hconn, d)
    with pytest.raises(ValueError):
        r.advance_to("2026-01-02")   # cannot go backwards


def test_replay_orders_by_ledger_then_ts_not_insertion(hconn):
    # inserted out of order: cancel first, then create, with ledger_index saying create came first
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE, ledger_index=1)
    ev(hconn, "offer_cancel", ts=D["2026-01-01"] + 3, from_addr=ALICE, offer_index="O1", offer_flags=1, ledger_index=3)
    ev(hconn, "offer_create", ts=D["2026-01-01"] + 2, from_addr=ALICE, offer_index="O1", offer_flags=1, ledger_index=2)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_epoch_state.py -q`
Expected: FAIL — `ImportError: cannot import name 'epoch_state'`.

- [ ] **Step 3: Create `lfg_core/epoch_state.py`**

```python
"""Owner-of-record and listed-state at an epoch close, replayed from the
history archive (#411 option 2; shared foundation for #412).

Pure over `history_<net>.db`: no network, no XRPL client, no clock. The
nightly drip and the gap backfill both ask the same question — "who held
this token, unlisted, when UTC day D closed?" — and `nft_events` already
records everything needed to answer it back to 2023.

Rules mirror what the live path (`brix_drip.classify_sell_offers`) decides:
a sell offer counts as a listing only while its CREATOR is still the current
holder; destination-locked sell offers count (brokered marketplaces list that
way); buy offers never count.

Fail-closed by construction: any event the replay cannot interpret (a legacy
row with no `offer_index`/`offer_flags`, a cancel that cannot be matched while
offers are open) makes that token's `listed` **None**, which
`brix_drip.evaluate_accruals` never pays. Re-deriving the archive
(`scripts/derive_history_events.py`) populates the columns and clears it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from lfg_core import history_store

_LSF_SELL = 1


@dataclass(frozen=True)
class EpochToken:
    """One token's state as of an epoch close."""

    nft_id: str
    owner: str | None
    listed: bool | None
    live: bool

    @property
    def is_burned(self) -> bool:
        return not self.live


def epoch_close_ts(epoch: str) -> int:
    """Unix ts of the instant epoch `epoch` closes — the NEXT day's 00:00:00Z.

    Exclusive upper bound: an event AT this ts belongs to the next epoch."""
    day = datetime.strptime(epoch, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int((day + timedelta(days=1)).timestamp())


@dataclass
class _TokenState:
    owner: str | None = None
    live: bool = False
    unknown: bool = False
    # offer_index -> creator, for OPEN sell offers on this token
    sell_offers: dict[str, str] | None = None

    def offers(self) -> dict[str, str]:
        if self.sell_offers is None:
            self.sell_offers = {}
        return self.sell_offers

    def snapshot(self, nft_id: str) -> EpochToken:
        if self.unknown:
            listed: bool | None = None
        else:
            listed = any(creator == self.owner for creator in self.offers().values())
        return EpochToken(nft_id=nft_id, owner=self.owner, listed=listed, live=self.live)


class EpochReplay:
    """Incremental replay: call `advance_to(epoch)` with non-decreasing epochs
    and it consumes only the events since the previous call. One pass over the
    archive serves a whole window (the #412 backfill walks ~340 epochs)."""

    def __init__(self, hconn: sqlite3.Connection) -> None:
        self._conn = hconn
        self._tokens: dict[str, _TokenState] = {}
        self._consumed_until: int | None = None  # exclusive ts bound already applied

    def advance_to(self, epoch: str) -> dict[str, EpochToken]:
        bound = epoch_close_ts(epoch)
        if self._consumed_until is not None and bound < self._consumed_until:
            raise ValueError(f"EpochReplay cannot rewind: {epoch} is before the last epoch replayed")
        lower = self._consumed_until
        rows = self._conn.execute(
            "SELECT nft_id, event, from_addr, to_addr, offer_index, offer_flags"
            " FROM nft_events WHERE ts < ?" + (" AND ts >= ?" if lower is not None else "") +
            " ORDER BY ledger_index, ts, rowid",
            (bound,) if lower is None else (bound, lower),
        )
        for row in rows:
            self._apply(row)
        self._consumed_until = bound
        return {nid: st.snapshot(nid) for nid, st in self._tokens.items()}

    def _apply(self, row: Any) -> None:
        nft_id = row["nft_id"]
        if not nft_id:
            return
        st = self._tokens.setdefault(nft_id, _TokenState())
        event = row["event"]
        if event == "mint":
            st.live = True
            st.owner = row["to_addr"]
            return
        if event in ("transfer", "sale"):
            st.live = True
            if row["to_addr"]:
                st.owner = row["to_addr"]
            self._close_offer(st, row["offer_index"])
            return
        if event == "burn":
            st.live = False
            return
        if event == "offer_create":
            flags = row["offer_flags"]
            if flags is None:
                st.unknown = True      # legacy row: bid or listing? cannot tell
                return
            if not (int(flags) & _LSF_SELL):
                return                 # buy offer: never a listing
            if not row["offer_index"]:
                st.unknown = True      # legacy row: can never be matched to its close
                return
            if not st.live:
                st.live = True         # archive predates this token's mint row
            st.offers()[str(row["offer_index"])] = row["from_addr"]
            return
        if event == "offer_cancel":
            self._close_offer(st, row["offer_index"])
            return
        # modify and anything else: no ownership/listing effect

    @staticmethod
    def _close_offer(st: _TokenState, offer_index: Any) -> None:
        if offer_index:
            st.offers().pop(str(offer_index), None)
        elif st.offers():
            st.unknown = True          # something closed, unknown which


def state_at_epoch(hconn: sqlite3.Connection, epoch: str) -> dict[str, EpochToken]:
    """Per-`nft_id` owner / listed / live as of the close of `epoch`."""
    return EpochReplay(hconn).advance_to(epoch)
```
(The `history_store` import is used by Task 3's certification gate; if ruff flags it unused at this step, leave it out until Task 3.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_epoch_state.py -q`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/epoch_state.py tests/test_epoch_state.py
git commit -m "feat(brix): epoch_state — replay owner/listed/live at an epoch close from the archive (#411)"
```

---

### Task 3: Certification gate `epoch_state.certify_epoch`

**Files:**
- Modify: `lfg_core/epoch_state.py`
- Test: `tests/test_epoch_state.py`

**Interfaces:**
- Produces: `certify_epoch(hconn, network: str, epoch: str) -> str | None` — `None` = payable; otherwise a short human reason (`"no archive_state row"`, `"baseline not complete"`, `"continuity gap recorded (<reason>)"`, `"archive validated through <iso> — epoch <epoch> not yet closed in archive"`).

Rules (spec §3): payable iff an `archive_state` row for `network` exists AND `baseline_complete == 1` AND all four `continuity_gap_*` columns are NULL AND `validated_close_time is not None and validated_close_time >= epoch_close_ts(epoch)`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_epoch_state.py`)

```python
def _archive_row(conn, *, complete=1, validated_close=None, gap_after=None, gap_reason=None):
    conn.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " continuity_gap_after, continuity_gap_reason, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("testnet", "G" * 64, complete, validated_close, gap_after, gap_reason, 1),
    )
    conn.commit()


def test_certify_no_archive_row_defers(hconn):
    assert epoch_state.certify_epoch(hconn, "testnet", "2026-01-01") is not None


def test_certify_payable_when_complete_and_validated_past_close(hconn):
    _archive_row(hconn, validated_close=D["2026-01-02"])
    assert epoch_state.certify_epoch(hconn, "testnet", "2026-01-01") is None


def test_certify_defers_when_archive_has_not_seen_epoch_close(hconn):
    _archive_row(hconn, validated_close=D["2026-01-02"] - 1)
    reason = epoch_state.certify_epoch(hconn, "testnet", "2026-01-01")
    assert reason and "not yet closed" in reason


def test_certify_defers_on_incomplete_baseline(hconn):
    _archive_row(hconn, complete=0, validated_close=D["2026-01-04"])
    assert "baseline" in (epoch_state.certify_epoch(hconn, "testnet", "2026-01-01") or "")


def test_certify_defers_on_continuity_gap(hconn):
    _archive_row(hconn, validated_close=D["2026-01-04"], gap_after=123, gap_reason="listener restart")
    reason = epoch_state.certify_epoch(hconn, "testnet", "2026-01-01")
    assert reason and "listener restart" in reason


def test_certify_is_per_network(hconn):
    _archive_row(hconn, validated_close=D["2026-01-04"])
    assert epoch_state.certify_epoch(hconn, "mainnet", "2026-01-01") is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_epoch_state.py -q -k certify`
Expected: FAIL — `AttributeError: module 'lfg_core.epoch_state' has no attribute 'certify_epoch'`.

- [ ] **Step 3: Implement** (append to `lfg_core/epoch_state.py`)

```python
def certify_epoch(hconn: sqlite3.Connection, network: str, epoch: str) -> str | None:
    """Why `epoch` is NOT payable from this archive, or None when it is.

    The replay is only as good as the archive's continuity, so an epoch pays
    only when the same provenance sponsored-mint eligibility fails closed on
    holds: certified baseline, no recorded continuity gap, and the listener has
    validated past the epoch's close (it has SEEN the whole epoch). Anything
    else is deferred — nothing written, cursor left behind — and the next run
    completes it once the listener's auto catch-up (#402) heals the archive.
    """
    state = history_store.get_archive_state(hconn, network)
    if state is None:
        return "no archive_state row"
    if not state.baseline_complete:
        return "baseline not complete"
    if (
        state.continuity_gap_at is not None
        or state.continuity_gap_after is not None
        or state.continuity_gap_before is not None
        or state.continuity_gap_reason is not None
    ):
        return f"continuity gap recorded ({state.continuity_gap_reason or 'unbounded'})"
    close = epoch_close_ts(epoch)
    if state.validated_close_time is None or state.validated_close_time < close:
        seen = (
            datetime.fromtimestamp(state.validated_close_time, tz=timezone.utc).isoformat()
            if state.validated_close_time is not None
            else "never"
        )
        return f"archive validated through {seen} — epoch {epoch} not yet closed in archive"
    return None
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_epoch_state.py -q`
Expected: PASS (22).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/epoch_state.py tests/test_epoch_state.py
git commit -m "feat(brix): certification gate — an epoch pays only from a certified, gap-free, fully-seen archive (#411)"
```

---

### Task 4: `brix_drip` — archive-driven accrual, deferral, cursor safety

**Files:**
- Modify: `lfg_core/brix_drip.py` (`evaluate_accruals` ~L180, `EpochReport` ~L261, `run_accrual` ~L301)
- Test: `tests/test_brix_drip.py` (`run_accrual` tests ~L249-303)

**Interfaces:**
- Consumes: `epoch_state.EpochReplay`, `epoch_state.certify_epoch`, `epoch_state.EpochToken`.
- Produces:
```python
class TokenLike(Protocol):         # what evaluate_accruals needs; OnchainNft and EpochToken both satisfy it
    nft_id: str
    owner: str | None
    is_burned: bool

@dataclass(frozen=True)
class EpochReport:                 # NEW field
    ...
    deferred: str | None = None    # certification reason; when set, accrued == 0 and cursor did NOT move

def accrue_epoch(conn, epoch: str, tokens: Mapping[str, EpochToken], system_accounts: frozenset[str]) -> EpochReport
    # evaluate + INSERT OR IGNORE; does NOT touch the cursor

def run_archive_accrual(conn, network: str, system_accounts: frozenset[str], today: str | None = None,
                        *, certify=epoch_state.certify_epoch, replay_factory=epoch_state.EpochReplay) -> list[EpochReport]
    # per owed epoch: certify → (deferred: append report, STOP — later epochs can't be certified either) |
    #               replay.advance_to → accrue_epoch → set_meta(LAST_ACCRUED_EPOCH)
```
`run_accrual` (live-state engine) is **removed**; `evaluate_accruals` keeps its signature but is typed on `Sequence[TokenLike]`. `fetch_sell_offer_state` / `classify_sell_offers` stay (live-verification helpers, spec §4).

- [ ] **Step 1: Rewrite the `run_accrual` tests** — in `tests/test_brix_drip.py` replace the three tests `test_run_accrual_advances_cursor_and_is_a_no_op_on_rerun`, `test_run_accrual_reports_skip_reasons_per_epoch`, `test_run_accrual_catch_up_writes_every_missed_epoch` with:

```python
from lfg_core.epoch_state import EpochToken


class _FakeReplay:
    """Stands in for epoch_state.EpochReplay: a fixed state for every epoch."""

    def __init__(self, tokens):
        self._tokens = tokens

    def advance_to(self, epoch):
        return self._tokens


def _tok(nft_id, owner="rHolder", listed=False, live=True):
    return EpochToken(nft_id=nft_id, owner=owner, listed=listed, live=live)


def _run(conn, tokens, *, today, certify=lambda c, n, e: None):
    return brix_drip.run_archive_accrual(
        conn, "testnet", frozenset(), today=today,
        certify=certify, replay_factory=lambda c: _FakeReplay(tokens),
    )


def test_run_archive_accrual_advances_cursor_and_is_a_no_op_on_rerun(conn):
    tokens = {"A": _tok("A", "rAlice"), "B": _tok("B", "rBob")}
    reports = _run(conn, tokens, today="2026-08-19")
    assert [r.epoch for r in reports] == ["2026-08-18"]
    assert reports[0].accrued == 2 and reports[0].deferred is None
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-18"
    assert _run(conn, tokens, today="2026-08-19") == []
    assert conn.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 2


def test_run_archive_accrual_reports_skip_reasons(conn):
    tokens = {
        "A": _tok("A", "rAlice"),
        "B": _tok("B", "rBob", listed=True),
        "C": _tok("C", "rCarol", listed=None),
        "D": _tok("D", "rDave", live=False),
        "E": _tok("E", "rSys"),
    }
    reports = brix_drip.run_archive_accrual(
        conn, "testnet", frozenset({"rSys"}), today="2026-08-19",
        certify=lambda c, n, e: None, replay_factory=lambda c: _FakeReplay(tokens),
    )
    r = reports[0]
    assert (r.accrued, r.skipped_listed, r.unknown, r.skipped_burned, r.skipped_system) == (1, 1, 1, 1, 1)


def test_run_archive_accrual_catch_up_writes_every_missed_epoch(conn):
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, "2026-08-15")
    reports = _run(conn, {"A": _tok("A", "rAlice")}, today="2026-08-19")
    assert [r.epoch for r in reports] == ["2026-08-16", "2026-08-17", "2026-08-18"]
    assert brix_drip.claimable(conn, "rAlice") == 3
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-18"


def test_uncertified_epoch_defers_and_leaves_cursor_behind(conn):
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, "2026-08-15")
    gated = {"2026-08-17"}
    certify = lambda c, n, e: ("gap" if e in gated else None)  # noqa: E731
    reports = _run(conn, {"A": _tok("A", "rAlice")}, today="2026-08-19", certify=certify)
    assert [(r.epoch, r.deferred) for r in reports] == [("2026-08-16", None), ("2026-08-17", "gap")]
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-16"
    assert brix_drip.claimable(conn, "rAlice") == 1
    # gap healed → next run completes 17 and 18 exactly once
    again = _run(conn, {"A": _tok("A", "rAlice")}, today="2026-08-19")
    assert [r.epoch for r in again] == ["2026-08-17", "2026-08-18"]
    assert brix_drip.claimable(conn, "rAlice") == 3
    assert conn.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 3


def test_listed_token_earns_nothing_from_archive_fixtures(conn, tmp_path):
    """Regression driven by the real replay, not a mocked RPC (spec §Testing)."""
    from lfg_core import epoch_state, history_store

    h = history_store.init_history_db(str(tmp_path / "h.db"))
    brix_drip.ensure_schema(h)
    h.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " updated_at) VALUES ('testnet', ?, 1, ?, 1)", ("G" * 64, 4102444800),
    )
    for i, (ev, kw) in enumerate([
        ("mint", {"nft_id": "L", "to_addr": "rAlice"}),
        ("offer_create", {"nft_id": "L", "from_addr": "rAlice", "offer_index": "O1", "offer_flags": 1}),
        ("mint", {"nft_id": "U", "to_addr": "rBob"}),
    ]):
        history_store.insert_nft_event(h, {"tx_hash": f"T{i}", "event": ev, "ts": 1767225600 + i,
                                           "ledger_index": i, **kw})
    h.commit()
    reports = brix_drip.run_archive_accrual(h, "testnet", frozenset(), today="2026-01-03")
    assert reports[-1].epoch == "2026-01-02"
    assert brix_drip.claimable(h, "rAlice") == 0
    assert brix_drip.claimable(h, "rBob") == 2   # 01-01 and 01-02
    assert epoch_state.state_at_epoch(h, "2026-01-02")["L"].listed is True
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_brix_drip.py -q -k "archive_accrual or uncertified or archive_fixtures"`
Expected: FAIL — `AttributeError: ... 'run_archive_accrual'`.

- [ ] **Step 3: Implement in `lfg_core/brix_drip.py`**

Imports: add `from typing import Mapping, Protocol` (merge with the existing typing import) and `from lfg_core import epoch_state` (check for an import cycle: `epoch_state` imports only `history_store`; fine).

Add above `evaluate_accruals`:
```python
class TokenLike(Protocol):
    """What the evaluator needs from a token — `nft_index.OnchainNft` (live
    index) and `epoch_state.EpochToken` (archive replay) both satisfy it."""

    @property
    def nft_id(self) -> str: ...
    @property
    def owner(self) -> str | None: ...
    @property
    def is_burned(self) -> bool: ...
```
Change `evaluate_accruals(live_tokens: Sequence[OnchainNft], ...)` → `Sequence[TokenLike]` (keep the `OnchainNft` import only if still referenced; otherwise drop it).

`EpochReport`: add `deferred: str | None = None` as the last field, with the docstring line "deferred = certification reason; nothing written and the cursor stayed put".

Replace `run_accrual` with:
```python
def accrue_epoch(
    conn: sqlite3.Connection,
    epoch: str,
    tokens: Mapping[str, epoch_state.EpochToken],
    system_accounts: frozenset[str],
) -> EpochReport:
    """Evaluate + write one epoch from replayed state. Cursor untouched —
    the nightly runner advances it, the #412 backfill never does."""
    result = evaluate_accruals(
        list(tokens.values()),
        listed_fn=lambda nft_id: tokens[nft_id].listed,
        system_accounts=system_accounts,
        epoch=epoch,
    )
    inserted = record_accruals(conn, result.rows)
    return EpochReport(
        epoch=epoch,
        accrued=inserted,
        skipped_listed=result.skipped_listed,
        skipped_burned=result.skipped_burned,
        skipped_system=result.skipped_system,
        skipped_ownerless=result.skipped_ownerless,
        unknown=result.unknown,
    )


def run_archive_accrual(
    conn: sqlite3.Connection,
    network: str,
    system_accounts: frozenset[str],
    today: str | None = None,
    *,
    certify: Callable[[sqlite3.Connection, str, str], str | None] = epoch_state.certify_epoch,
    replay_factory: Callable[[sqlite3.Connection], Any] = epoch_state.EpochReplay,
) -> list[EpochReport]:
    """Accrue every epoch still owed from the archive, advancing the cursor as
    each one lands (#411 option 2).

    Zero RPCs: owner-of-record and listed-state come from `epoch_state`, as of
    each epoch's close — so a catch-up epoch is evaluated against the state it
    HAD, not the state things are in now. An epoch the archive cannot certify
    is deferred: nothing written, cursor left behind it, and the walk stops
    (a later epoch cannot be certified while an earlier one is not). The
    accruals PK makes the eventual completion safe by construction.
    """
    today = today or utc_today()
    reports: list[EpochReport] = []
    replay = replay_factory(conn)
    for epoch in epochs_to_accrue(get_meta(conn, LAST_ACCRUED_EPOCH), today):
        reason = certify(conn, network, epoch)
        if reason is not None:
            reports.append(EpochReport(epoch, 0, 0, 0, 0, 0, 0, deferred=reason))
            break
        report = accrue_epoch(conn, epoch, replay.advance_to(epoch), system_accounts)
        set_meta(conn, LAST_ACCRUED_EPOCH, epoch)
        reports.append(report)
    return reports
```
Update the module docstring's mention of the live sweep if present (grep `run_accrual` in the file — no other references should remain).

- [ ] **Step 4: Grep for stale callers**

Run: `grep -rn "run_accrual\b" --include=*.py . | grep -v "\.venv"`
Expected: only `scripts/accrue_brix.py` (fixed in Task 5). If `tests/test_brix_endpoints.py` or others reference it, migrate them the same way.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_brix_drip.py tests/test_epoch_state.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lfg_core/brix_drip.py tests/test_brix_drip.py
git commit -m "feat(brix): run_archive_accrual — archive-driven nightly accrual with certification deferral (#411)"
```

---

### Task 5: `scripts/accrue_brix.py` — drop the per-token sweep

**Files:**
- Modify: `scripts/accrue_brix.py`
- Test: `tests/test_accrue_brix_cli.py` (create)

**Interfaces:**
- Consumes: `brix_drip.run_archive_accrual`, `brix_drip.verify_endpoint_chain`, `system_accounts()` (unchanged; PR #417 may swap its body — keep the function name).
- Produces: same CLI (`--network`, `--date`); exit 2 on network/chain mismatch; exit 0 otherwise; prints a `DEFERRED` line with the reason when an epoch is deferred; prints a `WARNING … run scripts/derive_history_events.py` hint when `unknown > 0`.

- [ ] **Step 1: Write the failing CLI test** — create `tests/test_accrue_brix_cli.py`

```python
"""accrue_brix.py drives run_archive_accrual — no per-token RPC sweep (#411)."""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import accrue_brix  # noqa: E402

from lfg_core import brix_drip, config  # noqa: E402


@pytest.fixture()
def fake_dbs(monkeypatch, tmp_path):
    from lfg_core import history_store, nft_index

    monkeypatch.setattr(history_store, "history_db_path", lambda net: str(tmp_path / f"h_{net}.db"))
    monkeypatch.setattr(nft_index, "index_db_path", lambda net: str(tmp_path / f"o_{net}.db"))
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")

    async def ok(conn, network):
        return None

    monkeypatch.setattr(brix_drip, "verify_endpoint_chain", ok)
    return tmp_path


def test_cli_calls_archive_accrual_and_never_fetches_offers(monkeypatch, fake_dbs, capsys):
    called = {}

    def fake_run(conn, network, system_accounts, today=None):
        called["args"] = (network, today)
        return [brix_drip.EpochReport("2026-08-18", 3, 1, 0, 0, 0, 0)]

    async def boom(*a, **k):
        raise AssertionError("live offer sweep must not run")

    monkeypatch.setattr(brix_drip, "run_archive_accrual", fake_run)
    monkeypatch.setattr(brix_drip, "fetch_sell_offer_state", boom)
    monkeypatch.setattr(sys, "argv", ["accrue_brix.py", "--network", "testnet", "--date", "2026-08-19"])
    assert accrue_brix.main() == 0
    assert called["args"] == ("testnet", "2026-08-19")
    assert "accrued=3" in capsys.readouterr().out


def test_cli_prints_deferral_reason(monkeypatch, fake_dbs, capsys):
    monkeypatch.setattr(
        brix_drip, "run_archive_accrual",
        lambda *a, **k: [brix_drip.EpochReport("2026-08-18", 0, 0, 0, 0, 0, 0, deferred="continuity gap recorded (x)")],
    )
    monkeypatch.setattr(sys, "argv", ["accrue_brix.py", "--network", "testnet"])
    assert accrue_brix.main() == 0
    out = capsys.readouterr().out
    assert "DEFERRED" in out and "continuity gap" in out


def test_cli_hints_rederive_on_unknown(monkeypatch, fake_dbs, capsys):
    monkeypatch.setattr(
        brix_drip, "run_archive_accrual",
        lambda *a, **k: [brix_drip.EpochReport("2026-08-18", 0, 0, 0, 0, 0, 7)],
    )
    monkeypatch.setattr(sys, "argv", ["accrue_brix.py", "--network", "testnet"])
    assert accrue_brix.main() == 0
    assert "derive_history_events.py" in capsys.readouterr().out


def test_cli_refuses_network_mismatch(monkeypatch, fake_dbs, capsys):
    monkeypatch.setattr(sys, "argv", ["accrue_brix.py", "--network", "mainnet"])
    assert accrue_brix.main() == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_accrue_brix_cli.py -q`
Expected: first test FAILS (`live offer sweep must not run` or `accrued=` missing) — the current script still sweeps.

- [ ] **Step 3: Rewrite `_amain` in `scripts/accrue_brix.py`** (keep `system_accounts`, `_utc_date`, arg parsing, the network-mismatch check, `verify_endpoint_chain`):

Replace the body from `excluded = system_accounts()` to the end of `_amain` with:
```python
    excluded = system_accounts()
    # No per-token sweep (#411 option 2): owner-of-record and listed-state come
    # from the history archive as of each epoch's close. Zero RPCs; DB-bound.
    reports = brix_drip.run_archive_accrual(hconn, args.network, excluded, today=args.date)
    if not reports:
        print(f"[{args.network}] nothing to accrue — cursor is current")
        return 0

    for r in reports:
        if r.deferred:
            print(
                f"[{args.network}] {r.epoch}: DEFERRED — {r.deferred}. Nothing written; the "
                f"cursor stays at the last certified epoch and this run will complete it later."
            )
            continue
        print(
            f"[{args.network}] {r.epoch}: accrued={r.accrued} listed={r.skipped_listed} "
            f"system={r.skipped_system} burned={r.skipped_burned} "
            f"ownerless={r.skipped_ownerless} unknown={r.unknown}"
        )
        if r.unknown:
            # Fail-closed under-accrual: listing state could not be reconstructed
            # for these tokens (legacy nft_events rows without offer_index /
            # offer_flags). The PK means a later run will NOT retroactively
            # grant them — rebuild the derived table, then the gap backfill can.
            print(
                f"[{args.network}] WARNING: {r.unknown} tokens skipped on unknown listing state — "
                f"run scripts/derive_history_events.py --network {args.network} to populate "
                f"offer_index/offer_flags, then scripts/backfill_brix_gap.py for the missed epochs"
            )

    outstanding = hconn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_accruals WHERE claim_id IS NULL"
    ).fetchone()[0]
    print(f"[{args.network}] total unclaimed liability: {outstanding} BRIX")
    return 0
```
Remove the now-unused `nft_index` usage (`oconn = nft_index.init_db(...)` and `tokens = nft_index.live_nfts(oconn)`) and its import if unused. Update the module docstring: the "checks each one's live sell offers on-ledger" sentence → "replays the history archive (`epoch_state`) for owner-of-record and listed-state as of each epoch's close — zero per-token RPCs (#411)". Keep the network-mismatch comment but reword: the chain-identity checks now guard the archive's network identity, not offer lookups.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_accrue_brix_cli.py tests/test_brix_drip.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/accrue_brix.py tests/test_accrue_brix_cli.py
git commit -m "feat(brix): accrue_brix.py reads the archive, drops the per-token offer sweep (#411)"
```

---

### Task 6: `lfg_core/brix_backfill.py` + `scripts/backfill_brix_gap.py` (#412)

**Files:**
- Create: `lfg_core/brix_backfill.py`
- Create: `scripts/backfill_brix_gap.py`
- Test: `tests/test_brix_backfill.py`

**Interfaces:**
- Consumes: `epoch_state.EpochReplay`, `epoch_state.certify_epoch`, `brix_drip.accrue_epoch`, `brix_drip.evaluate_accruals`, `brix_drip.record_accruals`, `brix_drip.Accrual`.
- Produces:
```python
@dataclass(frozen=True)
class EpochLine:
    epoch: str
    brix: int                 # rows that WOULD be / WERE newly written for this epoch
    listed: int
    unknown: int
    deferred: str | None

@dataclass(frozen=True)
class GapPlan:
    epochs: list[EpochLine]
    total_brix: int                     # sum over payable epochs of rows (1 BRIX each)
    wallets: dict[str, int]             # owner -> brix
    nfts: int                           # distinct nft_ids credited
    deferred: list[tuple[str, str]]     # (epoch, reason)
    written: int                        # rows actually inserted (0 on dry run)

def epoch_range(start: str, end: str) -> list[str]            # inclusive, raises ValueError if end < start
def plan_gap_backfill(hconn, network, system_accounts, *, start, end, apply: bool,
                      certify=epoch_state.certify_epoch, replay_factory=epoch_state.EpochReplay) -> GapPlan
```
`plan_gap_backfill` never reads or writes `brix_meta`. On `apply=False` it computes rows via `brix_drip.evaluate_accruals` and **subtracts rows that already exist** in `brix_accruals` (so a dry run after a partial apply reports only what's still owed); on `apply=True` it calls `brix_drip.record_accruals` per epoch (INSERT OR IGNORE) and reports the inserted count. Deferred epochs are skipped (not a stop: a historical gap in the middle should not hide the epochs after it — unlike the nightly cursor walk, there's no cursor to protect here).

- [ ] **Step 1: Write failing tests** — create `tests/test_brix_backfill.py`

```python
"""#412 gap reimbursement: strict historical reconstruction via epoch_state.

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import pytest

from lfg_core import brix_backfill, brix_drip, epoch_state, history_store

ALICE, BOB, SYS = "rAlice", "rBob", "rSystem"
D = {"2026-01-01": 1767225600, "2026-01-02": 1767312000, "2026-01-03": 1767398400,
     "2026-01-04": 1767484800, "2026-01-05": 1767571200}
FAR = 4102444800  # 2100-01-01


@pytest.fixture()
def h(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    brix_drip.ensure_schema(conn)
    conn.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " updated_at) VALUES ('testnet', ?, 1, ?, 1)", ("G" * 64, FAR),
    )
    conn.commit()
    yield conn
    conn.close()


_n = {"i": 0}


def ev(conn, event, *, ts, nft_id="N1", from_addr=None, to_addr=None, offer_index=None, offer_flags=None):
    _n["i"] += 1
    history_store.insert_nft_event(conn, {
        "tx_hash": f"T{_n['i']}", "nft_id": nft_id, "event": event, "from_addr": from_addr,
        "to_addr": to_addr, "ts": ts, "ledger_index": _n["i"],
        "offer_index": offer_index, "offer_flags": offer_flags,
    })
    conn.commit()


def plan(h, *, start="2026-01-01", end="2026-01-04", apply=False, system=frozenset()):
    return brix_backfill.plan_gap_backfill(h, "testnet", system, start=start, end=end, apply=apply)


def test_epoch_range_inclusive_and_validated():
    assert brix_backfill.epoch_range("2026-01-01", "2026-01-03") == ["2026-01-01", "2026-01-02", "2026-01-03"]
    with pytest.raises(ValueError):
        brix_backfill.epoch_range("2026-01-03", "2026-01-01")


def test_nft_earns_only_from_its_mint_epoch(h):
    ev(h, "mint", ts=D["2026-01-03"] + 1, to_addr=ALICE)
    p = plan(h)
    assert p.total_brix == 2 and p.wallets == {ALICE: 2}   # 01-03, 01-04
    assert [(l.epoch, l.brix) for l in p.epochs] == [
        ("2026-01-01", 0), ("2026-01-02", 0), ("2026-01-03", 1), ("2026-01-04", 1)]


def test_transfer_splits_credit_at_epoch_boundary(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "transfer", ts=D["2026-01-03"] + 1, from_addr=ALICE, to_addr=BOB)
    p = plan(h)
    assert p.wallets == {ALICE: 2, BOB: 2}


def test_listed_epochs_earn_nothing(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "offer_create", ts=D["2026-01-02"] + 1, from_addr=ALICE, offer_index="O1", offer_flags=1)
    ev(h, "offer_cancel", ts=D["2026-01-04"] + 1, from_addr=ALICE, offer_index="O1", offer_flags=1)
    p = plan(h)
    assert p.wallets == {ALICE: 2}   # 01-01 and 01-04
    assert [l.listed for l in p.epochs] == [0, 1, 1, 0]


def test_burned_token_stops_earning(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "burn", ts=D["2026-01-03"] + 1, from_addr=ALICE)
    assert plan(h).wallets == {ALICE: 2}


def test_system_wallets_earn_nothing(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=SYS)
    assert plan(h, system=frozenset({SYS})).total_brix == 0


def test_exploit_regression_current_holder_gets_nothing_for_pre_purchase_epochs(h):
    """A floor NFT bought AFTER the window must not hand its buyer the window."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "sale", ts=D["2026-01-05"] + 1, from_addr=ALICE, to_addr=BOB)   # after end=01-04
    p = plan(h)
    assert p.wallets == {ALICE: 4} and BOB not in p.wallets


def test_apply_writes_rows_claimable_through_existing_flow_and_is_idempotent(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    first = plan(h, apply=True)
    assert first.written == 4 and brix_drip.claimable(h, ALICE) == 4
    second = plan(h, apply=True)
    assert second.written == 0 and brix_drip.claimable(h, ALICE) == 4
    assert h.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 4
    # the dry run after a full apply reports nothing left owed
    assert plan(h).total_brix == 0


def test_apply_never_touches_the_nightly_cursor(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    brix_drip.set_meta(h, brix_drip.LAST_ACCRUED_EPOCH, "2025-12-31")
    plan(h, apply=True)
    assert brix_drip.get_meta(h, brix_drip.LAST_ACCRUED_EPOCH) == "2025-12-31"
    h.execute("DELETE FROM brix_meta")
    h.commit()
    plan(h, apply=True)
    assert brix_drip.get_meta(h, brix_drip.LAST_ACCRUED_EPOCH) is None


def test_uncertified_epoch_is_reported_and_skipped_not_fatal(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    h.execute("UPDATE archive_state SET validated_close_time = ?", (D["2026-01-04"],))  # saw through 01-03 only
    h.commit()
    p = plan(h, apply=True)
    assert [e for e, _ in p.deferred] == ["2026-01-04"]
    assert p.written == 3 and brix_drip.claimable(h, ALICE) == 3


def test_backfilled_accrual_binds_under_one_open_claim_index(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    plan(h, apply=True)
    claim_id, amount = brix_drip.open_claim(h, ALICE, last_ledger_seq=10_000)[:2] \
        if not hasattr(brix_drip, "ClaimHandle") else (None, None)
    # open_claim's exact return shape is in lfg_core/brix_drip.py ~L558 — adapt
    # the unpacking to it; the assertion that matters:
    assert h.execute(
        "SELECT COUNT(*) FROM brix_accruals WHERE owner=? AND claim_id IS NOT NULL", (ALICE,)
    ).fetchone()[0] == 4
```
Before running, open `lfg_core/brix_drip.py` at `def open_claim` and fix the last test's unpacking to its real signature/return (it binds all unclaimed accruals of the wallet to a new claim row). Remove the `hasattr` hedge — write the real call.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_brix_backfill.py -q`
Expected: FAIL — `ImportError: cannot import name 'brix_backfill'`.

- [ ] **Step 3: Create `lfg_core/brix_backfill.py`**

```python
"""BRIX drip gap reimbursement (#412): strict historical reconstruction.

Each NFT earns for exactly the epochs it was live, unlisted and held by a
non-system wallet, credited to whoever held it at each epoch's close — replayed
from the history archive by `epoch_state`, written as ordinary
`brix_accruals` rows that the existing claim flow pays. No new payout path.

Dry-run by default; `apply=True` writes. The nightly cursor
(`brix_meta.last_accrued_epoch`) is NEVER read or written here: this module
writes historical rows only and must not be able to make the nightly job
skip forward.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from lfg_core import brix_drip, epoch_state


@dataclass(frozen=True)
class EpochLine:
    epoch: str
    brix: int
    listed: int
    unknown: int
    deferred: str | None = None


@dataclass(frozen=True)
class GapPlan:
    epochs: list[EpochLine]
    total_brix: int
    wallets: dict[str, int]
    nfts: int
    deferred: list[tuple[str, str]]
    written: int = 0
    top: list[tuple[str, int]] = field(default_factory=list)


def epoch_range(start: str, end: str) -> list[str]:
    """Inclusive list of UTC dates start..end (YYYY-MM-DD)."""
    a = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    b = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if b < a:
        raise ValueError(f"--to {end} is before --from {start}")
    out = []
    while a <= b:
        out.append(a.strftime("%Y-%m-%d"))
        a += timedelta(days=1)
    return out


def _already_accrued(conn: sqlite3.Connection, epoch: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT nft_id FROM brix_accruals WHERE epoch_date = ?", (epoch,))}


def plan_gap_backfill(
    hconn: sqlite3.Connection,
    network: str,
    system_accounts: frozenset[str],
    *,
    start: str,
    end: str,
    apply: bool,
    certify: Callable[[sqlite3.Connection, str, str], str | None] = epoch_state.certify_epoch,
    replay_factory: Callable[[sqlite3.Connection], Any] = epoch_state.EpochReplay,
    top_n: int = 20,
) -> GapPlan:
    """Walk start..end through the same per-epoch path as the nightly job.

    Uncertified epochs are reported and SKIPPED (not a stop — a healed
    historical gap must not hide the epochs after it). On a dry run, rows that
    already exist are subtracted so the report shows what is still owed.
    """
    replay = replay_factory(hconn)
    lines: list[EpochLine] = []
    wallets: Counter[str] = Counter()
    nft_ids: set[str] = set()
    deferred: list[tuple[str, str]] = []
    written = 0
    for epoch in epoch_range(start, end):
        reason = certify(hconn, network, epoch)
        if reason is not None:
            deferred.append((epoch, reason))
            lines.append(EpochLine(epoch, 0, 0, 0, deferred=reason))
            continue
        tokens = replay.advance_to(epoch)
        result = brix_drip.evaluate_accruals(
            list(tokens.values()),
            listed_fn=lambda nft_id, _t=tokens: _t[nft_id].listed,
            system_accounts=system_accounts,
            epoch=epoch,
        )
        existing = _already_accrued(hconn, epoch)
        fresh = [r for r in result.rows if r.nft_id not in existing]
        if apply:
            written += brix_drip.record_accruals(hconn, fresh)
        for r in fresh:
            wallets[r.owner] += int(r.amount)
            nft_ids.add(r.nft_id)
        lines.append(EpochLine(epoch, sum(int(r.amount) for r in fresh), result.skipped_listed, result.unknown))
    total = sum(wallets.values())
    return GapPlan(
        epochs=lines,
        total_brix=total,
        wallets=dict(wallets),
        nfts=len(nft_ids),
        deferred=deferred,
        written=written,
        top=wallets.most_common(top_n),
    )
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_brix_backfill.py -q`
Expected: PASS.

- [ ] **Step 5: Create `scripts/backfill_brix_gap.py`** (CLI + report; reuses `accrue_brix.system_accounts` so the two exclusion rosters can never drift)

```python
#!/usr/bin/env python3
"""Reimburse the BRIX drip gap (#412) from historical ownership.

  python scripts/backfill_brix_gap.py --network mainnet               # dry run (default)
  python scripts/backfill_brix_gap.py --network mainnet --apply       # write accruals
  python scripts/backfill_brix_gap.py --network testnet --from 2026-08-01 --to 2026-08-10

Window defaults: --from 2025-09-15 (the day after the last real payout run,
see the #412 spec), --to yesterday (UTC). Strict historical: each NFT earns
1 BRIX for each epoch it was live, unlisted and held by a non-system wallet,
credited to the holder at that epoch's close. Rows land in `brix_accruals`
and are claimed through the existing POST /api/brix/claim flow — nothing is
paid here, unclaimed backpay never leaves the treasury.

The nightly cursor (`brix_meta.last_accrued_epoch`) is never touched.
Idempotent: re-running is a no-op; a partial run resumes.

PREREQUISITES (spec §Ops): re-derive the archive first so nft_events carries
offer_index/offer_flags (`scripts/derive_history_events.py --network <net>`),
and make sure the archive is certified for the window (otherwise every epoch
reports DEFERRED and nothing is written).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from accrue_brix import _utc_date, system_accounts  # noqa: E402

from lfg_core import brix_backfill, brix_drip, config, history_store  # noqa: E402

DEFAULT_FROM = "2025-09-15"


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


async def _treasury_balance(address: str) -> float | None:
    """Distributor's BRIX balance, or None when unreadable (report-only)."""
    try:
        from lfg_core import xrpl_ops

        lines = await xrpl_ops.get_account_lines(address)  # adapt to the real helper name in xrpl_ops
    except Exception:  # noqa: BLE001 — the report must not fail on a balance read
        return None
    for line in lines or []:
        if line.get("account") == config.BRIX_ISSUER and line.get("currency") == config.BRIX_CURRENCY_HEX:
            try:
                return float(line.get("balance") or 0)
            except (TypeError, ValueError):
                return None
    return None


def _print_report(network: str, plan: brix_backfill.GapPlan, *, applied: bool, liability: int,
                  treasury: float | None) -> None:
    verb = "WRITTEN" if applied else "WOULD WRITE"
    print(f"[{network}] {verb}: {plan.total_brix} BRIX to {len(plan.wallets)} wallets over {plan.nfts} NFTs")
    if applied:
        print(f"[{network}] rows inserted this run: {plan.written}")
    print(f"[{network}] per-epoch (epoch brix listed unknown):")
    for line in plan.epochs:
        tag = f"  DEFERRED — {line.deferred}" if line.deferred else ""
        print(f"  {line.epoch} {line.brix:6d} {line.listed:6d} {line.unknown:6d}{tag}")
    if plan.top:
        print(f"[{network}] top wallets:")
        for wallet, amount in plan.top:
            print(f"  {wallet} {amount}")
    if plan.deferred:
        print(f"[{network}] {len(plan.deferred)} epoch(s) failed certification and were NOT written:")
        for epoch, reason in plan.deferred:
            print(f"  {epoch}: {reason}")
    unknown_total = sum(line.unknown for line in plan.epochs)
    if unknown_total:
        print(
            f"[{network}] WARNING: {unknown_total} token-epochs skipped on unknown listing state — "
            f"re-run scripts/derive_history_events.py --network {network} first"
        )
    print(f"[{network}] outstanding unclaimed liability (incl. this run if applied): {liability} BRIX")
    if treasury is None:
        print(f"[{network}] treasury balance: unavailable")
    else:
        print(f"[{network}] treasury balance: {treasury:.0f} BRIX; headroom after backfill: {treasury - liability - (0 if applied else plan.total_brix):.0f}")


async def _amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", default=config.XRPL_NETWORK, choices=["testnet", "mainnet"])
    ap.add_argument("--from", dest="start", type=_utc_date, default=DEFAULT_FROM, metavar="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", type=_utc_date, default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="write accrual rows (default: dry run)")
    ap.add_argument("--top", type=int, default=20, help="top-N wallets to print")
    ap.add_argument("--treasury", default=config.BRIX_DISTRIBUTOR_ADDRESS,
                    help="wallet whose BRIX balance to compare against (default: distributor)")
    args = ap.parse_args()
    end = args.end or _yesterday()
    if args.network != config.XRPL_NETWORK:
        print(f"refusing: --network {args.network} but XRPL_NETWORK is {config.XRPL_NETWORK}")
        return 2

    hconn = history_store.init_history_db(history_store.history_db_path(args.network))
    brix_drip.ensure_schema(hconn)
    chain_error = await brix_drip.verify_endpoint_chain(hconn, args.network)
    if chain_error:
        print(f"refusing: {chain_error}")
        return 2

    plan = brix_backfill.plan_gap_backfill(
        hconn, args.network, system_accounts(), start=args.start, end=end, apply=args.apply, top_n=args.top,
    )
    liability = hconn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_accruals WHERE claim_id IS NULL"
    ).fetchone()[0]
    treasury = await _treasury_balance(args.treasury) if args.treasury else None
    _print_report(args.network, plan, applied=args.apply, liability=int(liability), treasury=treasury)
    if not args.apply:
        print(f"[{args.network}] dry run — re-run with --apply to write")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
```
Before committing: `grep -n "async def get_account_lines\|account_lines" lfg_core/xrpl_ops.py scripts/brix_admin_report.py` and use whatever helper `brix_admin_report.py` already uses for the distributor balance (keep `_treasury_balance` returning `None` on any failure).

- [ ] **Step 6: Smoke the CLI against the mainnet archive read-only (dry run only, no --apply)**

Run (from the worktree, it reads `~/LFG/history_mainnet.db` via `history_db_path` — check the path resolves; if `history_db_path` is CWD-relative, run with `cd /home/hamsa/LFG && <WT>/.venv/bin/python <WT>/scripts/backfill_brix_gap.py --network mainnet --to 2025-09-20`):
Expected: runs without traceback; most epochs show large `unknown` (legacy rows lack `offer_index`) — that is the expected pre-rederive fail-closed state, proving the gate works. Record the output in the PR description.

- [ ] **Step 7: ruff + mypy on the new files**

Run: `.venv/bin/ruff check lfg_core/brix_backfill.py lfg_core/epoch_state.py scripts/backfill_brix_gap.py scripts/accrue_brix.py && .venv/bin/ruff format --check lfg_core scripts tests && .venv/bin/mypy lfg_core/brix_backfill.py lfg_core/epoch_state.py lfg_core/brix_drip.py`
Expected: clean (fix anything reported).

- [ ] **Step 8: Commit**

```bash
git add lfg_core/brix_backfill.py scripts/backfill_brix_gap.py tests/test_brix_backfill.py
git commit -m "feat(brix): backfill_brix_gap.py — strict historical drip reimbursement, dry-run by default (#412)"
```

---

### Task 7: Docs + spec status + ops runbook

**Files:**
- Modify: `CLAUDE.md` (section "BRIX daily drip (#48)" — the `Ops:` block and the "Unlisted is checked on-ledger" bullet)
- Modify: `docs/superpowers/specs/2026-08-20-epoch-accurate-accrual-design.md`, `docs/superpowers/specs/2026-08-20-brix-gap-reimbursement-design.md` (Status line)
- Create: `docs/ops/brix-gap-backfill.md`
- Modify: `README.md` only if `scripts/check_repo_layout.py` requires new `scripts/*` to be listed (run the hook; it only mandates `lfg_core/*_flow.py` and `surfaces/<pkg>/`, so likely no change)

- [ ] **Step 1: CLAUDE.md edits**

Replace the bullet starting `- **"Unlisted" is checked on-ledger and fails closed.**` with:
```
- **"Unlisted" is reconstructed from the history archive and fails closed
  (#411 option 2).** `lfg_core/epoch_state.py` replays `nft_events` to the
  close of each epoch for owner-of-record + listed-state (a sell offer counts
  only while its creator is still the holder; destination-locked offers
  count; buy offers never do) — zero per-token RPCs. An epoch pays only when
  `epoch_state.certify_epoch` passes (certified baseline, no continuity gap,
  archive validated past the epoch close); otherwise it is **deferred** —
  nothing written, `brix_meta.last_accrued_epoch` stays behind it — and the
  next run completes it once the listener's auto catch-up heals the archive.
  Unknown listing state (legacy `nft_events` rows without
  `offer_index`/`offer_flags`) pays **nothing**; re-run
  `scripts/derive_history_events.py` to populate them, then
  `scripts/backfill_brix_gap.py` to reimburse the skipped epochs.
```
In the `Ops:` block add after the accrue/report lines:
```
  # #412 gap reimbursement — dry run first, review the report, then --apply
  .venv/bin/python scripts/derive_history_events.py --network mainnet --distributor <current-distributor>
  .venv/bin/python scripts/backfill_brix_gap.py --network mainnet            # dry run
  .venv/bin/python scripts/backfill_brix_gap.py --network mainnet --apply
```
Update the `accrue_brix.py refuses to run (exit 2)` sentence: "…on the wrong chain every token looks unlisted" → "…the archive's chain identity must match the endpoint's".

- [ ] **Step 2: Spec status lines**

Both specs: `**Status:** approved design, not yet implemented` → `**Status:** implemented — PR <n> (branch feat/411-412-epoch-accrual)` (fill the PR number after opening it; commit the stamp as part of the PR).

- [ ] **Step 3: Create `docs/ops/brix-gap-backfill.md`**

```markdown
# BRIX drip gap reimbursement — runbook (#412)

Spec: `docs/superpowers/specs/2026-08-20-brix-gap-reimbursement-design.md`.
Window: 2025-09-15 (day after the last real payout run) → yesterday.

## 0. Prerequisites (once, after this code is deployed)
1. Re-derive the archive so `nft_events` carries `offer_index`/`offer_flags`
   (also repairs the stale `brix_events` — ~14,500 2025 payouts missing):
   `.venv/bin/python scripts/derive_history_events.py --network mainnet --distributor rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ`
2. Confirm the archive is certified and gap-free:
   `sqlite3 history_mainnet.db "select baseline_complete, continuity_gap_reason, datetime(validated_close_time,'unixepoch') from archive_state"`
   → `1 | NULL | <recent>`. If not, run the catch-up
   (`scripts/backfill_history.py --network mainnet --catch-up-from-gap --baseline-provenance "…" --distributor rwr84Q…`).
3. Confirm the nightly job is healthy: `pm2 logs lfg-brix-accrue --lines 20` shows `accrued=` lines with `unknown=0`.

## 1. Rehearse on staging (testnet)
`cd ~/LFG-staging && .venv/bin/python scripts/backfill_brix_gap.py --network testnet --from <d1> --to <d2>` then `--apply`; claim one backfilled balance via the Activity.

## 2. Mainnet dry run
`.venv/bin/python scripts/backfill_brix_gap.py --network mainnet > reports/brix_gap_dryrun.txt`
Review: total vs the ~1.23M NFT-day upper bound, top-wallet concentration,
`DEFERRED` epochs (must be none), `unknown` (must be 0), treasury headroom.

## 3. Apply
`.venv/bin/python scripts/backfill_brix_gap.py --network mainnet --apply | tee reports/brix_gap_apply.txt`
Then `scripts/brix_admin_report.py --network mainnet` and
`scripts/audit_brix_distribution.py --network mainnet` (must PASS).
Re-running `--apply` is a no-op. The nightly cursor is never moved.

## Rollback
Rows are DB-only until claimed. To withdraw an unclaimed backfill:
`DELETE FROM brix_accruals WHERE claim_id IS NULL AND epoch_date BETWEEN '2025-09-15' AND '<to>'`
(never delete rows with a `claim_id` — a claim may be in flight).
```

- [ ] **Step 4: Run the full gate locally**

Run: `.venv/bin/python -m pytest -q -x` then `.venv/bin/python scripts/check_repo_layout.py`
Expected: all PASS.

- [ ] **Step 5: Commit, push, open PR**

```bash
git add CLAUDE.md docs/ops/brix-gap-backfill.md docs/superpowers/specs/2026-08-20-*.md
git commit -m "docs(brix): archive-driven accrual + gap reimbursement runbook (#411, #412)"
git push -u origin feat/411-412-epoch-accrual
gh pr create --repo Team-Hamsa/LFG --title "feat(brix): epoch-accurate accrual from the archive + gap reimbursement (#411, #412)" --body "<summary: what changed, the fail-closed rules, the dry-run smoke output from Task 6 step 6, ops steps from docs/ops/brix-gap-backfill.md; Closes #411, Closes #412>"
```
Then babysit Greptile + CodeRabbit per the global CLAUDE.md rules (fix + reply on every thread), and stamp the spec Status lines with the PR number.

---

## Self-review

**Spec coverage (#411):** §1 offer flag + index → Task 1. §2 `epoch_state` rules (owner follows mint/transfer/sale/burn; creator-must-be-holder; destination-locked counts; buy never) → Task 2 tests one-per-rule. §3 certification (baseline, validated past close, no gap; defer without advancing cursor) → Tasks 3+4. §4 accrue_brix zero-RPC, `verify_endpoint_chain` kept, `fetch_sell_offer_state` retained → Task 5. Testing list: synthetic-archive rules ✔, gate defer-then-complete-once ✔ (`test_uncertified_epoch_defers_and_leaves_cursor_behind`), idempotence ✔, listed-token regression from archive fixtures ✔. Ops notes (00:40 slot unchanged; recert with current distributor) → Task 7 runbook.
**Spec coverage (#412):** window defaults, per-epoch path identical to nightly, cursor never moved, certification gate, report (total/wallets/nfts, per-epoch series, top-N, uncertified list, treasury + liability) → Task 6. Testing list: mint mid-window ✔, transfer split ✔, listed partial ✔, burn ✔, exploit regression ✔, idempotence ✔, cursor safety ✔, claim integration ✔. Ops prerequisites → runbook.
**Deviation from spec, deliberate:** `state_at_epoch(hconn, epoch)` drops the spec's unused `oconn` parameter (nft_events is already collection-scoped); the backfill SKIPS deferred epochs rather than stopping (no cursor to protect there) while the nightly STOPS (cursor must not jump a gap). Known limitation recorded in Task 2's module docstring: a single `NFTokenCancelOffer` deleting two offers on the same token collides on the `(tx_hash, nft_id)` PK and loses one → that token may read as still-listed (under-pay, fail-closed direction).
**Placeholder scan:** Task 6 step 5 names one adaptation point (`xrpl_ops` account-lines helper name) and Task 6 step 1 one (`open_claim` return shape) — both point at the exact file/function to read; everything else is literal code.
**Type consistency:** `EpochToken(nft_id, owner, listed, live)` + `is_burned` property used identically in Tasks 2/4/6; `EpochReport(epoch, accrued, skipped_listed, skipped_burned, skipped_system, skipped_ownerless, unknown, deferred=None)` positional order matches the existing dataclass + the new trailing field; `certify(conn, network, epoch)` and `replay_factory(conn)` signatures match between `brix_drip.run_archive_accrual`, `brix_backfill.plan_gap_backfill` and the tests' fakes.
