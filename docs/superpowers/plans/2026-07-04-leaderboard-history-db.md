# Leaderboard + Ledger History Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-network SQLite archive of every XRPL transaction touching the LFG collection and BRIX token, plus a time-filterable leaderboard (API + Activity UI) computed from it.

**Architecture:** Raw txs land in `history_<network>.db` (`xrpl_txs`) from four backfill sources + the live listener; pure functions derive `nft_events` / `brix_events`; `lfg_core/leaderboard.py` turns events + the existing `onchain_<network>.db` into ranked boards served by one public aiohttp endpoint; a vanilla-JS card renders it on the Activity home.

**Tech Stack:** Python 3.10, sqlite3, xrpl-py `AsyncWebsocketClient` raw-dict requests (existing pattern in `lfg_core/nft_index.py:240`), aiohttp (existing `lfg_service/app.py`), vanilla JS no-build client.

**Spec:** `docs/superpowers/specs/2026-07-04-leaderboard-history-db-design.md`

## Global Constraints

- All new XRPL *submitting* code is out of scope — this project only READS the ledger. No SourceTag concerns.
- clio-only methods (`nft_history`, `nfts_by_issuer`) must use `config.CLIO_WS_URL` / the per-network clio endpoint, never `WS_URL`.
- History DBs are gitignored and regenerable: `history_testnet.db` / `history_mainnet.db`.
- New test files that import `lfg_core` at module top MUST copy the env-guard preamble used by existing tests (see `tests/test_nft_listener.py` head: set `BUNNY_PULL_ZONE`, `LAYER_SOURCE`, etc. **before** the import). Copy it verbatim from an existing test.
- Ripple epoch offset: unix = ripple_time + 946684800.
- BRIX currency: hex `4252495800000000000000000000000000000000`, also matches ledger form `"BRIX"`. Issuers per network from `config`: NFT issuer = `config.SWAP_ISSUER_ADDRESS`, BRIX issuer = `config.SWAP_OFFER_ISSUER`.
- Run tests with `.venv/bin/python -m pytest`. Pre-push hook runs the full suite; local `.env` has `ECONOMY_ENABLED=0`, so push with `ECONOMY_ENABLED=1 git push`.
- Commit style: `feat(history): …` / `feat(leaderboard): …`, Co-Authored-By Claude trailer, work on a feature branch `feat/leaderboard-history-db`, draft PR at the end.

## File Structure

- `lfg_core/history_store.py` — schema, connections, insert/cursor helpers (new)
- `lfg_core/history_events.py` — pure tx→events derivation (new)
- `lfg_core/leaderboard.py` — board queries + period math (new)
- `scripts/backfill_history.py` — resumable backfill CLI (new)
- `scripts/snapshot_balances.py` — nightly BRIX/LP balance snapshot CLI (new)
- `scripts/onchain_listener.py` — extend `process_stream_tx`/`_listen` to feed history DB (modify)
- `lfg_service/app.py` — `GET /api/leaderboard` (modify)
- `webapp/client/index.html`, `app.js`, `style.css` — leaderboard card (modify)
- `tests/test_history_store.py`, `tests/test_history_events.py`, `tests/test_backfill_history.py`, `tests/test_leaderboard.py`, `tests/test_leaderboard_api.py`, `tests/test_snapshot_balances.py`, `tests/fixtures/history_txs.py` (new)

---

### Task 1: History store module (schema + inserts + cursors)

**Files:**
- Create: `lfg_core/history_store.py`
- Test: `tests/test_history_store.py`

**Interfaces:**
- Produces:
  - `history_db_path(network: str) -> str` (env override `HISTORY_DB_PATH`)
  - `init_history_db(path: str) -> sqlite3.Connection` (creates schema, `sqlite3.Row` row_factory, WAL)
  - `insert_tx(conn, *, tx_hash, ledger_index, close_time, tx_type, account, source_tag, raw_json) -> bool` — True if newly inserted (`INSERT OR IGNORE`)
  - `get_cursor(conn, source: str) -> str | None` / `set_cursor(conn, source: str, cursor: str | None)`
  - `insert_nft_event(conn, ev: dict)` / `insert_brix_event(conn, ev: dict)` — `INSERT OR REPLACE`, keys per schema
  - `clear_derived(conn)` — truncate `nft_events` + `brix_events`
  - `upsert_snapshot(conn, snap_date: str, account: str, brix: float, lp_tokens: float)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_history_store.py
# <copy the env-guard preamble from tests/test_nft_listener.py verbatim here>
import sqlite3

from lfg_core import history_store


def _conn(tmp_path):
    return history_store.init_history_db(str(tmp_path / "h.db"))


def test_insert_tx_idempotent(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(tx_hash="AB" * 32, ledger_index=5, close_time=1700000000,
              tx_type="Payment", account="rSender", source_tag=None, raw_json="{}")
    assert history_store.insert_tx(conn, **kw) is True
    assert history_store.insert_tx(conn, **kw) is False
    n = conn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0]
    assert n == 1


def test_cursor_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    assert history_store.get_cursor(conn, "issuer_tx") is None
    history_store.set_cursor(conn, "issuer_tx", '{"ledger": 1}')
    assert history_store.get_cursor(conn, "issuer_tx") == '{"ledger": 1}'
    history_store.set_cursor(conn, "issuer_tx", None)
    assert history_store.get_cursor(conn, "issuer_tx") is None


def test_events_and_clear(tmp_path):
    conn = _conn(tmp_path)
    history_store.insert_nft_event(conn, {
        "tx_hash": "CD" * 32, "nft_id": "00" * 32, "nft_number": 7,
        "event": "mint", "from_addr": None, "to_addr": "rOwner",
        "price_drops": None, "price_token": None, "ledger_index": 9, "ts": 1700000001,
    })
    history_store.insert_brix_event(conn, {
        "tx_hash": "CD" * 32, "account": "rOwner", "counterparty": "rIssuer",
        "delta": 5.0, "kind": "airdrop", "ts": 1700000001,
    })
    assert conn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM brix_events").fetchone()[0] == 1
    history_store.clear_derived(conn)
    assert conn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 0


def test_snapshot_upsert(tmp_path):
    conn = _conn(tmp_path)
    history_store.upsert_snapshot(conn, "2026-07-04", "rA", 10.0, 1.5)
    history_store.upsert_snapshot(conn, "2026-07-04", "rA", 12.0, 1.5)
    row = conn.execute("SELECT brix FROM balance_snapshots").fetchone()
    assert row["brix"] == 12.0


def test_db_path_override(monkeypatch):
    monkeypatch.setenv("HISTORY_DB_PATH", "/tmp/x.db")
    assert history_store.history_db_path("mainnet") == "/tmp/x.db"
    monkeypatch.delenv("HISTORY_DB_PATH")
    assert history_store.history_db_path("mainnet").endswith("history_mainnet.db")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_history_store.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` on `history_store`.

- [ ] **Step 3: Implement `lfg_core/history_store.py`**

```python
"""Per-network ledger history archive: raw XRPL txs + derived NFT/BRIX events.

Raw `xrpl_txs` rows are the source of truth (verbatim {tx, meta} JSON);
`nft_events` / `brix_events` are derived, droppable, rebuildable. Follows the
same per-network-file posture as lfg_core/nft_index.py (onchain_<net>.db)."""

from __future__ import annotations

import os
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS xrpl_txs (
    tx_hash      TEXT PRIMARY KEY,
    ledger_index INTEGER,
    close_time   INTEGER,
    tx_type      TEXT,
    account      TEXT,
    source_tag   INTEGER,
    raw_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_txs_time ON xrpl_txs(close_time);
CREATE INDEX IF NOT EXISTS idx_txs_type ON xrpl_txs(tx_type);

CREATE TABLE IF NOT EXISTS nft_events (
    tx_hash      TEXT,
    nft_id       TEXT,
    nft_number   INTEGER,
    event        TEXT,   -- mint|burn|transfer|sale|offer_create|offer_cancel|modify
    from_addr    TEXT,
    to_addr      TEXT,
    price_drops  INTEGER,
    price_token  TEXT,   -- JSON {currency, issuer, value} for IOU sales
    ledger_index INTEGER,
    ts           INTEGER,
    PRIMARY KEY (tx_hash, nft_id)
);
CREATE INDEX IF NOT EXISTS idx_nftev_ts ON nft_events(ts);
CREATE INDEX IF NOT EXISTS idx_nftev_nft ON nft_events(nft_id);

CREATE TABLE IF NOT EXISTS brix_events (
    tx_hash      TEXT,
    account      TEXT,
    counterparty TEXT,
    delta        REAL,
    kind         TEXT,   -- payment|airdrop|amm_swap|amm_deposit|amm_withdraw|trustset|claim
    ts           INTEGER,
    PRIMARY KEY (tx_hash, account)
);
CREATE INDEX IF NOT EXISTS idx_brixev_ts ON brix_events(ts);

CREATE TABLE IF NOT EXISTS backfill_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    snap_date TEXT,
    account   TEXT,
    brix      REAL,
    lp_tokens REAL,
    PRIMARY KEY (snap_date, account)
);
"""


def history_db_path(network: str) -> str:
    """Per-network history DB file; HISTORY_DB_PATH overrides."""
    override = os.getenv("HISTORY_DB_PATH")
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, f"history_{network}.db")


def init_history_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def insert_tx(
    conn: sqlite3.Connection,
    *,
    tx_hash: str,
    ledger_index: int | None,
    close_time: int | None,
    tx_type: str,
    account: str | None,
    source_tag: int | None,
    raw_json: str,
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO xrpl_txs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tx_hash, ledger_index, close_time, tx_type, account, source_tag, raw_json),
    )
    conn.commit()
    return cur.rowcount > 0


def get_cursor(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute("SELECT cursor FROM backfill_state WHERE source=?", (source,)).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, cursor: str | None) -> None:
    conn.execute(
        "INSERT INTO backfill_state (source, cursor, updated_at)"
        " VALUES (?, ?, CURRENT_TIMESTAMP)"
        " ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,"
        " updated_at=CURRENT_TIMESTAMP",
        (source, cursor),
    )
    conn.commit()


_NFT_EV_COLS = (
    "tx_hash", "nft_id", "nft_number", "event", "from_addr", "to_addr",
    "price_drops", "price_token", "ledger_index", "ts",
)
_BRIX_EV_COLS = ("tx_hash", "account", "counterparty", "delta", "kind", "ts")


def insert_nft_event(conn: sqlite3.Connection, ev: dict) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO nft_events ({','.join(_NFT_EV_COLS)})"
        f" VALUES ({','.join('?' * len(_NFT_EV_COLS))})",
        tuple(ev.get(c) for c in _NFT_EV_COLS),
    )


def insert_brix_event(conn: sqlite3.Connection, ev: dict) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO brix_events ({','.join(_BRIX_EV_COLS)})"
        f" VALUES ({','.join('?' * len(_BRIX_EV_COLS))})",
        tuple(ev.get(c) for c in _BRIX_EV_COLS),
    )


def clear_derived(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM nft_events")
    conn.execute("DELETE FROM brix_events")
    conn.commit()


def upsert_snapshot(
    conn: sqlite3.Connection, snap_date: str, account: str, brix: float, lp_tokens: float
) -> None:
    conn.execute(
        "INSERT INTO balance_snapshots VALUES (?, ?, ?, ?)"
        " ON CONFLICT(snap_date, account) DO UPDATE SET"
        " brix=excluded.brix, lp_tokens=excluded.lp_tokens",
        (snap_date, account, brix, lp_tokens),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_history_store.py -v` — Expected: PASS (5 tests).

