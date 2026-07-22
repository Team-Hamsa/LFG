# SourceTag Metrics Badge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a self-updating brand-styled SVG badge in the README showing how many tagged XRPL transactions the project has generated and how many unique non-project wallets have signed one.

**Architecture:** A collector script on the deploy box reads the `source_tag` column of `history_<net>.db` and pushes a small JSON snapshot to `main` via the GitHub Contents API (never touching a local checkout). The existing `hackathon-loc.yml` workflow then renders that JSON into `assets/sourcetag.svg` on CI. Three separable pieces: compute, publish, render.

**Tech Stack:** Python 3.10 stdlib (`sqlite3`, `json`, `base64`, `argparse`, `subprocess`), the `gh` CLI (already authenticated on the box), pytest, pm2 cron, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-22-sourcetag-metrics-design.md`

## Global Constraints

- SourceTag is `2606160021`, read from `config.SOURCE_TAG` — never hardcoded in logic.
- `xrpl_txs.close_time` is stored as **unix seconds**, NOT the ripple epoch. Never apply a `- 946684800` or `+ 946684800` correction to it.
- The exclusion set applies to `unique_wallets` ONLY. `total_tagged_txs` and `by_type` count every tagged row including backend-signed ones. This asymmetry is deliberate; do not "fix" it.
- Excluded wallets: `rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ`, `rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf`, plus `config.SIGNING_ACCOUNT` (the backend issuer, resolved dynamically so a key rotation cannot silently start counting the backend as a user).
- Recipients/`Destination` addresses are never counted and never reported.
- Brand palette, font stack and drawing primitives live in `scripts/_brand.py` (Task 3) and are imported, never re-declared; SVG width is 728px to match `assets/dashboard.svg`.
- `mypy` excludes `^scripts/` (see `pyproject.toml`), so these scripts are not type-gated, but tests for them are required.
- New test files that import `lfg_core` at module top MUST carry the env-guard preamble (see Task 1 Step 1) or they strand frozen config constants and break `webapp/test_smoke` in full-suite order.
- No AI/Claude attribution in any commit message.

## File Structure

| Path | Responsibility |
|---|---|
| `scripts/sourcetag_metrics.py` | **Create.** Collector + publisher. Reads the history DB, emits the JSON snapshot, optionally PUTs it to `main` via `gh`. |
| `scripts/_brand.py` | **Create.** Shared palette + sticker/tile/sparkline primitives, lifted out of `readme_dashboard.py`. Stdlib only. |
| `scripts/readme_dashboard.py` | **Modify.** Consume `_brand` instead of its own copies. Output must stay byte-identical. |
| `scripts/render_sourcetag_svg.py` | **Create.** Pure renderer. `metrics/sourcetag.json` → `assets/sourcetag.svg`. No DB access, runs on CI. |
| `tests/test_sourcetag_metrics.py` | **Create.** Collector tests against a fixture DB. |
| `tests/test_render_sourcetag_svg.py` | **Create.** Renderer tests against a fixture JSON. |
| `metrics/sourcetag.json` | **Create** (committed seed). The snapshot the renderer consumes. |
| `.github/workflows/ci.yml` | **Modify.** Add `metrics/**` to `paths-ignore`. |
| `.github/workflows/hackathon-loc.yml` | **Modify.** Add the render step and `assets/sourcetag.svg` to `git add`. |
| `README.md` | **Modify.** Embed the badge. |
| `CLAUDE.md` | **Modify.** Document the `lfg-sourcetag` pm2 process. |

---

### Task 1: Collector — compute the metrics

**Files:**
- Create: `scripts/sourcetag_metrics.py`
- Create: `tests/test_sourcetag_metrics.py`

**Interfaces:**
- Consumes: `lfg_core.history_store.history_db_path(network) -> str`, `lfg_core.config.SOURCE_TAG: int`, `lfg_core.config.SIGNING_ACCOUNT: str`
- Produces:
  - `OPERATOR_WALLETS: frozenset[str]`
  - `excluded_wallets() -> list[str]` — sorted; operator wallets + `config.SIGNING_ACCOUNT`
  - `collect(db_path: str, network: str) -> dict` — the full JSON payload
  - `build_daily(rows: list[tuple[str, int]]) -> list[dict]` — gap-fills a `[(iso_date, count)]` series

- [ ] **Step 1: Write the failing test**

Create `tests/test_sourcetag_metrics.py`:

```python
# Tests for scripts/sourcetag_metrics.py
import importlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("ECONOMY_ENABLED", "1")

from lfg_core import config, history_store  # noqa: E402

stm = importlib.import_module("scripts.sourcetag_metrics")

TAG = config.SOURCE_TAG
USER_A = "rUserAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
USER_B = "rUserBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
OPERATOR = "rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ"

# 2026-07-20T12:00:00Z and 2026-07-22T12:00:00Z, as UNIX seconds. These are
# stored verbatim: close_time in xrpl_txs is unix, not the ripple epoch.
DAY0 = 1784548800
DAY2 = DAY0 + 2 * 86400


def _db(tmp_path, rows):
    """rows: (hash, close_time, tx_type, account, source_tag)"""
    path = str(tmp_path / "history_testnet.db")
    conn = history_store.init_history_db(path)
    conn.executemany(
        "INSERT INTO xrpl_txs (tx_hash, ledger_index, close_time, tx_type,"
        " account, source_tag, raw_json) VALUES (?,1,?,?,?,?,'{}')",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_counts_all_tagged_txs_but_excludes_our_wallets_from_unique(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h2", DAY0, "NFTokenAcceptOffer", USER_A, TAG),
            ("h3", DAY2, "NFTokenAcceptOffer", USER_B, TAG),
            ("h4", DAY2, "Payment", OPERATOR, TAG),
            ("h5", DAY2, "Payment", USER_A, None),  # untagged, must not count
        ],
    )
    out = stm.collect(path, "testnet")

    # every tagged row counts, including the backend-signed mint
    assert out["total_tagged_txs"] == 4
    # ...but only non-project signers are unique wallets
    assert out["unique_wallets"] == 2
    assert out["source_tag"] == TAG
    assert out["network"] == "testnet"


def test_by_type_is_descending_and_covers_all_tagged_rows(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h2", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h3", DAY0, "NFTokenAcceptOffer", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    assert list(out["by_type"].items()) == [("NFTokenMint", 2), ("NFTokenAcceptOffer", 1)]


def test_daily_series_is_gap_filled_and_uses_unix_close_time(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", USER_A, TAG),
            ("h2", DAY2, "NFTokenMint", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    # DAY0 is 2026-07-20; the intervening day must appear as a zero
    assert out["daily"] == [
        {"date": "2026-07-20", "count": 1},
        {"date": "2026-07-21", "count": 0},
        {"date": "2026-07-22", "count": 1},
    ]
    assert out["first_tagged_tx"] == "2026-07-20"


def test_excluded_addresses_are_reported(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    out = stm.collect(path, "testnet")
    assert config.SIGNING_ACCOUNT in out["excluded"]
    assert OPERATOR in out["excluded"]
    assert out["excluded"] == sorted(out["excluded"])


def test_no_tagged_rows_yields_zeros_not_a_crash(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, None)])
    out = stm.collect(path, "testnet")
    assert out["total_tagged_txs"] == 0
    assert out["unique_wallets"] == 0
    assert out["by_type"] == {}
    assert out["daily"] == []
    assert out["first_tagged_tx"] is None
    assert json.dumps(out)  # serialisable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sourcetag_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.sourcetag_metrics'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/sourcetag_metrics.py`:

```python
"""SourceTag volume metrics: how much tagged on-ledger activity this project
has generated, and how many unique non-project wallets have signed one.

Reads the `source_tag` column of the per-network ledger archive
(history_<net>.db, maintained live by the pm2 listeners) and emits a small
JSON snapshot. `--push` commits that snapshot to `main` via the GitHub
Contents API, deliberately without touching any local checkout so neither
polling deployer can observe divergence and halt.

The snapshot is rendered into assets/sourcetag.svg by CI — see
scripts/render_sourcetag_svg.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from lfg_core import config, history_store

# The operator's own wallets. The backend-signing issuer is resolved from
# config at call time instead of being listed here, so rotating the signing
# key can never silently start counting the backend as a user.
OPERATOR_WALLETS = frozenset(
    {
        "rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ",
        "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf",
    }
)


def excluded_wallets() -> list[str]:
    """Addresses that never count toward `unique_wallets`, sorted."""
    return sorted(OPERATOR_WALLETS | {config.SIGNING_ACCOUNT})


def _iso_day(close_time: int) -> str:
    """close_time is UNIX seconds (NOT the ripple epoch) — no offset applied."""
    return datetime.fromtimestamp(close_time, tz=timezone.utc).strftime("%Y-%m-%d")


def build_daily(rows: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """Gap-fill a [(iso_date, count)] series so quiet days read as zeros."""
    if not rows:
        return []
    per_day = dict(rows)
    cursor = date.fromisoformat(min(per_day))
    end = date.fromisoformat(max(per_day))
    out: list[dict[str, Any]] = []
    while cursor <= end:
        key = cursor.isoformat()
        out.append({"date": key, "count": per_day.get(key, 0)})
        cursor += timedelta(days=1)
    return out


def collect(db_path: str, network: str) -> dict[str, Any]:
    """Compute the full metrics payload from a history archive."""
    conn = sqlite3.connect(db_path)
    try:
        tag = config.SOURCE_TAG
        excluded = excluded_wallets()
        placeholders = ",".join("?" for _ in excluded)

        total = conn.execute(
            "SELECT COUNT(*) FROM xrpl_txs WHERE source_tag = ?", (tag,)
        ).fetchone()[0]

        # NOTE: the exclusion set applies here and ONLY here. `total`, `by_type`
        # and `daily` deliberately count backend-signed rows too — that is the
        # project's tagged volume regardless of who pressed the button.
        unique = conn.execute(
            f"SELECT COUNT(DISTINCT account) FROM xrpl_txs"
            f" WHERE source_tag = ? AND account NOT IN ({placeholders})",
            (tag, *excluded),
        ).fetchone()[0]

        by_type = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT tx_type, COUNT(*) FROM xrpl_txs WHERE source_tag = ?"
                " GROUP BY tx_type ORDER BY COUNT(*) DESC, tx_type",
                (tag,),
            )
        }

        day_rows = [
            (_iso_day(row[0]), row[1])
            for row in conn.execute(
                "SELECT close_time, COUNT(*) FROM xrpl_txs WHERE source_tag = ?"
                " AND close_time IS NOT NULL GROUP BY close_time",
                (tag,),
            )
        ]
        merged: dict[str, int] = {}
        for day, count in day_rows:
            merged[day] = merged.get(day, 0) + count
        daily = build_daily(sorted(merged.items()))

        newest = conn.execute("SELECT MAX(close_time) FROM xrpl_txs").fetchone()[0]
    finally:
        conn.close()

    return {
        "source_tag": tag,
        "network": network,
        "total_tagged_txs": total,
        "unique_wallets": unique,
        "by_type": by_type,
        "daily": daily,
        "excluded": excluded,
        "first_tagged_tx": daily[0]["date"] if daily else None,
        "archive_max_close_time": (
            datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None
        ),
        "as_of": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default=config.XRPL_NETWORK)
    args = ap.parse_args(argv)

    db_path = history_store.history_db_path(args.network)
    payload = collect(db_path, args.network)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_sourcetag_metrics.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Sanity-check against the real archive**

Run: `.venv/bin/python scripts/sourcetag_metrics.py --network mainnet | head -20`
Expected: `unique_wallets` is 16, `total_tagged_txs` is at least 1943 (it grows — the listener is live).

- [ ] **Step 6: Commit**

```bash
git add scripts/sourcetag_metrics.py tests/test_sourcetag_metrics.py
git commit -m "feat(metrics): SourceTag volume + unique-wallet collector"
```

---

### Task 2: Collector — write and publish the snapshot

**Files:**
- Modify: `scripts/sourcetag_metrics.py`
- Modify: `tests/test_sourcetag_metrics.py`

**Interfaces:**
- Consumes: `collect()` from Task 1
- Produces:
  - `is_unchanged(new: dict, existing: str | None) -> bool` — compares ignoring `as_of`
  - `push_to_github(payload: dict, runner=subprocess.run) -> bool` — returns True if a commit was made, False if skipped as unchanged
  - `main()` gains `--out PATH` (default `metrics/sourcetag.json`), `--json`, `--push`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sourcetag_metrics.py`:

```python
def test_is_unchanged_ignores_as_of():
    a = {"total_tagged_txs": 5, "as_of": "2026-07-22T00:00:00+00:00"}
    b = json.dumps({"total_tagged_txs": 5, "as_of": "2026-07-23T00:00:00+00:00"}, indent=2)
    assert stm.is_unchanged(a, b) is True

    c = json.dumps({"total_tagged_txs": 6, "as_of": "2026-07-22T00:00:00+00:00"}, indent=2)
    assert stm.is_unchanged(a, c) is False
    assert stm.is_unchanged(a, None) is False


def test_push_skips_when_unchanged_and_makes_no_write_call():
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        import subprocess as sp

        if "-X" in cmd and "PUT" in cmd:
            raise AssertionError("must not PUT when unchanged")
        body = json.dumps(
            {
                "sha": "abc123",
                "content": stm._b64(json.dumps({"total_tagged_txs": 5}, indent=2) + "\n"),
            }
        )
        return sp.CompletedProcess(cmd, 0, stdout=body, stderr="")

    made = stm.push_to_github({"total_tagged_txs": 5, "as_of": "now"}, runner=runner)
    assert made is False
    assert any("GET" in c or "contents" in " ".join(c) for c in calls)


def test_push_puts_with_existing_sha_when_changed():
    seen = {}

    def runner(cmd, **kw):
        import subprocess as sp

        if "PUT" in cmd:
            seen["put"] = cmd
            seen["input"] = kw.get("input")
            return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        body = json.dumps(
            {
                "sha": "abc123",
                "content": stm._b64(json.dumps({"total_tagged_txs": 1}, indent=2) + "\n"),
            }
        )
        return sp.CompletedProcess(cmd, 0, stdout=body, stderr="")

    made = stm.push_to_github({"total_tagged_txs": 99, "as_of": "now"}, runner=runner)
    assert made is True
    assert "abc123" in seen["input"]
    assert "main" in seen["input"]


def test_push_creates_file_when_absent_remotely():
    seen = {}

    def runner(cmd, **kw):
        import subprocess as sp

        if "PUT" in cmd:
            seen["input"] = kw.get("input")
            return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        # gh exits non-zero on 404
        return sp.CompletedProcess(cmd, 1, stdout="", stderr="Not Found (HTTP 404)")

    made = stm.push_to_github({"total_tagged_txs": 1, "as_of": "now"}, runner=runner)
    assert made is True
    assert '"sha"' not in seen["input"]


def test_out_writes_file(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    dest = tmp_path / "metrics" / "sourcetag.json"
    rc = stm.main(["--network", "testnet", "--db", path, "--out", str(dest)])
    assert rc == 0
    assert json.loads(dest.read_text())["total_tagged_txs"] == 1


def test_missing_db_exits_nonzero_without_writing(tmp_path):
    dest = tmp_path / "out.json"
    rc = stm.main(["--network", "testnet", "--db", str(tmp_path / "nope.db"), "--out", str(dest)])
    assert rc != 0
    assert not dest.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sourcetag_metrics.py -v`
Expected: FAIL — `AttributeError: module 'scripts.sourcetag_metrics' has no attribute 'is_unchanged'`

- [ ] **Step 3: Write minimal implementation**

In `scripts/sourcetag_metrics.py`, add these imports at the top:

```python
import base64
import os
import subprocess
from pathlib import Path
```

Then add, above `main()`:

```python
REPO = "Team-Hamsa/LFG"
REMOTE_PATH = "metrics/sourcetag.json"
BRANCH = "main"
DEFAULT_OUT = Path("metrics/sourcetag.json")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def is_unchanged(new: dict[str, Any], existing: str | None) -> bool:
    """True when only `as_of` differs, so a quiet day produces no commit."""
    if existing is None:
        return False
    try:
        old = json.loads(existing)
    except json.JSONDecodeError:
        return False
    return {k: v for k, v in old.items() if k != "as_of"} == {
        k: v for k, v in new.items() if k != "as_of"
    }


def _fetch_remote(runner: Any) -> tuple[str | None, str | None]:
    """(decoded_content, blob_sha) for the file on `main`; (None, None) if absent."""
    proc = runner(
        ["gh", "api", f"repos/{REPO}/contents/{REMOTE_PATH}?ref={BRANCH}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if "404" in (proc.stderr or "") or "Not Found" in (proc.stderr or ""):
            return None, None
        raise RuntimeError(f"gh api GET failed: {proc.stderr.strip()}")
    body = json.loads(proc.stdout)
    return base64.b64decode(body["content"]).decode(), body["sha"]


def push_to_github(payload: dict[str, Any], runner: Any = subprocess.run) -> bool:
    """Commit the snapshot to `main` via the Contents API. True if committed.

    Deliberately does not touch any working tree: ~/LFG stays on `deploy` and
    ~/LFG-staging stays on `main`, so neither deployer sees divergence.
    """
    existing, sha = _fetch_remote(runner)
    if is_unchanged(payload, existing):
        return False

    body: dict[str, Any] = {
        "message": "chore(metrics): refresh SourceTag snapshot",
        "content": _b64(_serialize(payload)),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    proc = runner(
        ["gh", "api", "-X", "PUT", f"repos/{REPO}/contents/{REMOTE_PATH}", "--input", "-"],
        input=json.dumps(body),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api PUT failed: {proc.stderr.strip()}")
    return True
```

Replace `main()` with:

```python
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default=config.XRPL_NETWORK)
    ap.add_argument("--db", default=None, help="override the history DB path")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="where to write the snapshot")
    ap.add_argument("--json", action="store_true", help="also print the payload")
    ap.add_argument("--push", action="store_true", help="commit the snapshot to main via gh")
    args = ap.parse_args(argv)

    db_path = args.db or history_store.history_db_path(args.network)
    if not os.path.exists(db_path):
        print(f"history DB not found: {db_path}", file=sys.stderr)
        return 2

    try:
        payload = collect(db_path, args.network)
    except sqlite3.Error as exc:
        print(f"failed to read {db_path}: {exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_serialize(payload))

    if args.json:
        print(_serialize(payload), end="")
    else:
        print(
            f"{payload['total_tagged_txs']} tagged txs · "
            f"{payload['unique_wallets']} unique wallets → {out}"
        )

    if args.push:
        try:
            committed = push_to_github(payload)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("pushed to main" if committed else "already current, no commit")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_sourcetag_metrics.py -v`
Expected: PASS, 11 passed

- [ ] **Step 5: Generate the seed snapshot**

Run: `.venv/bin/python scripts/sourcetag_metrics.py --network mainnet --out metrics/sourcetag.json`
Expected: `1943 tagged txs · 16 unique wallets → metrics/sourcetag.json` (counts may be higher).

- [ ] **Step 6: Commit**

```bash
git add scripts/sourcetag_metrics.py tests/test_sourcetag_metrics.py metrics/sourcetag.json
git commit -m "feat(metrics): snapshot output + gh-api publish to main"
```

---

### Task 3: Extract the shared brand module

**Files:**
- Create: `scripts/_brand.py`
- Modify: `scripts/readme_dashboard.py`
- Modify: `.github/workflows/hackathon-loc.yml` (invocation style only)
- Create: `tests/test_brand_module.py`

**Why:** the SourceTag badge (Task 4) must be visually identical in style to
`assets/dashboard.svg`. Duplicating the palette and the sticker/tile/sparkline
drawing code into a second renderer guarantees the two badges drift apart the
first time the brand changes. This task extracts the shared vocabulary FIRST so
Task 4 consumes it instead of copying it.

**Constraints:**
- `scripts/_brand.py` must import NOTHING from `lfg_core` and nothing outside
  the stdlib. Both renderers run on a bare CI runner with no `.env`.
- **Both renderers must be invoked as modules** (`python -m scripts.X`), not as
  file paths. `python scripts/readme_dashboard.py` puts `scripts/` on
  `sys.path` but NOT the repo root, so `from scripts._brand import …` would
  raise `ModuleNotFoundError`. Update the existing
  `run: python3 scripts/readme_dashboard.py` step in
  `.github/workflows/hackathon-loc.yml` to `run: python3 -m
  scripts.readme_dashboard` as part of this task. Do not paper over this with a
  `sys.path` mutation.
- This is a pure refactor of `readme_dashboard.py`: `assets/dashboard.svg` must
  be **byte-identical** before and after. That is the acceptance test.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (used by Task 4):
  - Palette constants: `INK`, `SURFACE`, `SURFACE_LIGHT`, `LINE`, `PAPER`,
    `TEXT`, `MUTED`, `ORANGE`, `RED`, `BLUE`, `YELLOW`, `GREEN`, `PURPLE`, `FONT`
  - `PALETTE: frozenset[str]` — every colour constant above, for tests
  - `fmt(n: int) -> str` — thousands separators
  - `esc(s: str) -> str` — XML-escapes `&`, `<`, `>`
  - `open_svg(w: int, h: int, label: str) -> str` — the `<svg …>` opening tag
    with `role="img"` and an escaped `aria-label`
  - `sticker_card(card_w: int, card_h: int) -> list[str]` — the hard `INK`
    offset shadow rect plus the `SURFACE` card with its 3px `PAPER` ring
  - `title_block(pad: int, title: str, subtitle: str) -> list[str]` — the
    19px/700 title at y=34 and the 13px `MUTED` subtitle at y=56
  - `stat_tiles(x: float, y: int, area_w: float, tiles: list[tuple[str, str, str]]) -> list[str]`
    — evenly spaced `SURFACE_LIGHT` tiles (height 60, gap 16, rx 12), each with
    a 30×5 colour bar, a 26px/800 numeral, and an 11px `MUTED` label.
    `tiles` items are `(value, label, colour)`.
  - `sparkline(x: float, base_y: int, area_w: float, series: list[int], colour: str, max_bar_h: int = 26) -> list[str]`
    — a `LINE` baseline plus one `ORANGE`-by-default bar per value, 55% of slot
    width, rx 2, zero values skipped.

- [ ] **Step 1: Capture the current dashboard SVG as the refactor oracle**

Run:
```bash
.venv/bin/python -m scripts.readme_dashboard
cp assets/dashboard.svg /tmp/dashboard-before.svg
```
Expected: `updated` or `already current`, and `/tmp/dashboard-before.svg` exists.

- [ ] **Step 2: Write the failing test**

Create `tests/test_brand_module.py`:

```python
# Tests for scripts/_brand.py — the shared badge vocabulary. Stdlib only:
# this module must import cleanly on a bare CI runner with no .env.
import importlib
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

brand = importlib.import_module("scripts._brand")


def test_module_has_no_lfg_core_dependency():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "_brand.py")).read()
    assert "lfg_core" not in src
    assert "dotenv" not in src


def test_palette_contains_every_colour_constant():
    for name in ("INK", "SURFACE", "SURFACE_LIGHT", "LINE", "PAPER", "TEXT",
                 "MUTED", "ORANGE", "RED", "BLUE", "YELLOW", "GREEN", "PURPLE"):
        assert getattr(brand, name) in brand.PALETTE


def test_fmt_and_esc():
    assert brand.fmt(1943) == "1,943"
    assert brand.esc("a & b <c>") == "a &amp; b &lt;c&gt;"


def test_open_svg_escapes_the_aria_label():
    tag = brand.open_svg(728, 330, "a & b")
    assert 'width="728"' in tag
    assert 'role="img"' in tag
    assert "a &amp; b" in tag


def test_sticker_card_and_tiles_parse_as_svg():
    parts = [brand.open_svg(728, 330, "t")]
    parts += brand.sticker_card(718, 320)
    parts += brand.title_block(24, "title", "subtitle")
    parts += brand.stat_tiles(24.0, 72, 672.0, [("16", "wallets", brand.BLUE),
                                                ("1,943", "txs", brand.ORANGE)])
    parts += brand.sparkline(24.0, 310, 672.0, [1, 0, 5], brand.ORANGE)
    parts.append("</svg>")
    root = ET.fromstring("\n".join(parts))
    assert root.attrib["height"] == "330"


def test_sparkline_skips_zero_values():
    bars = [p for p in brand.sparkline(0.0, 100, 100.0, [0, 0, 0], brand.ORANGE)
            if p.startswith("<rect")]
    assert bars == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brand_module.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts._brand'`

- [ ] **Step 4: Create the shared module**

Create `scripts/_brand.py` by lifting the palette and the drawing code out of
`scripts/readme_dashboard.py` verbatim — same numbers, same rounding, same
f-string formatting (`:.1f` where it is used today), so the rendered output is
unchanged:

```python
"""Shared LFG brand vocabulary for the README badge renderers.

Palette and drawing primitives used by scripts/readme_dashboard.py and
scripts/render_sourcetag_svg.py, so the badges cannot drift apart. Stdlib
only, and deliberately free of lfg_core imports: these run on a bare CI
runner with no .env. Source of truth for the colours is
webapp/client/style.css.
"""

from __future__ import annotations

INK = "#0A0A0A"
SURFACE = "#181818"
SURFACE_LIGHT = "#202020"  # subtle tile fill, one step up from the card
LINE = "#2C2C2C"
PAPER = "#FFFFFF"
TEXT = "#F5F4F1"
MUTED = "#9C9A94"
ORANGE = "#D89030"
RED = "#D84830"
BLUE = "#4890C0"
YELLOW = "#F0D848"
GREEN = "#3DA35D"
PURPLE = "#601878"
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

PALETTE = frozenset(
    {INK, SURFACE, SURFACE_LIGHT, LINE, PAPER, TEXT, MUTED,
     ORANGE, RED, BLUE, YELLOW, GREEN, PURPLE}
)


def fmt(n: int) -> str:
    return f"{n:,}"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def open_svg(w: int, h: int, label: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
    )


def sticker_card(card_w: int, card_h: int) -> list[str]:
    """Hard offset shadow, then the card with its paper ring. The dark fill is
    what lets the badge read on both GitHub light and dark themes."""
    return [
        f'<rect x="8" y="8" width="{card_w}" height="{card_h}" rx="18" fill="{INK}"/>',
        f'<rect x="2" y="2" width="{card_w}" height="{card_h}" rx="18" '
        f'fill="{SURFACE}" stroke="{PAPER}" stroke-width="3"/>',
    ]


def title_block(pad: int, title: str, subtitle: str) -> list[str]:
    return [
        f'<text x="{pad}" y="34" font-family="{FONT}" font-size="19" '
        f'font-weight="700" fill="{TEXT}">{esc(title)}</text>',
        f'<text x="{pad}" y="56" font-family="{FONT}" font-size="13" '
        f'fill="{MUTED}">{esc(subtitle)}</text>',
    ]


def stat_tiles(
    x: float, y: int, area_w: float, tiles: list[tuple[str, str, str]]
) -> list[str]:
    """Evenly spaced tiles: big brand-coloured number over a muted label."""
    parts: list[str] = []
    tile_h, gap = 60, 16
    tile_w = (area_w - gap * (len(tiles) - 1)) / len(tiles)
    for i, (value, label, color) in enumerate(tiles):
        tx = x + i * (tile_w + gap)
        parts.append(
            f'<rect x="{tx:.1f}" y="{y}" width="{tile_w:.1f}" height="{tile_h}" '
            f'rx="12" fill="{SURFACE_LIGHT}" stroke="{LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<rect x="{tx + 16:.1f}" y="{y + 12}" width="30" height="5" '
            f'rx="2.5" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{tx + 16:.1f}" y="{y + 42}" font-family="{FONT}" '
            f'font-size="26" font-weight="800" fill="{color}">{value}</text>'
        )
        parts.append(
            f'<text x="{tx + 16:.1f}" y="{y + 55}" font-family="{FONT}" '
            f'font-size="11" fill="{MUTED}">{esc(label)}</text>'
        )
    return parts


def sparkline(
    x: float, base_y: int, area_w: float, series: list[int],
    colour: str = ORANGE, max_bar_h: int = 26,
) -> list[str]:
    """A baseline with one thin bar per value; zero-height days are skipped."""
    parts = [
        f'<line x1="{x:.1f}" y1="{base_y}" x2="{x + area_w:.1f}" '
        f'y2="{base_y}" stroke="{LINE}" stroke-width="1"/>'
    ]
    if not series:
        return parts
    peak = max(max(series), 1)
    slot = area_w / len(series)
    bar_w = slot * 0.55
    for i, count in enumerate(series):
        if count <= 0:
            continue
        bar_h = max(max_bar_h * count / peak, 2.0)
        bx = x + i * slot + (slot - bar_w) / 2
        parts.append(
            f'<rect x="{bx:.1f}" y="{base_y - bar_h:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" rx="2" fill="{colour}"/>'
        )
    return parts
```

- [ ] **Step 5: Run the new test**

Run: `.venv/bin/python -m pytest tests/test_brand_module.py -v`
Expected: PASS, 6 passed

- [ ] **Step 6: Rewrite readme_dashboard.py to use the module**

In `scripts/readme_dashboard.py`, delete the palette constant block, the local
`fmt()`, and the inline tile/sparkline/card/title drawing code, replacing them
with calls into `scripts._brand`. Import as:

```python
from scripts._brand import (
    BLUE, GREEN, LINE, MUTED, ORANGE, RED, FONT,
    fmt, open_svg, sparkline, stat_tiles, sticker_card, title_block,
)
```

`build_svg` keeps its own geometry (`w, h = 728, 210`, `card_w, card_h = 718,
200`, `pad = 24`, `area_x, area_w = 24.0, 672.0`), its own `aria-label` string,
its own four-tile list, and its own "commits / day since …" caption — only the
shared drawing primitives move. The velocity chart becomes
`sparkline(area_x, 192, area_w, series, ORANGE)`.

Do NOT change any coordinate, font size, colour, or label text. The output must
be byte-identical.

- [ ] **Step 7: Prove the refactor changed nothing**

Run:
```bash
.venv/bin/python -m scripts.readme_dashboard
diff /tmp/dashboard-before.svg assets/dashboard.svg && echo "IDENTICAL"
```
Expected: `already current` (or `updated`), then `IDENTICAL` with no diff output.

If the diff is non-empty the refactor is wrong — fix the drawing code until it
matches exactly. Do not accept a "cosmetically equivalent" SVG.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass, no regressions.

- [ ] **Step 9: Commit**

```bash
git add scripts/_brand.py scripts/readme_dashboard.py tests/test_brand_module.py \
        .github/workflows/hackathon-loc.yml
git commit -m "refactor(badges): extract shared brand vocabulary into scripts/_brand.py"
```

---


### Task 4: Renderer — brand-styled SVG badge

**Files:**
- Create: `scripts/render_sourcetag_svg.py`
- Create: `tests/test_render_sourcetag_svg.py`

**Interfaces:**
- Consumes: `metrics/sourcetag.json` written by Task 2
- Produces: `build_svg(data: dict) -> str`, `SVG_PATH: Path`, `main() -> int`

**Note:** this module imports nothing from `lfg_core` — it must run on a bare
CI runner with no `.env`. It gets its palette and drawing primitives from
`scripts/_brand.py` (Task 3) and must NOT redefine them.

- [ ] **Step 1: Write the failing test**

Create `tests/test_render_sourcetag_svg.py`:

```python
# Tests for scripts/render_sourcetag_svg.py — pure renderer, no lfg_core import.
import importlib
import json
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

brand = importlib.import_module("scripts._brand")
rs = importlib.import_module("scripts.render_sourcetag_svg")

DATA = {
    "source_tag": 2606160021,
    "network": "mainnet",
    "total_tagged_txs": 1943,
    "unique_wallets": 16,
    "by_type": {
        "NFTokenMint": 700,
        "NFTokenCreateOffer": 692,
        "NFTokenAcceptOffer": 311,
        "NFTokenModify": 89,
        "NFTokenBurn": 77,
        "Payment": 64,
        "NFTokenCancelOffer": 2,
    },
    "daily": [
        {"date": "2026-07-20", "count": 12},
        {"date": "2026-07-21", "count": 0},
        {"date": "2026-07-22", "count": 30},
    ],
    "excluded": ["rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ"],
    "first_tagged_tx": "2026-07-20",
    "archive_max_close_time": "2026-07-22T03:20:11+00:00",
    "as_of": "2026-07-22T00:20:00+00:00",
}


def test_output_is_wellformed_xml_and_728_wide():
    root = ET.fromstring(rs.build_svg(DATA))
    assert root.attrib["width"] == "728"
    assert root.attrib["role"] == "img"
    assert "16" in root.attrib["aria-label"]
    assert "1,943" in root.attrib["aria-label"]


def test_headline_numbers_are_rendered_with_thousands_separators():
    svg = rs.build_svg(DATA)
    assert ">1,943<" in svg
    assert ">16<" in svg
    assert "2606160021" in svg


def test_uses_only_brand_palette_colours():
    import re

    svg = rs.build_svg(DATA)
    for colour in set(re.findall(r"#[0-9A-Fa-f]{6}", svg)):
        assert colour in brand.PALETTE, f"non-brand colour {colour}"


def test_renderer_does_not_redeclare_the_palette():
    """The palette lives in scripts/_brand.py; a second copy would drift."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "render_sourcetag_svg.py")).read()
    assert "#0A0A0A" not in src and "#D89030" not in src


def test_all_content_stays_inside_the_card():
    """Guards the geometry: a taller breakdown or sparkline must not overflow."""
    import re

    svg = rs.build_svg(DATA)
    root = ET.fromstring(svg)
    card_bottom = 2 + 320
    for el in root:
        y = el.attrib.get("y") or el.attrib.get("y1")
        if y is None:
            continue
        bottom = float(y) + float(el.attrib.get("height", 0))
        assert bottom <= card_bottom, f"{el.tag} at y={y} overflows the card"
    assert int(root.attrib["height"]) >= card_bottom


def test_zero_activity_renders_without_crashing():
    empty = dict(DATA, total_tagged_txs=0, unique_wallets=0, by_type={}, daily=[])
    root = ET.fromstring(rs.build_svg(empty))
    assert root.attrib["width"] == "728"


def test_main_writes_only_when_changed(tmp_path, monkeypatch):
    src = tmp_path / "sourcetag.json"
    src.write_text(json.dumps(DATA))
    dest = tmp_path / "sourcetag.svg"
    monkeypatch.setattr(rs, "JSON_PATH", src)
    monkeypatch.setattr(rs, "SVG_PATH", dest)

    assert rs.main() == 0
    first = dest.stat().st_mtime_ns
    assert rs.main() == 0
    assert dest.stat().st_mtime_ns == first  # idempotent, no rewrite


def test_main_fails_loudly_when_json_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "JSON_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(rs, "SVG_PATH", tmp_path / "out.svg")
    assert rs.main() != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_render_sourcetag_svg.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.render_sourcetag_svg'`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/render_sourcetag_svg.py`:

```python
"""Render metrics/sourcetag.json into the assets/sourcetag.svg README badge.

Pure renderer: reads one JSON file, writes one SVG. It touches no database and
imports nothing from lfg_core, so it runs on a bare CI runner. Idempotent —
the SVG is rewritten only when its content changes. Run by the same workflow
that refreshes assets/hackathon_loc.svg and assets/dashboard.svg.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from scripts._brand import (
    BLUE,
    FONT,
    GREEN,
    MUTED,
    ORANGE,
    PURPLE,
    RED,
    TEXT,
    YELLOW,
    esc,
    fmt,
    open_svg,
    sparkline,
    stat_tiles,
    sticker_card,
    title_block,
)

JSON_PATH = Path("metrics/sourcetag.json")
SVG_PATH = Path("assets/sourcetag.svg")

# Long XRPL type names are unreadable at 11px; these are the badge labels.
TYPE_LABELS = {
    "NFTokenMint": "mint",
    "NFTokenCreateOffer": "offer",
    "NFTokenAcceptOffer": "accept",
    "NFTokenModify": "modify",
    "NFTokenBurn": "burn",
    "NFTokenCancelOffer": "cancel",
    "Payment": "payment",
    "TrustSet": "trustset",
}
BAR_COLORS = [ORANGE, BLUE, RED, GREEN, YELLOW, PURPLE]


def build_svg(data: dict[str, Any]) -> str:
    """Sticker-style badge: two stat tiles, a type breakdown, a daily sparkline."""
    wallets = int(data["unique_wallets"])
    total = int(data["total_tagged_txs"])
    tag = data["source_tag"]
    by_type = data.get("by_type") or {}
    series = [int(d["count"]) for d in (data.get("daily") or [])]

    # Geometry: title block ends ~y=132, stat tiles 72..132, breakdown rows
    # 158..254 (6 × 16), sparkline caption at 276, chart baseline at 310 with
    # 26px of headroom (bar tops ≥ 284). Card spans y=2..322 — everything must
    # stay inside it.
    w, h = 728, 330
    card_w, card_h = 718, 320
    pad = 24
    area_x, area_w = float(pad), 672.0

    label = (
        f"XRPL source tag {tag}: {fmt(total)} tagged transactions "
        f"from {fmt(wallets)} unique wallets"
    )

    parts = [open_svg(w, h, label)]
    parts += sticker_card(card_w, card_h)
    parts += title_block(
        pad, f"XRPL source tag · {tag}", "live on-ledger volume · auto-updated daily"
    )
    parts += stat_tiles(
        area_x,
        72,
        area_w,
        [
            (fmt(wallets), "unique wallets", BLUE),
            (fmt(total), "tagged transactions", ORANGE),
        ],
    )

    # Type breakdown: one horizontal bar per tx type, longest first.
    row_y = 158
    ordered = sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    if ordered:
        peak = max(count for _, count in ordered)
        label_w, count_w = 58.0, 44.0
        track = area_w - label_w - count_w
        for i, (type_name, count) in enumerate(ordered):
            ry = row_y + i * 16
            color = BAR_COLORS[i % len(BAR_COLORS)]
            parts.append(
                f'<text x="{area_x:.1f}" y="{ry + 8}" font-family="{FONT}" '
                f'font-size="11" fill="{MUTED}">'
                f"{esc(TYPE_LABELS.get(type_name, type_name.lower()))}</text>"
            )
            bar_w = max(track * count / peak, 2.0)
            parts.append(
                f'<rect x="{area_x + label_w:.1f}" y="{ry:.1f}" width="{bar_w:.1f}" '
                f'height="9" rx="4.5" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{area_x + label_w + track + 8:.1f}" y="{ry + 8}" '
                f'font-family="{FONT}" font-size="11" fill="{TEXT}">{fmt(count)}</text>'
            )

    # Daily sparkline along the bottom.
    parts.append(
        f'<text x="{pad}" y="276" font-family="{FONT}" font-size="12" '
        f'fill="{MUTED}">tagged tx / day</text>'
    )
    parts += sparkline(area_x, 310, area_w, series, ORANGE)

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    if not JSON_PATH.exists():
        print(f"missing {JSON_PATH}", file=sys.stderr)
        return 2
    try:
        data = json.loads(JSON_PATH.read_text())
    except json.JSONDecodeError as exc:
        print(f"malformed {JSON_PATH}: {exc}", file=sys.stderr)
        return 2

    svg = build_svg(data)
    changed = not SVG_PATH.exists() or SVG_PATH.read_text() != svg
    if changed:
        SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
        SVG_PATH.write_text(svg)
    print("updated" if changed else "already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_render_sourcetag_svg.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Render the real badge and eyeball it**

Run: `.venv/bin/python -m scripts.render_sourcetag_svg && head -5 assets/sourcetag.svg`
Expected: `updated`, then SVG opening tags with the real numbers.

- [ ] **Step 6: Commit**

```bash
git add scripts/render_sourcetag_svg.py tests/test_render_sourcetag_svg.py assets/sourcetag.svg
git commit -m "feat(metrics): render SourceTag badge SVG in brand style"
```

---

### Task 5: Wire into CI and the README

**Files:**
- Modify: `.github/workflows/ci.yml:9-13`
- Modify: `.github/workflows/hackathon-loc.yml:26-32`
- Modify: `README.md`

- [ ] **Step 1: Exempt the metrics snapshot from the CI gate**

In `.github/workflows/ci.yml`, extend `paths-ignore` (currently `README.md`, `assets/**`):

```yaml
    paths-ignore:
      - 'README.md'
      - 'assets/**'
      # The nightly lfg-sourcetag snapshot commits only this JSON; no code
      # imports it, so it must not drag the full ruff/mypy/pytest gate.
      - 'metrics/**'
```

- [ ] **Step 2: Add the render step to the badge workflow**

In `.github/workflows/hackathon-loc.yml`, after the "Update repo-vitals dashboard" step:

```yaml
      - name: Update SourceTag badge
        run: python3 -m scripts.render_sourcetag_svg
```

and extend the `git add` line in the "Commit if changed" step:

```bash
          git add README.md assets/hackathon_loc.svg assets/dashboard.svg assets/sourcetag.svg
```

- [ ] **Step 3: Embed the badge in the README**

Find the existing `assets/dashboard.svg` reference in `README.md` and add the new badge directly beneath it, matching the surrounding markup style:

```markdown
<img src="assets/sourcetag.svg" alt="XRPL source tag 2606160021: tagged transaction volume and unique wallets" width="728">
```

- [ ] **Step 4: Verify the workflow file is valid and the render step is reachable**

Run: `.venv/bin/python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ('.github/workflows/ci.yml','.github/workflows/hackathon-loc.yml')]; print('yaml ok')"`
Expected: `yaml ok`

Run: `grep -c "assets/sourcetag.svg" .github/workflows/hackathon-loc.yml README.md`
Expected: `1` for each file.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest tests/test_sourcetag_metrics.py tests/test_render_sourcetag_svg.py -q`
Expected: 25 passed

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml .github/workflows/hackathon-loc.yml README.md
git commit -m "ci(metrics): render SourceTag badge on main, exempt metrics/ from the gate"
```

---

### Task 6: Document the pm2 cron

**Files:**
- Modify: `CLAUDE.md` (the "Running (two pm2 stacks…)" process table and the env/ops notes)

This task is documentation only — the actual `pm2 start` is an ops step the user runs on the box after merge (recorded in the handoff, not executed by the plan).

- [ ] **Step 1: Add the process to the prod pm2 table**

In `CLAUDE.md`, in the prod/staging table under "Running (two pm2 stacks, branch-driven — #223)", add a row to the prod column:

```
| `lfg-sourcetag` (cron 00:20) | — |
```

- [ ] **Step 2: Document the collector alongside the other ops scripts**

Add to `CLAUDE.md` under "Ledger history + leaderboards", after the nightly-balance-snapshots bullet:

```markdown
- **SourceTag metrics badge:** `scripts/sourcetag_metrics.py --network mainnet
  --push` reads the `source_tag` column of `history_<net>.db` and commits
  `metrics/sourcetag.json` to `main` **via the GitHub Contents API** — it never
  writes into a working tree, so neither polling deployer can see divergence
  and halt. CI (`hackathon-loc.yml`) then renders `assets/sourcetag.svg` via
  `scripts/render_sourcetag_svg.py`. Registered as pm2 process
  `lfg-sourcetag` (cron 00:20, `--no-autorestart` — pm2 shows it "stopped"
  between runs; that is normal). `unique_wallets` excludes the operator's
  wallets and `config.SIGNING_ACCOUNT`; `total_tagged_txs` deliberately does
  not (backend-signed mints are still the project's tagged volume).
  ```bash
  pm2 start scripts/sourcetag_metrics.py --name lfg-sourcetag \
    --cron "20 0 * * *" --no-autorestart --interpreter .venv/bin/python \
    -- --network mainnet --push
  ```
```

- [ ] **Step 3: Verify no other doc contradicts the close_time convention**

Run: `grep -rn "946684800" --include=*.md --include=*.py . | grep -v '\.venv'`
Expected: only matches inside `lfg_core/` conversion helpers for ledger-sourced values and the spec's warning — no match in `scripts/sourcetag_metrics.py`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): document the lfg-sourcetag metrics cron"
```

---

## Ops handoff (run on the box after merge)

1. Promote to prod is **not** required — the collector reads a DB that already exists and pushes to `main`; the renderer runs on CI. Nothing in the running services changes.
2. Register the cron on the box, from `~/LFG`:
   ```bash
   pm2 start scripts/sourcetag_metrics.py --name lfg-sourcetag \
     --cron "20 0 * * *" --no-autorestart --interpreter .venv/bin/python \
     -- --network mainnet --push
   pm2 save
   ```
3. Smoke it once by hand: `pm2 start lfg-sourcetag && pm2 logs lfg-sourcetag --lines 20`
   Expected: `NNNN tagged txs · 16 unique wallets → metrics/sourcetag.json` then `pushed to main` (or `already current, no commit`).
4. Confirm the badge rendered: the `hackathon-loc` workflow run triggered by that JSON commit should show "Update SourceTag badge → updated" and commit `assets/sourcetag.svg`.
