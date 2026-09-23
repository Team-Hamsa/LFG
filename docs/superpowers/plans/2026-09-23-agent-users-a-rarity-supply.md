# Agent users (A of 3): `GET /api/rarity/supply` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public, cached endpoint that returns how many live collection tokens carry each trait value. The counts come from the same single read the `nft_rarity` board scores.

**Architecture:** A new `leaderboard.live_trait_table(oconn)` helper runs one `SELECT` and returns the per-token pairs plus a `Counter` of pairs. The `nft_rarity` board and the new `handle_rarity_supply` both use it. The handler follows `handle_rarity_odds`: work runs on an executor thread with its own short-lived index connection, and results are cached 60 s per network.

**Tech Stack:** Python 3.10, aiohttp, sqlite3, pytest (`scripts/run-tests`).

**Spec:** `docs/superpowers/specs/2026-09-22-agent-users-design.md` §3. This PR is A of the three the operator chose on 2026-09-23. B covers RegularKey proofs and revocation, C covers the `agent` provider and memos. Each ships as its own PR.

## Global Constraints

- The endpoint is public and unauthenticated, cached 60 s per network, and reads the network from `config.XRPL_NETWORK`.
- Response shape: `{"network", "as_of", "n_live", "counts": {trait_type: {value: count}}}`.
- The board and the endpoint share **one** `SELECT` (`live_trait_table`).
- Values keep the board's parsing, `str()` of each JSON value. A missing value reads `"None"`, and an empty Accessory stays `""`.
- `n_live = len(table)`, including tokens with zero parsed pairs.
- No new stores, and no new `conftest.py` pins.
- The pre-push gate must pass: ruff, ruff-format, mypy, gitleaks, the whole pytest suite, check-repo-layout.
- **No Claude/AI attribution** in commits, PR bodies or comments.
- The PR opens ready (non-draft). Greptile and CodeRabbit must both pass, and every finding is closed on its own thread.

---

### Task 1: `live_trait_table`, shared by the `nft_rarity` board

**Files:**
- Modify: `lfg_core/leaderboard.py`: the module docstring's "Boards" list, the imports (lines 53–58), `_nft_rarity` (543–583)
- Test: `tests/test_leaderboard.py` (append)

**Interfaces:**
- Produces: `leaderboard.LiveTraitTable = dict[str, tuple[int | None, list[tuple[str, str]]]]` and `leaderboard.live_trait_table(oconn: sqlite3.Connection) -> tuple[LiveTraitTable, Counter[tuple[str, str]]]`. `oconn` must have `row_factory = sqlite3.Row`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_leaderboard.py`, and add `from collections import Counter` to its imports)

```python
def _live(o, rows):
    o.executemany(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, attributes_json)"
        " VALUES (?,?,?,?,?)",
        rows,
    )


def test_live_trait_table_reads_live_tokens_and_counts_pairs():
    _, o = _dbs()
    _live(
        o,
        [
            ("N1", 1, "rA", 0, '[{"trait_type": "Hat", "value": "Cap"},'
                               ' {"trait_type": "Eyes", "value": null}]'),
            ("N2", 2, "rB", 0, '[{"trait_type": "Hat", "value": "Cap"}]'),
            ("N3", 3, "rC", 1, '[{"trait_type": "Hat", "value": "Crown"}]'),  # burned
            ("N4", 4, "rD", 0, "[]"),  # live, no pairs: still counted in the table
        ],
    )
    table, freq = leaderboard.live_trait_table(o)
    assert table == {
        "N1": (1, [("Hat", "Cap"), ("Eyes", "None")]),
        "N2": (2, [("Hat", "Cap")]),
        "N4": (4, []),
    }
    assert freq == Counter({("Hat", "Cap"): 2, ("Eyes", "None"): 1})