- [ ] **Step 5: Add history DBs to .gitignore and commit**

Append to `.gitignore`:

```
history_testnet.db*
history_mainnet.db*
```

(`*` covers WAL/SHM sidecars.)

```bash
git checkout -b feat/leaderboard-history-db
git add lfg_core/history_store.py tests/test_history_store.py .gitignore
git commit -m "feat(history): per-network ledger history store (raw txs + derived events)"
```

---

### Task 2: Shared tx fixtures + NFT event derivation

**Files:**
- Create: `lfg_core/history_events.py`, `tests/fixtures/__init__.py` (empty), `tests/fixtures/history_txs.py`
- Test: `tests/test_history_events.py`

**Interfaces:**
- Consumes: `lfg_core.nft_listener.affected_nft_ids(tx)` (existing).
- Produces:
  - `RIPPLE_EPOCH = 946684800`; `tx_unix_time(tx: dict) -> int | None` (reads `tx["date"]`, else `tx["close_time_iso"]` ISO parse, else None)
  - `derive_nft_events(tx: dict, *, nft_issuer: str) -> list[dict]` — dicts shaped for `history_store.insert_nft_event` (no `nft_number` resolution here; that field stays None and Task 5 fills it from `onchain_<net>.db`)
  - `normalize_entry(entry: dict) -> dict` — flatten an account_tx/nft_history entry (`tx`/`tx_json` + `meta`/`metaData` + top-level `hash`/`ledger_index`/`validated`) into one tx dict with `meta`, `hash`, `ledger_index` keys.

The tx dicts passed in are always normalized: fields at top level, `meta` key holds metadata (same shape `scripts/onchain_listener.py:_normalize_stream_tx` produces).

- [ ] **Step 1: Write fixtures**

```python
# tests/fixtures/history_txs.py
"""Canned normalized XRPL tx dicts (tx fields top-level + `meta`) for
derivation tests. Shapes mirror clio account_tx / nft_history output after
history_events.normalize_entry."""

ISSUER = "rIssuerXXXXXXXXXXXXXXXXXXXXXXXXXXX"
BRIX_ISSUER = "rBrixIssuerXXXXXXXXXXXXXXXXXXXXXXX"
DISTRIBUTOR = "rAirdropXXXXXXXXXXXXXXXXXXXXXXXXXX"
ALICE = "rAliceXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
BOB = "rBobXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
NFT_A = "000A" + "0" * 60
BRIX_HEX = "4252495800000000000000000000000000000000"

MINT = {
    "TransactionType": "NFTokenMint", "Account": ISSUER, "hash": "01" * 32,
    "ledger_index": 100, "date": 800000000,
    "meta": {"nftoken_id": NFT_A, "AffectedNodes": []},
}

BURN = {
    "TransactionType": "NFTokenBurn", "Account": ISSUER, "Owner": ALICE,
    "NFTokenID": NFT_A, "hash": "02" * 32, "ledger_index": 101,
    "date": 800000100, "meta": {"AffectedNodes": []},
}

MODIFY = {
    "TransactionType": "NFTokenModify", "Account": ISSUER, "Owner": ALICE,
    "NFTokenID": NFT_A, "hash": "03" * 32, "ledger_index": 102,
    "date": 800000200, "meta": {"AffectedNodes": []},
}

def _deleted_offer(owner, amount, flags):
    return {"DeletedNode": {"LedgerEntryType": "NFTokenOffer", "FinalFields": {
        "Owner": owner, "Amount": amount, "Flags": flags, "NFTokenID": NFT_A}}}

# Alice sells to Bob for 5 XRP (Bob accepts Alice's sell offer, flag 1)
SALE_XRP = {
    "TransactionType": "NFTokenAcceptOffer", "Account": BOB, "hash": "04" * 32,
    "ledger_index": 103, "date": 800000300,
    "meta": {"nftoken_id": NFT_A, "AffectedNodes": [_deleted_offer(ALICE, "5000000", 1)]},
}

# Issuer transfers to Alice for 0 (zero-price sell offer)
TRANSFER_FREE = {
    "TransactionType": "NFTokenAcceptOffer", "Account": ALICE, "hash": "05" * 32,
    "ledger_index": 104, "date": 800000400,
    "meta": {"nftoken_id": NFT_A, "AffectedNodes": [_deleted_offer(ISSUER, "0", 1)]},
}

# Bob buys from Alice with a BUY offer (flag 0): offer.Owner = buyer, accepter = seller
SALE_IOU = {
    "TransactionType": "NFTokenAcceptOffer", "Account": ALICE, "hash": "06" * 32,
    "ledger_index": 105, "date": 800000500,
    "meta": {"nftoken_id": NFT_A, "AffectedNodes": [_deleted_offer(
        BOB, {"currency": BRIX_HEX, "issuer": BRIX_ISSUER, "value": "10"}, 0)]},
}

OFFER_CREATE = {
    "TransactionType": "NFTokenCreateOffer", "Account": ALICE, "NFTokenID": NFT_A,
    "Amount": "9000000", "Flags": 1, "hash": "07" * 32, "ledger_index": 106,
    "date": 800000600, "meta": {"AffectedNodes": []},
}

OFFER_CANCEL = {
    "TransactionType": "NFTokenCancelOffer", "Account": ALICE, "hash": "08" * 32,
    "ledger_index": 107, "date": 800000700,
    "meta": {"AffectedNodes": [_deleted_offer(ALICE, "9000000", 1)]},
}

def _ripplestate(holder, issuer, old, new, high_is_issuer=True):
    # holder as LOW account: Balance.value is the holder's (positive) balance
    low, high = (holder, issuer) if high_is_issuer else (issuer, holder)
    return {"ModifiedNode": {"LedgerEntryType": "RippleState", "FinalFields": {
        "Balance": {"currency": BRIX_HEX, "issuer": "rrrrrrrrrrrrrrrrrrrrBZbvji", "value": str(new)},
        "HighLimit": {"issuer": high, "currency": BRIX_HEX, "value": "0"},
        "LowLimit": {"issuer": low, "currency": BRIX_HEX, "value": "0"},
    }, "PreviousFields": {"Balance": {"currency": BRIX_HEX, "value": str(old)}}}}

# Distributor sends Alice 3 BRIX. Alice is LOW account (holder balance positive):
# old 10 -> new 13; distributor is low in its own line: old -50 ... keep one node
# per account for clarity.
AIRDROP = {
    "TransactionType": "Payment", "Account": DISTRIBUTOR, "Destination": ALICE,
    "Amount": {"currency": BRIX_HEX, "issuer": BRIX_ISSUER, "value": "3"},
    "hash": "09" * 32, "ledger_index": 108, "date": 800000800,
    "meta": {"AffectedNodes": [
        _ripplestate(ALICE, BRIX_ISSUER, 10, 13),
        _ripplestate(DISTRIBUTOR, BRIX_ISSUER, 50, 47),
    ]},
}

TRUSTSET = {
    "TransactionType": "TrustSet", "Account": BOB,
    "LimitAmount": {"currency": BRIX_HEX, "issuer": BRIX_ISSUER, "value": "1000000"},
    "hash": "0A" * 32, "ledger_index": 109, "date": 800000900,
    "meta": {"AffectedNodes": []},
}

AMM_DEPOSIT = {
    "TransactionType": "AMMDeposit", "Account": ALICE, "hash": "0B" * 32,
    "ledger_index": 110, "date": 800001000,
    "meta": {"AffectedNodes": [_ripplestate(ALICE, BRIX_ISSUER, 13, 3)]},
}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_history_events.py
# <copy the env-guard preamble from tests/test_nft_listener.py verbatim here>
from lfg_core import history_events
from tests.fixtures import history_txs as fx


def _nft(tx):
    return history_events.derive_nft_events(tx, nft_issuer=fx.ISSUER)


def test_mint():
    (ev,) = _nft(fx.MINT)
    assert ev["event"] == "mint" and ev["nft_id"] == fx.NFT_A
    assert ev["to_addr"] == fx.ISSUER
    assert ev["ts"] == 800000000 + history_events.RIPPLE_EPOCH


def test_burn_records_owner():
    (ev,) = _nft(fx.BURN)
    assert ev["event"] == "burn" and ev["from_addr"] == fx.ALICE


def test_modify_is_swap():
    (ev,) = _nft(fx.MODIFY)
    assert ev["event"] == "modify" and ev["to_addr"] == fx.ALICE


def test_sale_xrp_seller_buyer_price():
    (ev,) = _nft(fx.SALE_XRP)
    assert ev["event"] == "sale"
    assert (ev["from_addr"], ev["to_addr"]) == (fx.ALICE, fx.BOB)
    assert ev["price_drops"] == 5000000 and ev["price_token"] is None


def test_zero_price_is_transfer():
    (ev,) = _nft(fx.TRANSFER_FREE)
    assert ev["event"] == "transfer"
    assert (ev["from_addr"], ev["to_addr"]) == (fx.ISSUER, fx.ALICE)


def test_buy_offer_iou_sale():
    (ev,) = _nft(fx.SALE_IOU)
    assert ev["event"] == "sale"
    assert (ev["from_addr"], ev["to_addr"]) == (fx.ALICE, fx.BOB)
    assert ev["price_drops"] is None and '"value": "10"' in ev["price_token"]


def test_offer_create_and_cancel():
    (c,) = _nft(fx.OFFER_CREATE)
    assert c["event"] == "offer_create" and c["price_drops"] == 9000000
    (x,) = _nft(fx.OFFER_CANCEL)
    assert x["event"] == "offer_cancel" and x["nft_id"] == fx.NFT_A


def test_non_nft_tx_yields_nothing():
    assert _nft(fx.AIRDROP) == []


def test_normalize_entry_account_tx_shape():
    entry = {"tx": {"TransactionType": "Payment", "Account": "rX", "date": 1},
             "meta": {"AffectedNodes": []}, "hash": "FF" * 32,
             "ledger_index": 42, "validated": True}
    tx = history_events.normalize_entry(entry)
    assert tx["hash"] == "FF" * 32 and tx["ledger_index"] == 42
    assert tx["meta"] == {"AffectedNodes": []}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_history_events.py -v`
