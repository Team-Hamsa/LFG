"""SourceTag volume metrics: how much tagged on-ledger activity this project
has generated, and how many unique non-project wallets have signed one.

Reads the `source_tag` column of the per-network ledger archive
(history_<net>.db, maintained live by the pm2 listeners) and emits a small
JSON snapshot: printed by default, written to a file only with `--out`.

Make Waves closed 2026-09-21. The committed metrics/sourcetag.json and its
rendered badge (assets/sourcetag.svg, scripts/render_sourcetag_svg.py) are
frozen as submitted, so this script no longer publishes anywhere: the nightly
`--push` to `main` is gone, and nothing is written unless `--out` names a
path. `--out` refuses metrics/sourcetag.json in any checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, funding, history_store, system_wallets  # noqa: E402
from lfg_core.db_path import app_db_path  # noqa: E402

# The operator's own wallets. The backend-signing issuer is resolved from
# config at call time instead of being listed here, so rotating the signing
# key can never silently start counting the backend as a user.
OPERATOR_WALLETS = frozenset(
    {
        "rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ",
        "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf",
    }
)

# Every address that has EVER signed transactions for the backend, durably.
# `config.SIGNING_ACCOUNT` only ever reflects the CURRENT signer (mainnet
# signs via a regular key today, and that key has rotated before and can
# rotate again) — rows already archived under a previous signing address
# don't move or disappear when the key rotates, so if that old address isn't
# listed here it silently reclassifies as external "user" activity the
# instant the rotation happens, permanently inflating `unique_wallets`.
# Any address the backend has ever signed with MUST be added here permanently
# — do not remove an entry just because it's no longer the active signer.
HISTORICAL_SIGNING_ADDRESSES = frozenset(
    {
        "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ",  # mainnet issuer
        # mainnet BRIX issuer: signs via its regular key (the distributor);
        # its one tagged AccountSet (2026-09-18) counted it as a user.
        "rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px",
    }
)


def excluded_wallets() -> list[str]:
    """Addresses that never count toward `unique_wallets`, sorted."""
    # config.SIGNING_ACCOUNT and the configured BRIX distributor stay unioned
    # in on top of the durable historical sets so a newly-rotated key is
    # excluded immediately, before anyone remembers to add it here.
    #
    # The distributor matters as much as the signer: every BRIX claim payout
    # has Account = the distributor and carries our SourceTag, so without this
    # each nightly payout run counts a project wallet as an external user
    # (#413). Retired distributors come from system_wallets, durably (#414).
    # The house wallet (#548) signs its own setup (trust line + Closet accept).
    configured = {
        config.SIGNING_ACCOUNT,
        config.BRIX_DISTRIBUTOR_ADDRESS,
        config.CLOSET_HOUSE_WALLET,
    }
    return sorted(
        OPERATOR_WALLETS
        | HISTORICAL_SIGNING_ADDRESSES
        | system_wallets.DURABLE_SYSTEM_ACCOUNTS
        | {a for a in configured if a}
    )


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


def dedup_by_funder(wallets: Iterable[str], funders: Mapping[str, str | None]) -> int:
    """Count distinct ACTORS among `wallets`, collapsing wallet farms.

    Two wallets activated by the same non-exchange funder are one person
    (the #461 sybil rule, shared with `sponsored_mint.reserve_if_eligible`).
    A funder in `funding.EXCHANGES` aggregates unrelated customers and is
    never a farm signal, so exchange-funded wallets each count separately —
    and so does a wallet with NO funder row at all. That last case is a
    deliberate fail-OPEN, the opposite of the admission gate: missing funder
    coverage must never silently shrink a published number.

    A funder that is itself one of the counted wallets collapses into the
    same actor as the wallets it funded, so a farm's parent wallet doesn't
    count twice.
    """
    keys = set()
    for wallet in wallets:
        funder = funders.get(wallet)
        if funder and funder not in funding.EXCHANGES:
            keys.add(funder)
        else:
            keys.add(wallet)
    return len(keys)


class AppDBError(RuntimeError):
    """The app DB (source of `wallet_funders`) could not be read.

    Distinct from a history-archive failure so the diagnostic names the
    database that actually failed — the two are different files, and the
    archive is usually fine when this is raised (CodeRabbit on #490).
    """

    def __init__(self, path: str) -> None:
        super().__init__(f"failed to read app DB {path}")
        self.path = path


def load_funders(app_db: str | None) -> dict[str, str | None]:
    """`wallet -> activation funder` from the app DB, or {} if unavailable.

    Funder rows live in the app DB (`lfg_nfts.db`), NOT in the history
    archive this script otherwise reads. A missing file or a DB that simply
    has no `wallet_funders` table yet is not an error: dedup falls open to
    the raw wallet count.

    Every OTHER sqlite failure (corruption, I/O, an incompatible schema)
    raises `AppDBError`, so `main()` exits 2 naming the app DB. Swallowing those would
    be indistinguishable from "no funder coverage" and would publish
    `unique_actors == unique_wallets` with no operational signal at all
    (Greptile P2 on #490).
    """
    if not app_db or not os.path.exists(app_db):
        return {}
    try:
        conn = sqlite3.connect(app_db)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_funders'"
            ).fetchone():
                return {}
            return dict(conn.execute("SELECT wallet, funder FROM wallet_funders"))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AppDBError(app_db) from exc


def collect(db_path: str, network: str, app_db: str | None = None) -> dict[str, Any]:
    """Compute the full metrics payload from a history archive.

    All reads run inside one explicit read transaction so they observe a
    single consistent snapshot of `xrpl_txs`, even though the pm2 listeners
    are writing to this database concurrently. `isolation_level=None` puts
    the connection in autocommit mode so we control the transaction
    boundary ourselves — a plain `sqlite3.connect()` in its default mode
    never issues a `BEGIN` for SELECTs, so it would give no snapshot
    guarantee at all (SQLite would happily interleave a concurrent writer's
    commit between our statements).
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN")
        tag = config.SOURCE_TAG
        excluded = excluded_wallets()
        placeholders = ",".join("?" for _ in excluded)

        total = conn.execute(
            "SELECT COUNT(*) FROM xrpl_txs WHERE source_tag = ?", (tag,)
        ).fetchone()[0]

        # NOTE: the exclusion set applies here and ONLY here. `total`, `by_type`
        # and `daily` deliberately count backend-signed rows too — that is the
        # project's tagged volume regardless of who pressed the button.
        accounts = [
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT account FROM xrpl_txs"
                f" WHERE source_tag = ? AND account NOT IN ({placeholders})",
                (tag, *excluded),
            )
        ]
        unique = len(accounts)

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

        # XRP moved by tagged, validated-successful `Payment`s, split by
        # direction relative to the project's own wallets: `in` = a user
        # paying the project (mint fees paid in XRP), `out` = the project
        # paying a user (refunds, rebates, XRP-denominated payouts), `other`
        # = neither side is ours. Only XRP counts — a `delivered_amount` that
        # is a drops string. IOU payments (BRIX / LFGO) are an object and are
        # deliberately NOT valued here; this exists to be compared against
        # external "volume" dashboards, which sum XRP Payments only.
        vol_in = vol_out = vol_other = 0
        for account, dest, delivered in conn.execute(
            "SELECT account, json_extract(raw_json, '$.Destination'),"
            " json_extract(raw_json, '$.meta.delivered_amount')"
            " FROM xrpl_txs WHERE source_tag = ? AND tx_type = 'Payment'"
            " AND json_extract(raw_json, '$.meta.TransactionResult') = 'tesSUCCESS'"
            " AND json_type(raw_json, '$.meta.delivered_amount') = 'text'",
            (tag,),
        ):
            # Not every text-valued delivered_amount is drops: rippled emits
            # the sentinel "unavailable" for old partial payments. Skip anything
            # that isn't a non-negative integer string rather than abort the
            # nightly run (Greptile P1 on #422).
            if not isinstance(delivered, str) or not delivered.isdigit():
                print(f"skipping non-drops delivered_amount {delivered!r}", file=sys.stderr)
                continue
            drops = int(delivered)
            if dest in excluded:
                vol_in += drops
            elif account in excluded:
                vol_out += drops
            else:
                vol_other += drops

        newest = conn.execute("SELECT MAX(close_time) FROM xrpl_txs").fetchone()[0]
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "source_tag": tag,
        "network": network,
        "total_tagged_txs": total,
        "unique_wallets": unique,
        # Same wallet set, collapsed by activation funder (#461 rule). This
        # is the number the hackathon leaderboards report; `unique_wallets`
        # stays published alongside it so the raw/deduped gap is visible.
        "unique_actors": dedup_by_funder(accounts, load_funders(app_db)),
        "by_type": by_type,
        "daily": daily,
        "excluded": excluded,
        "xrp_payment_volume": {
            "in_drops": vol_in,
            "out_drops": vol_out,
            "other_drops": vol_other,
        },
        # This is the earliest day PRESENT IN THE ARCHIVE (history_<net>.db),
        # which begins wherever the backfill was started from — not the
        # earliest day the SourceTag actually appears on-ledger. Don't read it
        # as a ledger fact.
        "first_tagged_tx": daily[0]["date"] if daily else None,
        "archive_max_close_time": (
            datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None
        ),
        "as_of": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }


# The committed snapshot's path, frozen as submitted. Matched by its last two
# components so every checkout and worktree is covered.
FROZEN_SNAPSHOT = ("metrics", "sourcetag.json")

# Every snapshot is checked against this whitelist before it is written. Keep
# it in lockstep with collect()'s payload: a new field must be added here
# deliberately, which is the point.
ALLOWED_KEYS = frozenset(
    {
        "source_tag",
        "network",
        "total_tagged_txs",
        "unique_wallets",
        "unique_actors",
        "by_type",
        "daily",
        "excluded",
        "xrp_payment_volume",
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
_VOLUME_KEYS = frozenset({"in_drops", "out_drops", "other_drops"})


def validate_payload(payload: dict[str, Any]) -> None:
    """Refuse to write anything outside the known schema.

    Raises ValueError on the first violation. This is a publication guard, not
    a correctness check: it exists so a future change that starts folding raw
    ledger JSON or an env value into the snapshot cannot silently write it.
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

    for key in ("source_tag", "total_tagged_txs", "unique_wallets", "unique_actors"):
        _int(key)

    if not _NETWORK_RE.fullmatch(str(payload["network"])):
        raise ValueError("network must be a bare lowercase name")

    by_type = payload["by_type"]
    if not isinstance(by_type, dict):
        raise ValueError("by_type must be a dict")
    for name, count in by_type.items():
        if (
            not isinstance(name, str)
            or not _TX_TYPE_RE.fullmatch(name)
            or not isinstance(count, int)
        ):
            raise ValueError(f"bad by_type entry: {name!r}")

    daily = payload["daily"]
    if not isinstance(daily, list):
        raise ValueError("daily must be a list")
    for entry in daily:
        if set(entry) != {"date", "count"}:
            raise ValueError(f"bad daily entry: {entry!r}")
        if not _DATE_RE.fullmatch(str(entry["date"])) or not isinstance(entry["count"], int):
            raise ValueError(f"bad daily entry: {entry!r}")

    excluded = payload["excluded"]
    if not isinstance(excluded, list) or not all(_ADDRESS_RE.fullmatch(str(a)) for a in excluded):
        raise ValueError("excluded must be a list of XRPL addresses")

    volume = payload["xrp_payment_volume"]
    if not isinstance(volume, dict) or set(volume) != _VOLUME_KEYS:
        raise ValueError(f"xrp_payment_volume must have exactly keys {sorted(_VOLUME_KEYS)}")
    for key, drops in volume.items():
        if not isinstance(drops, int) or isinstance(drops, bool) or drops < 0:
            raise ValueError(f"xrp_payment_volume.{key} must be a non-negative int")

    first = payload["first_tagged_tx"]
    if first is not None and not _DATE_RE.fullmatch(str(first)):
        raise ValueError("first_tagged_tx must be YYYY-MM-DD or null")

    newest = payload["archive_max_close_time"]
    if newest is not None and not _ISO_RE.fullmatch(str(newest)):
        raise ValueError("archive_max_close_time must be ISO-8601 or null")

    if not isinstance(payload["as_of"], str) or not _ISO_RE.fullmatch(payload["as_of"]):
        raise ValueError("as_of must be an ISO-8601 string")


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default=config.XRPL_NETWORK)
    ap.add_argument("--db", default=None, help="override the history DB path")
    ap.add_argument(
        "--app-db",
        default=None,
        help=(
            "override the app DB path (source of wallet_funders, which dedups "
            "unique_actors). A missing DB/table is not an error: dedup falls open "
            "and unique_actors equals unique_wallets."
        ),
    )
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "write the snapshot to this path (default: print only; the committed "
            "metrics/sourcetag.json is frozen as submitted to Make Waves)"
        ),
    )
    ap.add_argument("--json", action="store_true", help="print the full payload")
    args = ap.parse_args(argv)

    if args.out is not None and Path(args.out).resolve().parts[-2:] == FROZEN_SNAPSHOT:
        print(
            f"refusing to write {args.out}: metrics/sourcetag.json is frozen as "
            "submitted to Make Waves (2026-09-21)",
            file=sys.stderr,
        )
        return 2

    db_path = args.db or history_store.history_db_path(args.network)
    if not os.path.exists(db_path):
        print(f"history DB not found: {db_path}", file=sys.stderr)
        return 2

    try:
        payload = collect(db_path, args.network, app_db=args.app_db or app_db_path(args.network))
    except AppDBError as exc:
        print(f"{exc}: {exc.__cause__}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"failed to read {db_path}: {exc}", file=sys.stderr)
        return 2

    summary = (
        f"{payload['total_tagged_txs']} tagged txs · "
        f"{payload['unique_actors']} actors ({payload['unique_wallets']} wallets)"
    )
    if args.out is not None:
        try:
            validate_payload(payload)
        except ValueError as exc:
            print(f"refusing to write {args.out}: {exc}", file=sys.stderr)
            return 2
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_serialize(payload))
        summary += f" → {out}"
    if args.json:
        print(_serialize(payload), end="")
    else:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
