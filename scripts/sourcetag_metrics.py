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