Expected: FAIL — no module `history_events`.

- [ ] **Step 4: Implement NFT derivation in `lfg_core/history_events.py`**

```python
"""Pure derivation of NFT/BRIX events from normalized XRPL tx dicts.

A "normalized" tx has its fields at top level plus `meta` (metadata dict),
`hash`, `ledger_index` — the shape scripts/onchain_listener.py's
_normalize_stream_tx produces and normalize_entry() below reproduces for
account_tx / nft_history entries. All functions are pure and unit-testable."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from lfg_core import nft_listener

RIPPLE_EPOCH = 946684800

_LSF_SELL = 0x00000001  # lsfSellNFToken on NFTokenOffer


def normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Flatten one account_tx / nft_history response entry into a normalized
    tx dict (tx fields top-level, plus meta/hash/ledger_index)."""
    tx = dict(entry.get("tx") or entry.get("tx_json") or {})
    tx["meta"] = entry.get("meta") or entry.get("metaData") or {}
    tx.setdefault("hash", entry.get("hash"))
    tx.setdefault("ledger_index", entry.get("ledger_index"))
    if "close_time_iso" in entry:
        tx.setdefault("close_time_iso", entry["close_time_iso"])
    return tx


def tx_unix_time(tx: dict[str, Any]) -> int | None:
    date = tx.get("date")
    if isinstance(date, int):
        return date + RIPPLE_EPOCH
    iso = tx.get("close_time_iso")
    if isinstance(iso, str):
        try:
            return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _deleted_nft_offers(meta: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for node in meta.get("AffectedNodes", []):
        wrapper = node.get("DeletedNode") or {}
        if wrapper.get("LedgerEntryType") == "NFTokenOffer":
            out.append(wrapper.get("FinalFields") or {})
    return out


def _price_fields(amount: Any) -> tuple[int | None, str | None]:
    """XRPL Amount -> (price_drops, price_token JSON)."""
    if isinstance(amount, str):
        return int(amount), None
    if isinstance(amount, dict):
        return None, json.dumps(amount, sort_keys=True)
    return None, None


def derive_nft_events(tx: dict[str, Any], *, nft_issuer: str) -> list[dict[str, Any]]:
    ttype = str(tx.get("TransactionType", ""))
    meta = tx.get("meta") or {}
    ts = tx_unix_time(tx)
    base = {
        "tx_hash": tx.get("hash"),
        "nft_number": None,
        "price_drops": None,
        "price_token": None,
        "ledger_index": tx.get("ledger_index"),
        "ts": ts,
    }
    account = tx.get("Account")

    if ttype == "NFTokenMint":
        ids = nft_listener.affected_nft_ids(tx)
        return [
            {**base, "nft_id": i, "event": "mint", "from_addr": None,
             "to_addr": tx.get("Issuer") or account}
            for i in ids
        ]

    if ttype == "NFTokenBurn":
        nft_id = tx.get("NFTokenID")
        if not nft_id:
            return []
        return [{**base, "nft_id": nft_id, "event": "burn",
                 "from_addr": tx.get("Owner") or account, "to_addr": None}]

    if ttype == "NFTokenModify":
        nft_id = tx.get("NFTokenID")
        if not nft_id:
            return []
        return [{**base, "nft_id": nft_id, "event": "modify",
                 "from_addr": None, "to_addr": tx.get("Owner") or account}]

    if ttype == "NFTokenAcceptOffer":
        ids = nft_listener.affected_nft_ids(tx)
        offers = _deleted_nft_offers(meta)
        if not ids or not offers:
            return []
        # Prefer the sell offer for price/seller; fall back to the first.
        sell = next((o for o in offers if int(o.get("Flags") or 0) & _LSF_SELL), None)
        offer = sell or offers[0]
        drops, token = _price_fields(offer.get("Amount"))
        if sell is not None:
            seller, buyer = sell.get("Owner"), account
        else:  # buy offer accepted: offer owner is the buyer, accepter sells
            seller, buyer = account, offer.get("Owner")
        event = "transfer" if (drops == 0 and token is None) else "sale"
        out = {**base, "nft_id": ids[0], "event": event, "from_addr": seller,
               "to_addr": buyer, "price_token": token}
        if event == "sale" and drops:
            out["price_drops"] = drops
        return [out]

    if ttype == "NFTokenCreateOffer":
        nft_id = tx.get("NFTokenID")
        if not nft_id:
            return []
        drops, token = _price_fields(tx.get("Amount"))
        return [{**base, "nft_id": nft_id, "event": "offer_create",
                 "from_addr": account, "to_addr": tx.get("Destination"),
                 "price_drops": drops, "price_token": token}]

    if ttype == "NFTokenCancelOffer":
        return [
            {**base, "nft_id": o.get("NFTokenID"), "event": "offer_cancel",
             "from_addr": o.get("Owner"), "to_addr": None}
            for o in _deleted_nft_offers(meta)
            if o.get("NFTokenID")
        ]

    return []
```

- [ ] **Step 5: Run tests, then commit**

Run: `.venv/bin/python -m pytest tests/test_history_events.py -v` — Expected: PASS (10 tests).

```bash
git add lfg_core/history_events.py tests/fixtures/ tests/test_history_events.py
git commit -m "feat(history): derive NFT events from raw txs (mint/burn/sale/transfer/offer/modify)"
```

---

### Task 3: BRIX event derivation

**Files:**
- Modify: `lfg_core/history_events.py`
- Test: `tests/test_history_events.py` (append)

**Interfaces:**
- Produces: `derive_brix_events(tx: dict, *, brix_issuer: str, brix_hex: str, distributor: str | None = None) -> list[dict]` — one event per non-issuer account whose BRIX trustline balance changed, shaped for `history_store.insert_brix_event`.

Balance-delta rules (RippleState convention): `Balance.value` is from the **low** account's perspective. The holder is whichever of `LowLimit.issuer` / `HighLimit.issuer` is **not** `brix_issuer`. If the holder is the low account, holder balance = `+value`; if high, holder balance = `-value`. Delta = final − previous. Kind: `trustset` for TrustSet (delta 0 rows are still emitted only for TrustSet), `airdrop` if `tx.Account == distributor` and type Payment, `payment` for other Payments, `amm_deposit` / `amm_withdraw` for those types, `amm_swap` for anything else with deltas (OfferCreate, cross-currency Payment path).

- [ ] **Step 1: Append failing tests**

```python
def _brix(tx, distributor=None):
    return history_events.derive_brix_events(
        tx, brix_issuer=fx.BRIX_ISSUER, brix_hex=fx.BRIX_HEX, distributor=distributor)


def test_airdrop_deltas_and_kind():
    evs = _brix(fx.AIRDROP, distributor=fx.DISTRIBUTOR)
    by = {e["account"]: e for e in evs}
    assert by[fx.ALICE]["delta"] == 3.0 and by[fx.ALICE]["kind"] == "airdrop"
    assert by[fx.DISTRIBUTOR]["delta"] == -3.0
    assert by[fx.ALICE]["counterparty"] == fx.DISTRIBUTOR


def test_payment_without_distributor_is_payment():
    evs = _brix(fx.AIRDROP)
    assert all(e["kind"] == "payment" for e in evs)


def test_trustset_kind():
    evs = _brix(fx.TRUSTSET)
    assert evs == [] or all(e["kind"] == "trustset" for e in evs)


def test_amm_deposit_kind():
    evs = _brix(fx.AMM_DEPOSIT)
    assert len(evs) == 1 and evs[0]["kind"] == "amm_deposit"
    assert evs[0]["delta"] == -10.0


def test_non_brix_tx_no_events():
    assert _brix(fx.SALE_XRP) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_history_events.py -v` — new tests FAIL (`derive_brix_events` missing).

- [ ] **Step 3: Implement**

Append to `lfg_core/history_events.py`:

