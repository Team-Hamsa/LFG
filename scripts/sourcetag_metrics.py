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
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, history_store  # noqa: E402

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


REPO = "Team-Hamsa/LFG"
REMOTE_PATH = "metrics/sourcetag.json"
BRANCH = "main"
DEFAULT_OUT = Path("metrics/sourcetag.json")


# The nightly push lands on `main` of a public repo without passing through
# the pre-push gate, so this whitelist is the only thing inspecting it. Keep it
# in lockstep with collect()'s payload: a new field must be added here
# deliberately, which is the point.
ALLOWED_KEYS = frozenset(
    {
        "source_tag",
        "network",
        "total_tagged_txs",
        "unique_wallets",
        "by_type",
        "daily",
        "excluded",
        "first_tagged_tx",
        "archive_max_close_time",
        "as_of",
    }
)
_NETWORK_RE = re.compile(r"^[a-z]+$")
_ADDRESS_RE = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.+\-]{8,}$")
_TX_TYPE_RE = re.compile(r"^[A-Za-z]+$")


def validate_payload(payload: dict[str, Any]) -> None:
    """Refuse to publish anything outside the known schema.

    Raises ValueError on the first violation. This is a publication guard, not
    a correctness check: it exists so a future change that starts folding raw
    ledger JSON or an env value into the snapshot cannot silently push it.
    """
    unexpected = set(payload) - ALLOWED_KEYS
    if unexpected:
        raise ValueError(f"unexpected key(s) in payload: {sorted(unexpected)}")

    # ALLOWED_KEYS is both a whitelist (enforced above) and a completeness
    # requirement: every published snapshot must carry every known field, so a
    # future change that drops a field (or forgets to add a new one to the
    # payload) cannot silently publish an incomplete document. Only these two
    # fields may be present-but-null; every other key must be present and
    # non-null.
    missing = ALLOWED_KEYS - set(payload)
    if missing:
        raise ValueError(f"missing required key(s): {sorted(missing)}")

    _NULLABLE_KEYS = {"first_tagged_tx", "archive_max_close_time"}
    for key in ALLOWED_KEYS - _NULLABLE_KEYS:
        if payload[key] is None:
            raise ValueError(f"{key} must not be null")

    def _int(key: str) -> None:
        if not isinstance(payload[key], int) or isinstance(payload[key], bool):
            raise ValueError(f"{key} must be an int")

    for key in ("source_tag", "total_tagged_txs", "unique_wallets"):
        _int(key)

    if not _NETWORK_RE.match(str(payload["network"])):
        raise ValueError("network must be a bare lowercase name")

    by_type = payload["by_type"]
    if not isinstance(by_type, dict):
        raise ValueError("by_type must be a dict")
    for name, count in by_type.items():
        if not _TX_TYPE_RE.match(str(name)) or not isinstance(count, int):
            raise ValueError(f"bad by_type entry: {name!r}")

    daily = payload["daily"]
    if not isinstance(daily, list):
        raise ValueError("daily must be a list")
    for entry in daily:
        if set(entry) != {"date", "count"}:
            raise ValueError(f"bad daily entry: {entry!r}")
        if not _DATE_RE.match(str(entry["date"])) or not isinstance(entry["count"], int):
            raise ValueError(f"bad daily entry: {entry!r}")

    excluded = payload["excluded"]
    if not isinstance(excluded, list) or not all(_ADDRESS_RE.match(str(a)) for a in excluded):
        raise ValueError("excluded must be a list of XRPL addresses")

    first = payload["first_tagged_tx"]
    if first is not None and not _DATE_RE.match(str(first)):
        raise ValueError("first_tagged_tx must be YYYY-MM-DD or null")

    newest = payload["archive_max_close_time"]
    if newest is not None and not _ISO_RE.match(str(newest)):
        raise ValueError("archive_max_close_time must be ISO-8601 or null")

    if not isinstance(payload["as_of"], str) or not _ISO_RE.match(payload["as_of"]):
        raise ValueError("as_of must be an ISO-8601 string")


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
    ~/LFG-staging stays on `main`, so neither deployer sees divergence. The
    trade-off is that this commit bypasses the local pre-push gate, so the
    payload is schema-validated here BEFORE any network call.
    """
    validate_payload(payload)
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


if __name__ == "__main__":
    sys.exit(main())