def test_live_trait_table_skips_unreadable_and_non_list_attributes():
    h, o = _dbs()
    _live(
        o,
        [
            ("N1", 1, "rA", 0, "null"),  # json.loads -> None: used to raise TypeError
            ("N2", 2, "rB", 0, '{"trait_type": "Hat"}'),  # an object, not a list
            ("N3", 3, "rC", 0, "not json"),
            ("N4", 4, "rD", 0, '[{"trait_type": "Hat", "value": "Cap"}]'),
        ],
    )
    table, _ = leaderboard.live_trait_table(o)
    assert list(table) == ["N4"]
    rows = leaderboard.compute(
        "nft_rarity", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert [r["nft_id"] for r in rows] == ["N4"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_leaderboard.py -k live_trait_table -v`
Expected: FAIL. The first test fails with `AttributeError: module 'lfg_core.leaderboard' has no attribute 'live_trait_table'`.

- [ ] **Step 3: Implement.** In `lfg_core/leaderboard.py`, change the imports to:

```python
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
```

Add this above `_nft_rarity`:

```python
LiveTraitTable = dict[str, tuple[int | None, list[tuple[str, str]]]]


def live_trait_table(
    oconn: sqlite3.Connection,
) -> tuple[LiveTraitTable, Counter[tuple[str, str]]]:
    """Every live collection token's trait pairs, and how many live tokens carry
    each (trait_type, value), from ONE read of `onchain_nfts`.

    Shared by the `nft_rarity` board and GET /api/rarity/supply, so the counts a
    player reads are exactly the ones the board scores, never two reads the
    listener wrote between. Values are the board's parsing: `str()` of each JSON
    value, so a missing value reads "None" and an empty Accessory stays "". A
    live token whose attributes parse to no pairs still counts toward the table;
    one whose JSON is unreadable, or isn't a list, is skipped.
    """
    rows = oconn.execute(
        "SELECT nft_id, nft_number, attributes_json FROM onchain_nfts"
        " WHERE is_burned=0 AND attributes_json IS NOT NULL AND attributes_json != ''"
    ).fetchall()
    table: LiveTraitTable = {}
    freq: Counter[tuple[str, str]] = Counter()
    for r in rows:
        try:
            attrs = json.loads(r["attributes_json"])
        except ValueError:
            continue
        if not isinstance(attrs, list):
            continue
        pairs = [
            (str(t.get("trait_type")), str(t.get("value")))
            for t in attrs
            if isinstance(t, dict) and t.get("trait_type") is not None
        ]
        table[r["nft_id"]] = (r["nft_number"], pairs)
        freq.update(pairs)
    return table, freq
```

Replace the body of `_nft_rarity` (keep its signature) with:

```python
    table, freq = live_trait_table(oconn)
    n_live = len(table) or 1
    scored = [
        _row(
            nft_id=nft_id,
            nft_number=number,
            value=round(sum(n_live / freq[p] for p in pairs), 2),
        )
        for nft_id, (number, pairs) in table.items()
        if pairs
    ]
    scored.sort(key=lambda x: x["value"], reverse=True)
    return scored[:limit]
```

In the module docstring's "Boards (this module)" list, add after the `nft_swaps` bullet:

```
- `nft_rarity`: live tokens scored by trait rarity, Σ n_live / count(pair) over
  each token's (trait_type, value) pairs, from `live_trait_table` (the same
  read GET /api/rarity/supply reports).
```

- [ ] **Step 4: Run the leaderboard tests**

Run: `.venv/bin/python -m pytest tests/test_leaderboard.py -v`
Expected: all PASS, including the existing `test_nft_rarity_scores_unique_traits_highest_and_excludes_burned`.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/leaderboard.py tests/test_leaderboard.py
git commit -m "refactor(leaderboard): live_trait_table, one read shared by the nft_rarity board

Also skips attributes_json that parses to a non-list: a JSON null used to
raise TypeError and 500 the whole board."
```

---

### Task 2: `GET /api/rarity/supply`

**Files:**
- Modify: `lfg_service/app.py`: add the handler after `handle_rarity_odds` (~2617), and the route next to `/api/rarity` (~12066)
- Modify: `CLAUDE.md`: the leaderboard "API" bullet (~822–829)
- Create: `tests/test_rarity_supply_api.py`

**Interfaces:**
- Consumes: `leaderboard.live_trait_table` (Task 1).
- Produces: `app._SUPPLY_CACHE: dict[str, tuple[float, dict[str, Any]]]`, `app._SUPPLY_CACHE_TTL = 60.0`, `app._compute_trait_supply(network: str) -> dict[str, Any]`, and `app.handle_rarity_supply(request) -> web.Response`.

- [ ] **Step 1: Write the failing tests** (`tests/test_rarity_supply_api.py`; the root `conftest.py` supplies the env, so no preamble is needed)

```python
"""GET /api/rarity/supply (agent users spec §3): shape, the 60 s per-network
cache, and parity with the nft_rarity board."""

import asyncio
import json
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from lfg_core import history_store, leaderboard
from lfg_core.nft_index import init_db as init_onchain_db
from lfg_service import app as server

TOKENS = [  # nft_id, nft_number, is_burned, attributes
    ("N1", 1, 0, [{"trait_type": "Background", "value": "Blue"},
                  {"trait_type": "Hat", "value": "Common"}]),
    ("N2", 2, 0, [{"trait_type": "Background", "value": "Blue"},
                  {"trait_type": "Hat", "value": "Unique"}]),
    ("N3", 3, 0, [{"trait_type": "Background", "value": "Red"},
                  {"trait_type": "Hat", "value": "Common"},
                  {"trait_type": "Accessory", "value": ""}]),
    ("N4", 4, 1, [{"trait_type": "Background", "value": "Blue"},
                  {"trait_type": "Hat", "value": "Unique"}]),  # burned
    ("N5", 5, 0, []),  # live, no pairs
]


@pytest.fixture
def supply_env(tmp_path, monkeypatch):
    path = str(tmp_path / "onchain_testnet.db")
    oconn = init_onchain_db(path)
    oconn.executemany(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, attributes_json)"
        " VALUES (?,?,?,?,?)",
        [(i, n, "rOwner", b, json.dumps(a)) for i, n, b, a in TOKENS],
    )
    oconn.commit()
    oconn.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    server._SUPPLY_CACHE.clear()
    yield path
    server._SUPPLY_CACHE.clear()


def _get():
    req = make_mocked_request("GET", "/api/rarity/supply", app=web.Application())
    resp = asyncio.get_event_loop().run_until_complete(server.handle_rarity_supply(req))
    return resp.status, json.loads(resp.body)


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_supply_counts_live_tokens_per_trait_value(supply_env):
    status, body = _get()
    assert status == 200
    assert body["network"] == "testnet"
    assert isinstance(body["as_of"], int)
    assert body["n_live"] == 4  # N1, N2, N3 and N5 (no pairs); N4 is burned
    assert body["counts"] == {
        "Accessory": {"": 1},
        "Background": {"Blue": 2, "Red": 1},
        "Hat": {"Common": 2, "Unique": 1},
    }


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_supply_is_cached_60s_per_network(supply_env, monkeypatch):
    calls = []
    real = leaderboard.live_trait_table

    def counting(oconn):
        calls.append(1)
        return real(oconn)

    monkeypatch.setattr(server.leaderboard, "live_trait_table", counting)
    first = _get()[1]
    assert _get()[1] == first and len(calls) == 1  # within the TTL: cached
    # ONCHAIN_DB_PATH overrides the index path for every network, so mainnet reads
    # the same fixture; only the cache key differs
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "mainnet")
    assert _get()[1]["network"] == "mainnet" and len(calls) == 2  # keyed per network
    ts, payload = server._SUPPLY_CACHE["testnet"]
    server._SUPPLY_CACHE["testnet"] = (ts - server._SUPPLY_CACHE_TTL - 1, payload)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    _get()
    assert len(calls) == 3  # expired: recomputed


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_supply_counts_reproduce_the_nft_rarity_board(supply_env):
    _, body = _get()
    oconn = init_onchain_db(supply_env)
    oconn.row_factory = sqlite3.Row
    board = leaderboard.compute(
        "nft_rarity",
        history_store.init_history_db(":memory:"),
        oconn,
        start_ts=0,
        end_ts=99,
        network="testnet",
        system_accounts=frozenset(),
    )
    n_live = body["n_live"] or 1
    attrs = {i: a for i, _, _, a in TOKENS}
    for row in board:
        pairs = [(t["trait_type"], str(t["value"])) for t in attrs[row["nft_id"]]]
        expected = round(sum(n_live / body["counts"][tt][v] for tt, v in pairs), 2)
        assert row["value"] == expected
    assert {r["nft_id"] for r in board} == {"N1", "N2", "N3"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_rarity_supply_api.py -v`
Expected: FAIL with `AttributeError: module 'lfg_service.app' has no attribute '_SUPPLY_CACHE'`.

- [ ] **Step 3: Implement.** In `lfg_service/app.py`, after `handle_rarity_odds`:

```python
# --- GET /api/rarity/supply (agent users spec §3: live trait supply) -------
# Public: how many live collection tokens carry each (trait_type, value), from
# the same single read the nft_rarity board scores
# (leaderboard.live_trait_table), so a player optimizing against these counts
# sees exactly what the board scores. Values are as the index stores them.
# Cached 60s per network.

_SUPPLY_CACHE_TTL = 60.0
_SUPPLY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _compute_trait_supply(network: str) -> dict[str, Any]:
    """Sync work for GET /api/rarity/supply, run on an executor thread with its
    own short-lived index connection (sqlite3 connections never cross threads)."""
    oconn = nft_index.init_db(nft_index.index_db_path(network))
    oconn.row_factory = sqlite3.Row  # init_db doesn't set it; the helper reads r["..."]
    try:
        table, freq = leaderboard.live_trait_table(oconn)
    finally:
        oconn.close()
    counts: dict[str, dict[str, int]] = {}
    for (trait_type, value), n in sorted(freq.items()):
        counts.setdefault(trait_type, {})[value] = n
    return {
        "network": network,
        "as_of": int(time.time()),
        "n_live": len(table),
        "counts": counts,
    }


async def handle_rarity_supply(request: web.Request) -> web.Response:
    """Public: GET /api/rarity/supply. See the note above."""
    network = config.XRPL_NETWORK
    now_mono = time.monotonic()
    cached = _SUPPLY_CACHE.get(network)
    if cached is not None and now_mono - cached[0] < _SUPPLY_CACHE_TTL:
        return web.json_response(cached[1])
    payload = await asyncio.get_event_loop().run_in_executor(
        None, _compute_trait_supply, network
    )
    for k in [k for k, (ts, _) in _SUPPLY_CACHE.items() if now_mono - ts >= _SUPPLY_CACHE_TTL]:
        del _SUPPLY_CACHE[k]
    _SUPPLY_CACHE[network] = (now_mono, payload)
    return web.json_response(payload)
```

Add the route right after `app.router.add_get("/api/rarity", handle_rarity_odds)`:

```python
    app.router.add_get("/api/rarity/supply", handle_rarity_supply)
```

In `CLAUDE.md`, after the leaderboard "API" bullet (ending "keyed on `(network, board, period, start)`."), add:

```markdown
- **Live trait supply:** `GET /api/rarity/supply` — public, no auth, cached 60s
  per network. Returns `{network, as_of, n_live, counts: {trait_type: {value:
  count}}}` over live collection tokens, from `leaderboard.live_trait_table`,
  the same single read the `nft_rarity` board scores. Values are as the index
  stores them (e.g. an empty Accessory is `""`); `n_live` includes live tokens
  with no parsed traits.
```

- [ ] **Step 4: Run the new and neighbouring tests**

Run: `.venv/bin/python -m pytest tests/test_rarity_supply_api.py tests/test_leaderboard_api.py tests/test_rarity.py tests/test_leaderboard.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_rarity_supply_api.py CLAUDE.md
git commit -m "feat(api): GET /api/rarity/supply — live trait counts from the nft_rarity board's read

Public, cached 60s per network (agent users spec §3)."
```

---

### Task 3: Gate, PR, review

- [ ] **Step 1: Run the whole gate locally**

Run: `scripts/run-tests -n auto --dist loadfile` and `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service`
Expected: all green. (The pre-push hook runs the same checks again on push.)

- [ ] **Step 2: Push and open the PR (ready, not draft)**

```bash
git push -u origin feat/agent-users-supply
gh pr create --repo Team-Hamsa/LFG --base main --head feat/agent-users-supply \
  --title "feat(api): GET /api/rarity/supply — live trait counts (agent users A/3)" \
  --body "Implements §3 of docs/superpowers/specs/2026-09-22-agent-users-design.md (plan: docs/superpowers/plans/2026-09-23-agent-users-a-rarity-supply.md).

- leaderboard.live_trait_table: one read of onchain_nfts, shared by the nft_rarity board and the new endpoint, so the counts are exactly what the board scores
- GET /api/rarity/supply: public, cached 60s per network
- Hardening: an attributes_json that parses to a non-list (e.g. JSON null) is skipped instead of raising and 500ing the board

Part A of three (B: RegularKey proofs + revocation; C: agent sign-in provider + platform=agent memos)."
```

- [ ] **Step 3: Wait for both bots, then close out every finding.** Greptile's clean pass lives only in the `Greptile Review` check-run summary:

```bash
sha=$(gh pr view <n> --repo Team-Hamsa/LFG --json headRefOid --jq .headRefOid)
gh api repos/Team-Hamsa/LFG/commits/$sha/check-runs --jq '.check_runs[]|select(.name|test("reptile"))|{conclusion,summary:.output.summary}'
```

For each actionable finding, fix it, push, and reply on its thread naming the fixing commit (or give the reason it's declined): `gh api -X POST repos/Team-Hamsa/LFG/pulls/<n>/comments/<id>/replies -f body='…'`. Re-trigger with `@greptile-apps please re-review` / `@coderabbitai review` after fixes.