```python
def _is_brix(cur: Any, brix_hex: str) -> bool:
    return isinstance(cur, str) and cur.upper() in (brix_hex.upper(), "BRIX")


def _brix_deltas(meta: dict[str, Any], brix_issuer: str, brix_hex: str) -> dict[str, float]:
    """Per-holder BRIX balance change from RippleState node diffs."""
    deltas: dict[str, float] = {}
    for node in meta.get("AffectedNodes", []):
        wrapper = (
            node.get("ModifiedNode") or node.get("CreatedNode") or node.get("DeletedNode") or {}
        )
        if wrapper.get("LedgerEntryType") != "RippleState":
            continue
        final = wrapper.get("FinalFields") or wrapper.get("NewFields") or {}
        bal = final.get("Balance") or {}
        if not _is_brix(bal.get("currency"), brix_hex):
            continue
        low = (final.get("LowLimit") or {}).get("issuer")
        high = (final.get("HighLimit") or {}).get("issuer")
        if brix_issuer not in (low, high):
            continue
        holder = high if low == brix_issuer else low
        sign = 1.0 if holder == low else -1.0
        prev_bal = (wrapper.get("PreviousFields") or {}).get("Balance") or {}
        old = float(prev_bal.get("value") or 0.0)
        new = float(bal.get("value") or 0.0)
        if node.get("DeletedNode"):
            new = 0.0
        delta = sign * (new - old)
        if delta:
            deltas[holder] = deltas.get(holder, 0.0) + delta
    return deltas


def derive_brix_events(
    tx: dict[str, Any],
    *,
    brix_issuer: str,
    brix_hex: str,
    distributor: str | None = None,
) -> list[dict[str, Any]]:
    ttype = str(tx.get("TransactionType", ""))
    account = tx.get("Account")
    deltas = _brix_deltas(tx.get("meta") or {}, brix_issuer, brix_hex)
    if not deltas:
        return []
    if ttype == "TrustSet":
        kind = "trustset"
    elif ttype == "Payment":
        kind = "airdrop" if distributor and account == distributor else "payment"
    elif ttype == "AMMDeposit":
        kind = "amm_deposit"
    elif ttype == "AMMWithdraw":
        kind = "amm_withdraw"
    else:
        kind = "amm_swap"
    ts = tx_unix_time(tx)
    accounts = sorted(deltas)
    return [
        {
            "tx_hash": tx.get("hash"),
            "account": a,
            # counterparty: the other mover if exactly two, else the tx sender.
            "counterparty": (
                next((b for b in accounts if b != a), account)
                if len(accounts) == 2
                else account
            ),
            "delta": deltas[a],
            "kind": kind,
            "ts": ts,
        }
        for a in accounts
    ]
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_history_events.py -v` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/history_events.py tests/fixtures/history_txs.py tests/test_history_events.py
git commit -m "feat(history): derive BRIX balance events (payment/airdrop/amm/trustset)"
```

---

### Task 4: Backfill CLI — account_tx sources, resumable

**Files:**
- Create: `scripts/backfill_history.py`
- Test: `tests/test_backfill_history.py`

**Interfaces:**
- Consumes: `history_store` (Task 1), `history_events.normalize_entry` (Task 2).
- Produces (importable, network-free for tests):
  - `store_raw_tx(conn, tx: dict) -> bool` — insert one normalized tx into `xrpl_txs` (extracts hash/ledger_index/close_time/type/account/SourceTag; returns insert_tx's bool)
  - `async backfill_account_tx(conn, request_fn, account: str, source: str) -> int` — pages `account_tx` with `forward=True`, persists the marker to `backfill_state` after **every page**, returns count of new txs. `request_fn(req: dict) -> dict` is injected (real impl wraps `AsyncWebsocketClient.request`).
  - CLI: `python scripts/backfill_history.py --network testnet|mainnet [--distributor rXXX] [--sources issuer,brix,distributor,nfts] [--derive-only]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_backfill_history.py
# <copy the env-guard preamble from tests/test_nft_listener.py verbatim here>
import asyncio

from lfg_core import history_store
from tests.fixtures import history_txs as fx

import importlib
bh = importlib.import_module("scripts.backfill_history")


def _entry(tx, hash_, ledger=100):
    t = {k: v for k, v in tx.items() if k != "meta"}
    return {"tx": t, "meta": tx["meta"], "hash": hash_, "ledger_index": ledger,
            "validated": True}


def _fake_request_fn(pages):
    """pages: list of (entries, marker_or_None). Returns an async fn."""
    calls = []

    async def request_fn(req):
        calls.append(dict(req))
        entries, marker = pages[len(calls) - 1]
        out = {"transactions": entries}
        if marker is not None:
            out["marker"] = marker
        return out

    request_fn.calls = calls
    return request_fn


def test_store_raw_tx(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    from lfg_core import history_events
    tx = history_events.normalize_entry(_entry(fx.MINT, "AA" * 32))
    assert bh.store_raw_tx(conn, tx) is True
    assert bh.store_raw_tx(conn, tx) is False
    row = conn.execute("SELECT * FROM xrpl_txs").fetchone()
    assert row["tx_type"] == "NFTokenMint" and row["account"] == fx.ISSUER


def test_backfill_pages_and_resumes(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    fn = _fake_request_fn([
        ([_entry(fx.MINT, "01" * 32)], {"ledger": 5, "seq": 0}),
        ([_entry(fx.BURN, "02" * 32)], None),
    ])
    n = asyncio.run(bh.backfill_account_tx(conn, fn, fx.ISSUER, "issuer_tx"))
    assert n == 2
    assert fn.calls[0]["forward"] is True
    assert fn.calls[1]["marker"] == {"ledger": 5, "seq": 0}
    # cursor cleared once exhausted
    assert history_store.get_cursor(conn, "issuer_tx") is None

    # resume: a stored cursor is sent on the first request
    history_store.set_cursor(conn, "issuer_tx", '{"ledger": 9, "seq": 1}')
    fn2 = _fake_request_fn([([], None)])
    asyncio.run(bh.backfill_account_tx(conn, fn2, fx.ISSUER, "issuer_tx"))
    assert fn2.calls[0]["marker"] == {"ledger": 9, "seq": 1}


def test_backfill_marker_persisted_midway(tmp_path):
    """If a later page raises, the cursor from the last good page survives."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))

    async def request_fn(req):
        if req.get("marker"):
            raise RuntimeError("boom")
        return {"transactions": [_entry(fx.MINT, "03" * 32)], "marker": {"ledger": 7}}

    try:
        asyncio.run(bh.backfill_account_tx(conn, request_fn, fx.ISSUER, "issuer_tx"))
    except RuntimeError:
        pass
    assert history_store.get_cursor(conn, "issuer_tx") == '{"ledger": 7}'
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_backfill_history.py -v` — FAIL (module missing).

- [ ] **Step 3: Implement `scripts/backfill_history.py`**

```python
#!/usr/bin/env python3
"""One-time (resumable, idempotent) ledger-history backfill.

  python scripts/backfill_history.py --network mainnet
  python scripts/backfill_history.py --network mainnet --distributor rXXX
  python scripts/backfill_history.py --network mainnet --derive-only

Sources: account_tx over the NFT issuer, the BRIX issuer, and (if given) the
airdrop distributor; clio nft_history per nft_id known to onchain_<net>.db.
Pagination markers persist to backfill_state after every page, so Ctrl-C and
re-run is always safe. Derivation (Task 5) rebuilds nft_events/brix_events
from the raw rows."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from xrpl.asyncio.clients import AsyncWebsocketClient  # noqa: E402
from xrpl.models.requests import Request  # noqa: E402

from lfg_core import history_events, history_store  # noqa: E402

PAGE_LIMIT = 200


def store_raw_tx(conn: Any, tx: dict[str, Any]) -> bool:
    return history_store.insert_tx(
        conn,
        tx_hash=str(tx.get("hash")),
        ledger_index=tx.get("ledger_index"),
        close_time=history_events.tx_unix_time(tx),
        tx_type=str(tx.get("TransactionType", "")),
        account=tx.get("Account"),
        source_tag=tx.get("SourceTag"),
        raw_json=json.dumps(tx, sort_keys=True),
    )


async def backfill_account_tx(conn: Any, request_fn: Any, account: str, source: str) -> int:
    """Page account_tx forward, persisting the marker after every page."""
    stored = history_store.get_cursor(conn, source)
    marker: Any = json.loads(stored) if stored else None
    new = 0
    while True:
        req: dict[str, Any] = {
            "method": "account_tx",
            "account": account,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "limit": PAGE_LIMIT,
            "forward": True,
        }
        if marker:
            req["marker"] = marker
        result = await request_fn(req)
        for entry in result.get("transactions", []):
            if entry.get("validated") is False:
                continue
            tx = history_events.normalize_entry(entry)
            if store_raw_tx(conn, tx):
                new += 1
        marker = result.get("marker")
        history_store.set_cursor(conn, source, json.dumps(marker) if marker else None)
        if not marker:
            return new


async def backfill_nft_history(conn: Any, request_fn: Any, nft_id: str) -> int:
    """Full nft_history (clio) for one token; cursor keyed per nft_id."""
    source = f"nft_history:{nft_id}"
    if history_store.get_cursor(conn, source) == "done":
        return 0
    marker: Any = None
    new = 0
    while True:
        req: dict[str, Any] = {"method": "nft_history", "nft_id": nft_id, "limit": 100}
        if marker:
            req["marker"] = marker
        result = await request_fn(req)
        for entry in result.get("transactions", []):
            tx = history_events.normalize_entry(entry)
            if store_raw_tx(conn, tx):
                new += 1
        marker = result.get("marker")
        if not marker:
            history_store.set_cursor(conn, source, "done")
            return new


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import backfill_onchain as bf

    from lfg_core import config, nft_index

    parser = argparse.ArgumentParser(description="Ledger history backfill.")
    parser.add_argument("--network", choices=sorted(bf.NETWORKS), default=config.XRPL_NETWORK)
    parser.add_argument("--distributor", help="airdrop distributor wallet to scrape")
    parser.add_argument("--sources", default="issuer,brix,distributor,nfts")
    parser.add_argument("--derive-only", action="store_true")
    args = parser.parse_args()

    net = bf.NETWORKS[args.network]
    clio = net["clio"]
    issuer = net["issuer"] or config.SWAP_ISSUER_ADDRESS
    conn = history_store.init_history_db(history_store.history_db_path(args.network))

    if args.derive_only:
        from derive_history_events import rederive  # Task 5

        rederive(conn, args.network, distributor=args.distributor)
        return 0

    wanted = set(args.sources.split(","))
    async with AsyncWebsocketClient(clio) as client:

        async def request_fn(req: dict[str, Any]) -> dict[str, Any]:
            r = await client.request(Request.from_dict(req))
            if not r.is_successful():
                raise RuntimeError(f"{req['method']} failed: {r.result}")
            return r.result

        if "issuer" in wanted:
            n = await backfill_account_tx(conn, request_fn, issuer, "issuer_tx")
            logging.info(f"issuer_tx: +{n}")
        if "brix" in wanted:
            n = await backfill_account_tx(conn, request_fn, config.SWAP_OFFER_ISSUER, "brix_tx")
            logging.info(f"brix_tx: +{n}")
        if "distributor" in wanted and args.distributor:
            n = await backfill_account_tx(conn, request_fn, args.distributor, "distributor_tx")
            logging.info(f"distributor_tx: +{n}")
        if "nfts" in wanted:
            oconn = nft_index.init_db(nft_index.index_db_path(args.network))
            ids = [r[0] for r in oconn.execute("SELECT nft_id FROM onchain_nfts")]
            total = 0
            for i, nft_id in enumerate(ids, 1):
                total += await backfill_nft_history(conn, request_fn, nft_id)
                if i % 100 == 0:
                    logging.info(f"nft_history: {i}/{len(ids)} tokens, +{total} txs")
            logging.info(f"nft_history: done, +{total}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_backfill_history.py -v` — Expected: PASS. Also run `.venv/bin/python -m pytest tests/test_economy_scripts_import.py -v` to confirm no script-import regressions.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_history.py tests/test_backfill_history.py
git commit -m "feat(history): resumable account_tx + nft_history backfill CLI"
```

---

### Task 5: Event derivation pass (raw → events) + nft_number enrichment

**Files:**
- Create: `scripts/derive_history_events.py`
- Test: `tests/test_backfill_history.py` (append)

**Interfaces:**
- Consumes: `history_store`, `history_events`, `nft_index.index_db_path` / `onchain_nfts.nft_number`.
- Produces: `rederive(hconn, network: str, *, distributor: str | None = None, oconn=None) -> dict` — clears derived tables, walks every `xrpl_txs` row, applies both derivers, fills `nft_events.nft_number` from `onchain_nfts`, returns `{"nft_events": n, "brix_events": m}`. `oconn` injectable for tests (falls back to opening `index_db_path(network)`).

- [ ] **Step 1: Append failing test**

```python
def test_rederive_from_raw(tmp_path):
    import importlib
    dh = importlib.import_module("scripts.derive_history_events")
    from lfg_core import history_events
    import sqlite3

    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    for tx, h in ((fx.MINT, "01" * 32), (fx.SALE_XRP, "04" * 32), (fx.AIRDROP, "09" * 32)):
        bh.store_raw_tx(conn, history_events.normalize_entry(_entry(tx, h)))

    oconn = sqlite3.connect(":memory:")
    oconn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER)")
    oconn.execute("INSERT INTO onchain_nfts VALUES (?, 7)", (fx.NFT_A,))

    counts = dh.rederive(conn, "testnet", distributor=fx.DISTRIBUTOR, oconn=oconn,
                         nft_issuer=fx.ISSUER, brix_issuer=fx.BRIX_ISSUER)
    assert counts == {"nft_events": 2, "brix_events": 2}
    rows = conn.execute("SELECT event, nft_number FROM nft_events ORDER BY ts").fetchall()
    assert [(r["event"], r["nft_number"]) for r in rows] == [("mint", 7), ("sale", 7)]
    # idempotent
    counts2 = dh.rederive(conn, "testnet", distributor=fx.DISTRIBUTOR, oconn=oconn,
                          nft_issuer=fx.ISSUER, brix_issuer=fx.BRIX_ISSUER)
    assert counts2 == counts
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_backfill_history.py::test_rederive_from_raw -v` FAILS.

- [ ] **Step 3: Implement `scripts/derive_history_events.py`**

```python
#!/usr/bin/env python3
"""Rebuild nft_events / brix_events from the raw xrpl_txs archive.

  python scripts/derive_history_events.py --network mainnet [--distributor rXXX]

Derived tables are droppable: this clears and rebuilds them in one pass.
Also invoked by backfill_history.py --derive-only."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, history_events, history_store  # noqa: E402

BRIX_HEX = "4252495800000000000000000000000000000000"


def rederive(
    hconn: Any,
    network: str,
    *,
    distributor: str | None = None,
    oconn: Any = None,
    nft_issuer: str | None = None,
    brix_issuer: str | None = None,
) -> dict[str, int]:
    from lfg_core import nft_index

    nft_issuer = nft_issuer or config.SWAP_ISSUER_ADDRESS
    brix_issuer = brix_issuer or config.SWAP_OFFER_ISSUER
    if oconn is None:
        oconn = nft_index.init_db(nft_index.index_db_path(network))
    numbers = dict(oconn.execute("SELECT nft_id, nft_number FROM onchain_nfts"))

    history_store.clear_derived(hconn)
    n_nft = n_brix = 0
    for row in hconn.execute("SELECT raw_json FROM xrpl_txs ORDER BY ledger_index"):
        tx = json.loads(row["raw_json"])
        for ev in history_events.derive_nft_events(tx, nft_issuer=nft_issuer):
            ev["nft_number"] = numbers.get(ev["nft_id"])
            history_store.insert_nft_event(hconn, ev)
            n_nft += 1
        for ev in history_events.derive_brix_events(
            tx, brix_issuer=brix_issuer, brix_hex=BRIX_HEX, distributor=distributor
        ):
            history_store.insert_brix_event(hconn, ev)
            n_brix += 1
    hconn.commit()
    return {"nft_events": n_nft, "brix_events": n_brix}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Rebuild derived history events.")
    parser.add_argument("--network", default=config.XRPL_NETWORK)
    parser.add_argument("--distributor")
    args = parser.parse_args()
    hconn = history_store.init_history_db(history_store.history_db_path(args.network))
    counts = rederive(hconn, args.network, distributor=args.distributor)
    print(f"[{args.network}] derived: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: `derive_nft_events` emits events for ALL NFTs in the raw archive; raw rows only enter via issuer/BRIX/distributor/nft_history scrapes, so foreign-collection noise is limited to txs that also touched our accounts — filter later if it matters (YAGNI now). Fix the import in Task 4's `--derive-only` branch to `from derive_history_events import rederive` (scripts dir is on sys.path) and pass `distributor`.

- [ ] **Step 4: Run tests** — `pytest tests/test_backfill_history.py -v` PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/derive_history_events.py scripts/backfill_history.py tests/test_backfill_history.py
git commit -m "feat(history): derivation pass rebuilding events from raw archive"
```

---

### Task 6: Live listener extension

**Files:**
- Modify: `scripts/onchain_listener.py` (`process_stream_tx` signature + `_listen` + subscription)
- Test: `tests/test_onchain_listener.py` (append)

**Interfaces:**
- Consumes: `store_raw_tx` logic (re-implemented via `history_store` to avoid scripts↔scripts import in the listener: use `backfill_history.store_raw_tx` — scripts dir already on `sys.path` there).
- Produces: `process_stream_tx(conn, tx, *, fetch_token, fetch_meta, is_ours, history_conn=None, history_ctx=None)` — when `history_conn` is given, every stream tx that yields NFT or BRIX events (or is an NFToken tx passing the existing filter) is stored raw + events inserted incrementally. `history_ctx` is `{"nft_issuer":…, "brix_issuer":…, "brix_hex":…, "distributor":…, "numbers": dict}`.

- [ ] **Step 1: Append failing test to `tests/test_onchain_listener.py`** (mirror that file's existing stub style for `fetch_token`/`fetch_meta`/`is_ours` — read it first and reuse its helpers):

```python
def test_stream_tx_feeds_history(tmp_path):
    from lfg_core import history_store
    from tests.fixtures import history_txs as fx

    hconn = history_store.init_history_db(str(tmp_path / "h.db"))
    conn = _index_conn()  # reuse the file's existing in-memory index helper
    ctx = {"nft_issuer": fx.ISSUER, "brix_issuer": fx.BRIX_ISSUER,
           "brix_hex": fx.BRIX_HEX, "distributor": None, "numbers": {}}
    tx = dict(fx.AIRDROP)  # BRIX-only tx: index apply is a no-op, history isn't
    asyncio.run(ol.process_stream_tx(
        conn, tx, fetch_token=_none_token, fetch_meta=_none_meta,
        is_ours=lambda t: False, history_conn=hconn, history_ctx=ctx))
    assert hconn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0] == 1
    assert hconn.execute("SELECT COUNT(*) FROM brix_events").fetchone()[0] == 2
```

(Adapt `_index_conn`/`_none_token`/`_none_meta` to whatever helpers `tests/test_onchain_listener.py` actually defines — read the file before writing; if no equivalent exists, define tiny local stubs in the test.)

- [ ] **Step 2: Run to verify failure** — new test FAILS (`process_stream_tx` has no `history_conn` param).

- [ ] **Step 3: Implement.** In `scripts/onchain_listener.py`:

Add imports: `import json as _json`, `from lfg_core import history_events, history_store`.

Extend `process_stream_tx` (after the existing economy block):

```python
async def process_stream_tx(
    conn: Any,
    tx: dict[str, Any],
    *,
    fetch_token: nft_listener.FetchTokenFn,
    fetch_meta: nft_listener.FetchMetaFn,
    is_ours: Callable[[dict[str, Any]], bool],
    history_conn: Any = None,
    history_ctx: dict[str, Any] | None = None,
) -> None:
    ...existing body unchanged...
    if history_conn is not None and history_ctx is not None:
        _record_history(history_conn, tx, history_ctx)


def _record_history(hconn: Any, tx: dict[str, Any], ctx: dict[str, Any]) -> None:
    """Append one stream tx to the history archive iff it produces events."""
    nft_evs = history_events.derive_nft_events(tx, nft_issuer=ctx["nft_issuer"])
    brix_evs = history_events.derive_brix_events(
        tx, brix_issuer=ctx["brix_issuer"], brix_hex=ctx["brix_hex"],
        distributor=ctx.get("distributor"),
    )
    if not nft_evs and not brix_evs:
        return
    if not tx.get("hash"):
        return
    history_store.insert_tx(
        hconn,
        tx_hash=str(tx["hash"]),
        ledger_index=tx.get("ledger_index"),
        close_time=history_events.tx_unix_time(tx),
        tx_type=str(tx.get("TransactionType", "")),
        account=tx.get("Account"),
        source_tag=tx.get("SourceTag"),
        raw_json=_json.dumps(tx, sort_keys=True),
    )
    for ev in nft_evs:
        ev["nft_number"] = ctx["numbers"].get(ev["nft_id"])
        history_store.insert_nft_event(hconn, ev)
    for ev in brix_evs:
        history_store.insert_brix_event(hconn, ev)
    hconn.commit()
```

In `_listen`: open `hconn = history_store.init_history_db(history_store.history_db_path(network))`; build `history_ctx` once (numbers = `dict(conn.execute("SELECT nft_id, nft_number FROM onchain_nfts"))`, refreshed by re-reading on each mint — simplest: `ctx["numbers"] = dict(...)` re-read inside `_record_history` is too hot; refresh when a mint event has no number: acceptable to leave None, the nightly `--derive-only` rerun fills it). `distributor` from env `BRIX_DISTRIBUTOR_ADDRESS` (add to `lfg_core/config.py`: `BRIX_DISTRIBUTOR_ADDRESS = os.getenv("BRIX_DISTRIBUTOR_ADDRESS")`). Pass `history_conn=hconn, history_ctx=ctx` in the `process_stream_tx` call.

Also update the stream message normalization: `_normalize_stream_tx` must carry `hash`, `ledger_index`, and `close_time_iso` from the envelope — extend it:

```python
    tx = dict(msg.get("tx_json") or msg.get("transaction") or {})
    tx["meta"] = msg.get("meta") or msg.get("metaData") or {}
    tx.setdefault("hash", msg.get("hash"))
    tx.setdefault("ledger_index", msg.get("ledger_index"))
    if "close_time_iso" in msg:
        tx.setdefault("close_time_iso", msg["close_time_iso"])
    return tx
```

The existing early `Issuer` filter in `_listen` must NOT skip BRIX txs: it only `continue`s on foreign `NFTokenMint`, which is fine — leave as is.

- [ ] **Step 4: Run tests** — `.venv/bin/python -m pytest tests/test_onchain_listener.py -v` PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add scripts/onchain_listener.py lfg_core/config.py tests/test_onchain_listener.py
git commit -m "feat(history): listener appends stream txs to history archive"
```

---

### Task 7: Balance snapshots CLI

**Files:**
- Create: `scripts/snapshot_balances.py`
- Test: `tests/test_snapshot_balances.py`

**Interfaces:**
- Produces:
  - `async collect_balances(request_fn, brix_issuer: str, amm_account: str | None) -> dict[str, dict]` — pages `account_lines` on the BRIX issuer (`{"method": "account_lines", "account": brix_issuer, "limit": 400, "marker": …}`; each line: `{"account": holder, "balance": "-12.5", …}` — issuer-side balances are negated, so holder BRIX = `-float(balance)`), and if `amm_account` is set, calls `{"method": "amm_info", "amm_account": amm_account}` → `result["amm"]["lp_token"]` for total, then pages `account_lines` on `amm_account` for per-holder LP balances. Returns `{holder: {"brix": x, "lp": y}}`.
  - `write_snapshot(hconn, balances: dict, snap_date: str) -> int`
  - CLI: `python scripts/snapshot_balances.py --network mainnet [--amm-account rXXX] [--date YYYY-MM-DD]` — AMM account from env `BRIX_AMM_ACCOUNT` (add to config: `BRIX_AMM_ACCOUNT = os.getenv("BRIX_AMM_ACCOUNT")`; testnet pool `rLUnD5mskBnHfwFxCjakDA3RVgK584XQXG`, mainnet from memory `rn6TaseGA12G2…` — user confirms exact address at deploy).

- [ ] **Step 1: Write failing tests** — fake `request_fn` returning one `account_lines` page for the issuer (`[{"account": "rA", "balance": "-10"}]`) and one for the AMM (`[{"account": "rA", "balance": "-2.5"}]`); assert `collect_balances` returns `{"rA": {"brix": 10.0, "lp": 2.5}}`; assert `write_snapshot` inserts rows readable via `balance_snapshots` and re-running same date overwrites (count stable).

```python
# tests/test_snapshot_balances.py
# <env-guard preamble>
import asyncio
import importlib

from lfg_core import history_store

sb = importlib.import_module("scripts.snapshot_balances")


def test_collect_and_write(tmp_path):
    async def request_fn(req):
        if req["method"] == "account_lines" and req["account"] == "rBrix":
            return {"lines": [{"account": "rA", "balance": "-10"}]}
        if req["method"] == "account_lines" and req["account"] == "rAmm":
            return {"lines": [{"account": "rA", "balance": "-2.5"}]}
        raise AssertionError(req)

    bal = asyncio.run(sb.collect_balances(request_fn, "rBrix", "rAmm"))
    assert bal == {"rA": {"brix": 10.0, "lp": 2.5}}

    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    assert sb.write_snapshot(conn, bal, "2026-07-04") == 1
    assert sb.write_snapshot(conn, bal, "2026-07-04") == 1
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — same script skeleton as Task 4 (argparse, `AsyncWebsocketClient(clio)`, injected `request_fn`); `collect_balances` pages with `marker` like `backfill_account_tx`; skip non-positive holder balances of exactly 0; `write_snapshot` loops `history_store.upsert_snapshot` and returns row count. CLI prints `[network] snapshot 2026-07-04: N holders`.

```python
async def collect_balances(request_fn, brix_issuer, amm_account):
    async def lines(account):
        out, marker = [], None
        while True:
            req = {"method": "account_lines", "account": account, "limit": 400}
            if marker:
                req["marker"] = marker
            r = await request_fn(req)
            out.extend(r.get("lines", []))
            marker = r.get("marker")
            if not marker:
                return out

    balances: dict[str, dict] = {}
    for line in await lines(brix_issuer):
        v = -float(line.get("balance") or 0)
        if v:
            balances.setdefault(line["account"], {"brix": 0.0, "lp": 0.0})["brix"] = v
    if amm_account:
        for line in await lines(amm_account):
            v = -float(line.get("balance") or 0)
            if v:
                balances.setdefault(line["account"], {"brix": 0.0, "lp": 0.0})["lp"] = v
    return balances


def write_snapshot(hconn, balances, snap_date):
    for account, b in balances.items():
        history_store.upsert_snapshot(hconn, snap_date, account, b["brix"], b["lp"])
    return len(balances)
```

`snap_date` default in CLI: `datetime.now(timezone.utc).strftime("%Y-%m-%d")`. Exclude the AMM account itself and the distributor from the `brix` map keys? No — leave raw; leaderboard excludes known system accounts (Task 8).

- [ ] **Step 4: Run tests** — PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/snapshot_balances.py tests/test_snapshot_balances.py lfg_core/config.py
git commit -m "feat(history): nightly BRIX + AMM LP balance snapshot CLI"
```

---

### Task 8: Leaderboard queries — periods + user/NFT boards

**Files:**
- Create: `lfg_core/leaderboard.py`
- Test: `tests/test_leaderboard.py`

**Interfaces:**
- Produces:
  - `period_bounds(period: str, start: str | None, *, now: int) -> tuple[int, int]` — `(start_ts, end_ts)` unix UTC; `period ∈ {today, week, month, year, all}`; `start` ISO date anchors a specific past period; `all` → `(0, now)`; week starts Monday.
  - `BOARDS: dict[str, callable]` registry; `compute(board: str, hconn, oconn, *, start_ts: int, end_ts: int, network: str, system_accounts: frozenset[str]) -> list[dict]` — each row `{"wallet": str|None, "nft_id": str|None, "nft_number": int|None, "value": float}` sorted desc, top 25.
  - Boards this task: `users_nfts`, `users_swaps`, `users_builds`, `nft_swaps`.
  - `system_accounts` = `{nft_issuer, brix_issuer, distributor, amm_account}` minus Nones — always excluded from user boards.

Board SQL (exact):
- `users_nfts` all-time (`start_ts == 0`): `SELECT owner, COUNT(*) FROM onchain_nfts WHERE is_burned=0 AND owner IS NOT NULL GROUP BY owner` (oconn). Windowed: net acquisitions from nft_events — `to_addr` of `mint|transfer|sale` = +1, `from_addr` of `transfer|sale|burn` = −1, `ts` in window, positive totals only.
- `users_swaps`: `SELECT to_addr, COUNT(*) FROM nft_events WHERE event='modify' AND ts>=? AND ts<? GROUP BY to_addr`.
- `users_builds`: issuer→user deliveries of rebirth tokens:

```sql
SELECT t.to_addr AS wallet, COUNT(*) AS value
FROM nft_events t
JOIN nft_events m ON m.nft_id = t.nft_id AND m.event = 'mint'
WHERE t.event IN ('transfer','sale') AND t.from_addr = :issuer
  AND t.ts >= :start AND t.ts < :end
  AND EXISTS (SELECT 1 FROM nft_events b
              WHERE b.event='burn' AND b.nft_number = t.nft_number
                AND b.nft_id != t.nft_id AND b.ts < m.ts)
GROUP BY t.to_addr ORDER BY value DESC LIMIT 25
```

(v1 caveat, documented in the module docstring: also counts admin re-offers of reminted legacy editions.)
- `nft_swaps`: `SELECT nft_id, nft_number, COUNT(*) FROM nft_events WHERE event='modify' AND ts>=? AND ts<? GROUP BY nft_id ORDER BY 3 DESC LIMIT 25`.

- [ ] **Step 1: Write failing tests** — build an in-memory history DB (via `history_store.init_history_db(":memory:")`) + a stub `onchain_nfts` table; insert hand-rolled `nft_events` rows (3 wallets, mixed events/timestamps); assert: period math (`today` bounds; `week` Monday-anchored; explicit `start=2026-01-01&period=month` → Jan 1–Feb 1), all-time `users_nfts` reads oconn, windowed `users_nfts` nets +/-, `users_swaps` counts modify by `to_addr`, `users_builds` counts only rebirths (seed: edition 7 burn at t=10, second-token mint t=20, issuer transfer to Alice t=30 → 1 build for Alice; plus a non-rebirth issuer transfer that must NOT count), `nft_swaps` groups by token, system accounts excluded everywhere.

```python
# tests/test_leaderboard.py
# <env-guard preamble>
import sqlite3
from datetime import datetime, timezone

from lfg_core import history_store, leaderboard

ISSUER = "rIssuer"
SYS = frozenset({ISSUER})


def _dbs():
    h = history_store.init_history_db(":memory:")
    o = sqlite3.connect(":memory:")
    o.row_factory = sqlite3.Row
    o.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INT,"
              " owner TEXT, is_burned INT DEFAULT 0, attributes_json TEXT, image TEXT)")
    return h, o


def _ev(h, **kw):
    base = dict(tx_hash=kw.get("tx_hash", str(id(kw))), nft_id="N1", nft_number=1,
                event="mint", from_addr=None, to_addr=None, price_drops=None,
                price_token=None, ledger_index=1, ts=0)
    base.update(kw)
    history_store.insert_nft_event(h, base)
    h.commit()


def test_period_bounds_today_and_anchored_month():
    now = int(datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc).timestamp())
    s, e = leaderboard.period_bounds("today", None, now=now)
    assert s == int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp()) and e == now
    s, e = leaderboard.period_bounds("month", "2026-01-01", now=now)
    assert (s, e) == (int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
                      int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()))
    s, e = leaderboard.period_bounds("week", "2026-06-30", now=now)  # Tue -> Mon 06-29
    assert s == int(datetime(2026, 6, 29, tzinfo=timezone.utc).timestamp())
    assert leaderboard.period_bounds("all", None, now=now) == (0, now)


def test_users_nfts_alltime_and_windowed():
    h, o = _dbs()
    o.executemany("INSERT INTO onchain_nfts (nft_id, nft_number, owner) VALUES (?,?,?)",
                  [("N1", 1, "rA"), ("N2", 2, "rA"), ("N3", 3, "rB"), ("N4", 4, ISSUER)])
    rows = leaderboard.compute("users_nfts", h, o, start_ts=0, end_ts=99,
                               network="testnet", system_accounts=SYS)
    assert [(r["wallet"], r["value"]) for r in rows] == [("rA", 2), ("rB", 1)]
    _ev(h, tx_hash="t1", event="sale", from_addr="rB", to_addr="rA", ts=50)
    rows = leaderboard.compute("users_nfts", h, o, start_ts=40, end_ts=60,
                               network="testnet", system_accounts=SYS)
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_users_swaps_and_nft_swaps():
    h, o = _dbs()
    for i, w in enumerate(["rA", "rA", "rB"]):
        _ev(h, tx_hash=f"m{i}", event="modify", to_addr=w, nft_id="N1", ts=5)
    rows = leaderboard.compute("users_swaps", h, o, start_ts=0, end_ts=10,
                               network="testnet", system_accounts=SYS)
    assert rows[0] == {"wallet": "rA", "nft_id": None, "nft_number": None, "value": 2}
    rows = leaderboard.compute("nft_swaps", h, o, start_ts=0, end_ts=10,
                               network="testnet", system_accounts=SYS)
    assert rows[0]["nft_id"] == "N1" and rows[0]["value"] == 3


def test_users_builds_counts_only_rebirths():
    h, o = _dbs()
    # edition 7: first token burned, second minted later, delivered to rA
    _ev(h, tx_hash="b", event="burn", nft_id="OLD", nft_number=7, from_addr="rX", ts=10)
    _ev(h, tx_hash="m", event="mint", nft_id="NEW", nft_number=7, to_addr=ISSUER, ts=20)
    _ev(h, tx_hash="d", event="transfer", nft_id="NEW", nft_number=7,
        from_addr=ISSUER, to_addr="rA", ts=30)
    # non-rebirth issuer transfer must not count
    _ev(h, tx_hash="m2", event="mint", nft_id="N9", nft_number=9, to_addr=ISSUER, ts=20)
    _ev(h, tx_hash="d2", event="transfer", nft_id="N9", nft_number=9,
        from_addr=ISSUER, to_addr="rB", ts=30)
    rows = leaderboard.compute("users_builds", h, o, start_ts=0, end_ts=99,
                               network="testnet", system_accounts=SYS)
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `lfg_core/leaderboard.py`** — module docstring explaining boards + the builds caveat; `period_bounds` with `datetime`/`calendar` (month arithmetic: next month = `(y + m // 12, m % 12 + 1)`); board functions taking `(hconn, oconn, start_ts, end_ts, issuer_excl)` returning normalized row dicts; `compute()` dispatching via `BOARDS`, applying system-account exclusion (`wallet NOT IN`) and `LIMIT 25`. All-time `users_nfts` switches on `start_ts == 0`. Keep every query parameterized.

- [ ] **Step 4: Run tests** — PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/leaderboard.py tests/test_leaderboard.py
git commit -m "feat(leaderboard): period math + user/NFT boards over history events"
```

---

### Task 9: Leaderboard queries — BRIX boards + rarity

**Files:**
- Modify: `lfg_core/leaderboard.py`
- Test: `tests/test_leaderboard.py` (append)

**Interfaces:**
- Produces boards: `brix_rich`, `brix_lp`, `brix_earned`, `nft_rarity` (registered in `BOARDS`).

Rules:
- `brix_rich` / `brix_lp`: all-time (`start_ts == 0`) → latest snapshot (`SELECT account, brix FROM balance_snapshots WHERE snap_date = (SELECT MAX(snap_date) FROM balance_snapshots)`); windowed → delta between the latest snapshot ≤ window end and the latest snapshot ≤ window start (missing start snapshot → treat as 0, i.e. growth since first sighting). Positive deltas only for windowed.
- `brix_earned`: `SELECT account, SUM(delta) FROM brix_events WHERE delta > 0 AND (kind IN ('airdrop','claim') OR (kind='payment' AND counterparty IN (:brix_issuer, :nft_issuer))) AND ts window GROUP BY account`. Pass issuers via `system_accounts` — earned = received *from* system accounts.
- `nft_rarity`: statistical rarity over live census in oconn: parse `attributes_json` (list of `{trait_type, value}`), per (slot, value) frequency; token score = Σ over its traits of `N_live / freq(slot, value)`; top 25 by score. Period-independent — ignore the window. Cache inside the call is unnecessary (API layer caches).

- [ ] **Step 1: Append failing tests** — snapshots: two dates (2026-07-01: rA 10 / 2026-07-03: rA 25, rB 5); all-time `brix_rich` = [(rA,25),(rB,5)]; windowed [07-02..07-04] = [(rA,15),(rB,5)]. `brix_earned`: seed brix_events (airdrop +3 to rA, payment +5 to rA from rB (non-system — excluded), payment +2 to rB from brix issuer) → rA 3, rB 2. `nft_rarity`: 3 live tokens, one with a unique trait value scores highest; burned token excluded.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Snapshot dates map to ts via `datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()`. `brix_earned` needs the issuer set: extend `compute()` signature with `earn_sources: frozenset[str] | None = None`, defaulting to `system_accounts`. Rarity: 

```python
def _nft_rarity(hconn, oconn, start_ts, end_ts, system_accounts, limit=25):
    import json as _j
    from collections import Counter

    rows = oconn.execute(
        "SELECT nft_id, nft_number, attributes_json FROM onchain_nfts"
        " WHERE is_burned=0 AND attributes_json IS NOT NULL AND attributes_json != ''"
    ).fetchall()
    token_traits: dict[str, tuple[int | None, list[tuple[str, str]]]] = {}
    freq: Counter[tuple[str, str]] = Counter()
    for r in rows:
        try:
            attrs = _j.loads(r["attributes_json"])
        except ValueError:
            continue
        pairs = [
            (str(t.get("trait_type")), str(t.get("value")))
            for t in attrs
            if isinstance(t, dict) and t.get("trait_type") is not None
        ]
        token_traits[r["nft_id"]] = (r["nft_number"], pairs)
        freq.update(pairs)
    n_live = len(token_traits) or 1
    scored = [
        {"wallet": None, "nft_id": nft_id, "nft_number": number,
         "value": round(sum(n_live / freq[p] for p in pairs), 2)}
        for nft_id, (number, pairs) in token_traits.items()
        if pairs
    ]
    scored.sort(key=lambda x: x["value"], reverse=True)
    return scored[:limit]
```

- [ ] **Step 4: Run tests** — full `tests/test_leaderboard.py` PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/leaderboard.py tests/test_leaderboard.py
git commit -m "feat(leaderboard): BRIX richlist/LP/earned boards + statistical rarity"
```

---

### Task 10: API endpoint

**Files:**
- Modify: `lfg_service/app.py`
- Test: `tests/test_leaderboard_api.py`

**Interfaces:**
- Produces: `GET /api/leaderboard?board=users_nfts&period=week&start=2026-06-29&me=rWALLET` → 200 JSON:

```json
{"board": "users_nfts", "period": "week", "start_ts": 0, "end_ts": 0,
 "rows": [{"rank": 1, "wallet": "rA", "display_name": "rA…", "nft_id": null,
            "nft_number": null, "image": null, "value": 2}],
 "me": {"rank": 4, "value": 1}}
```

- Public (no `require_wallet`); unknown board / bad period → 400. 60-second module-level cache keyed `(network, board, period, start)`; `me` computed post-cache from the cached full rows (cache stores up to rank 500: run queries with LIMIT 500 and slice top 25 for `rows` — adjust `compute()` to accept `limit: int = 25`).
- Display name: query the identities DB (`lfg_service.identity` — reuse its sqlite path/connection helpers; `SELECT handle FROM identities WHERE wallet=? ORDER BY linked_at DESC LIMIT 1` — read `identity.py` first and reuse an existing accessor if one fits, else add `identity.handle_for_wallet(wallet) -> str | None`). Fallback: `wallet[:6] + "…" + wallet[-4:]`.
- Image for NFT boards: `onchain_nfts.image` for the row's `nft_id`.
- DB connections: open lazily at first request via `history_store.history_db_path(config.XRPL_NETWORK)` / `nft_index.index_db_path(...)`, store on `request.app` under keys `"history_db"` / `"onchain_db"` (aiohttp app-level, like existing state usage — read how `app[...]` is used in `lfg_service/app.py` and follow it).

- [ ] **Step 1: Write failing tests** — use the same test style as `webapp/test_smoke.py` (`make_mocked_request` + monkeypatched app state): seed temp history/onchain DBs, monkeypatch env `HISTORY_DB_PATH`/`ONCHAIN_DB_PATH` to them, call `handle_leaderboard`; assert 200 + ranked rows, 400 on `board=nope`, 400 on `period=fortnight`, `me` rank present when `me=` given, second call served from cache (monkeypatch `leaderboard.compute` to raise after first call — cached response still returned).

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement `handle_leaderboard` + route.** Route: `app.router.add_get("/api/leaderboard", handle_leaderboard)` next to `/api/nfts`. Cache: `_LB_CACHE: dict[tuple, tuple[float, dict]] = {}`; TTL via `time.monotonic()`. `system_accounts` built from `config.SWAP_ISSUER_ADDRESS`, `config.SWAP_OFFER_ISSUER`, `config.BRIX_DISTRIBUTOR_ADDRESS`, `config.BRIX_AMM_ACCOUNT`.

- [ ] **Step 4: Run tests** — `.venv/bin/python -m pytest tests/test_leaderboard_api.py webapp/test_smoke.py -v` PASS (smoke suite guards against route/regression fallout).

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_leaderboard_api.py
git commit -m "feat(leaderboard): public /api/leaderboard endpoint with 60s cache"
```

---

### Task 11: Activity UI card

**Files:**
- Modify: `webapp/client/index.html` (leaderboard card inside `#mint-panel`, after `.actions`), `webapp/client/app.js`, `webapp/client/style.css`
- Test: `tests/test_app_js_boot.py` (append a smoke assertion; read the file first — it drives the client in a JS-less DOM harness, follow its existing pattern)

**Interfaces:**
- Consumes: `GET /api/leaderboard` (Task 10 response shape).

HTML (append inside `#mint-panel`, after the `.actions` div):

```html
<div id="leaderboard" class="leaderboard">
  <h3 class="lb-title">🏆 Leaderboard</h3>
  <div id="lb-periods" class="lb-chips" role="tablist" aria-label="Time period">
    <button class="lb-chip active" data-period="today">Today</button>
    <button class="lb-chip" data-period="week">Week</button>
    <button class="lb-chip" data-period="month">Month</button>
    <button class="lb-chip" data-period="year">Year</button>
    <button class="lb-chip" data-period="all">All Time</button>
  </div>
  <div id="lb-stepper" class="lb-stepper" hidden>
    <button id="lb-prev" class="link">‹</button>
    <span id="lb-range"></span>
    <button id="lb-next" class="link" disabled>›</button>
  </div>
  <div id="lb-boards" class="lb-chips lb-boards">
    <button class="lb-chip active" data-board="users_nfts">Holders</button>
    <button class="lb-chip" data-board="users_swaps">Swappers</button>
    <button class="lb-chip" data-board="users_builds">Builders</button>
    <button class="lb-chip" data-board="nft_swaps">Hot NFTs</button>
    <button class="lb-chip" data-board="nft_rarity">Rarest</button>
    <button class="lb-chip" data-board="brix_rich">BRIX Rich</button>
    <button class="lb-chip" data-board="brix_lp">LP</button>
    <button class="lb-chip" data-board="brix_earned">Earned</button>
  </div>
  <ol id="lb-list" class="lb-list"></ol>
  <p id="lb-me" class="lb-me" hidden></p>
  <p id="lb-empty" class="card-sub" hidden>Nothing here yet for this period.</p>
</div>
```

JS behavior (new `app.js` section, ~120 lines): state `{period: 'week', board: 'users_nfts', anchor: null}`; `anchor` = ISO date for stepped periods (null = current). Stepper visible only for week/month/year; ‹ moves anchor back one period, › forward (disabled when at current). `loadLeaderboard()` fetches `api('/api/leaderboard?…&me=' + encodeURIComponent(wallet || ''))`, renders rows: rank medal (🥇🥈🥉 then `#n`), `display_name` (or `#nft_number` + thumbnail via existing `imgUrl(image)` helper for NFT boards), value formatted (`Intl.NumberFormat`), caller row from `me` when present. Call `loadLeaderboard()` from `showMintHome()`; re-fetch on chip clicks (event delegation on the two chip rows). Errors → `lb-empty` text swapped to "Leaderboard unavailable." — never block the mint UI.

CSS: `.lb-chips` horizontal scroll row (`display:flex; gap:.4rem; overflow-x:auto`), `.lb-chip` pill buttons with `.active` state using existing accent variables, `.lb-list` rows `display:flex; justify-content:space-between`, thumbnails `2rem` rounded. Follow the existing card/button styles in `style.css` — reuse its custom properties, don't invent new colors.

- [ ] **Step 1: Read `tests/test_app_js_boot.py`, then append a test** asserting the leaderboard markup exists and app.js references `/api/leaderboard` (pattern-match how that file asserts on client source; typically string-presence checks):

```python
def test_leaderboard_card_present():
    html = (CLIENT / "index.html").read_text()
    assert 'id="leaderboard"' in html and 'data-board="brix_rich"' in html

def test_app_js_wires_leaderboard():
    js = (CLIENT / "app.js").read_text()
    assert "/api/leaderboard" in js and "loadLeaderboard" in js
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement HTML + JS + CSS as specified.** Bump the style.css cache-buster query (`style.css?v=11`) in index.html.

- [ ] **Step 4: Run tests + manual check.** `.venv/bin/python -m pytest tests/test_app_js_boot.py webapp/ -v` PASS. Then `WEBAPP_DEV_MODE=1` local harness: start the service, open the Activity page, confirm the card renders, chips switch, stepper appears for Week (mock data acceptable if the dev harness has no history DB — verify graceful "Nothing here yet").

- [ ] **Step 5: Commit**

```bash
git add webapp/client/index.html webapp/client/app.js webapp/client/style.css tests/test_app_js_boot.py
git commit -m "feat(leaderboard): Activity home leaderboard card (periods, boards, me-row)"
```

---

### Task 12: Conservation cross-check, docs, PR

**Files:**
- Modify: `scripts/audit_collection_integrity.py` (or create `scripts/audit_history.py` if the existing auditor's structure doesn't fit — read it first), `CLAUDE.md`
- Test: `tests/test_backfill_history.py` (append)

**Interfaces:**
- Produces: `audit_history(hconn, oconn) -> dict` — `{"mints": int, "burns": int, "live_events": mints-burns, "live_index": count(onchain is_burned=0), "drift": live_events - live_index}`; CLI flag/entry printing PASS/FAIL (nonzero drift = FAIL exit 1).

- [ ] **Step 1: Append failing test** — seed history events (3 mints, 1 burn) + onchain stub (2 live) → drift 0 passes; remove one onchain row → drift 1.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** in `scripts/audit_history.py` (standalone, same skeleton as other scripts; count DISTINCT nft_id for mints/burns to tolerate re-derivation overlap).

- [ ] **Step 4: Docs.** Add a `### Ledger history + leaderboards` section to `CLAUDE.md` under the on-chain index section, covering: `history_<net>.db` files (gitignored, regenerable), the four backfill sources + `--distributor`, `derive_history_events.py` rebuild, listener now dual-writes, `snapshot_balances.py` (cron: `pm2 start scripts/snapshot_balances.py --name lfg-snapshot --cron "10 0 * * *" --no-autorestart --interpreter .venv/bin/python -- --network mainnet`), `/api/leaderboard` params, new env vars `BRIX_DISTRIBUTOR_ADDRESS` / `BRIX_AMM_ACCOUNT` (+ add both to the `.env` example block).

- [ ] **Step 5: Full suite, push, draft PR.**

Run: `ECONOMY_ENABLED=1 .venv/bin/python -m pytest` — all green.

```bash
git add scripts/audit_history.py tests/test_backfill_history.py CLAUDE.md
git commit -m "feat(history): mint/burn conservation audit + docs"
ECONOMY_ENABLED=1 git push -u origin feat/leaderboard-history-db
gh pr create --draft --repo Team-Hamsa/LFG --title "feat: ledger history database + Activity leaderboard" --body "..."
```

PR body: link the spec + this plan, note the ops follow-ups.

---

## Post-merge ops (not plan tasks — run with the user)

1. Kick off testnet backfill: `.venv/bin/python scripts/backfill_history.py --network testnet` → then mainnet (hours; resumable).
2. Identify the airdrop distributor wallet from `brix_events` (`SELECT account, COUNT(*) FROM brix_events WHERE delta < 0 GROUP BY account ORDER BY 2 DESC` on the raw brix scrape), confirm with the user, set `BRIX_DISTRIBUTOR_ADDRESS`, rerun `--derive-only --distributor rXXX`.
3. Set `BRIX_AMM_ACCOUNT` (mainnet pool account), run first `snapshot_balances.py`, install the pm2 cron.
4. `pm2 restart lfg-index-testnet lfg-index-mainnet lfg-activity` to pick up the listener + API.
5. Run `scripts/audit_history.py --network mainnet` — investigate drift before announcing.
