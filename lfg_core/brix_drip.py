"""BRIX daily distribution (#48): accrual store + pure accrual evaluation.

Holders earn 1 BRIX per **unlisted** live NFT per UTC-day epoch. Accruals are
DB-only — nothing touches the ledger until the holder explicitly claims, so
accruing needs no trustline and costs no fees.

Tables live in `history_<net>.db` alongside `brix_events`/`balance_snapshots`:
accruals ARE BRIX-economy history, and the conservation audit joins them
against the derived on-chain events.

Two invariants are enforced by the ENGINE, not by app logic:

1. `PRIMARY KEY (epoch_date, nft_id)` + `INSERT OR IGNORE` — one token can
   never accrue twice for one epoch, so re-running the daily job (or a
   catch-up sweep overlapping an already-accrued day) is a no-op.
2. Binding accruals to a claim is a single `UPDATE ... WHERE claim_id IS
   NULL`, and `idx_one_open_claim` rejects a second open claim per wallet.
   Together those make a double-claim structurally impossible rather than
   merely unlikely.

Amounts are INTEGER whole BRIX. The conservation audit compares SUMs across
three sources; float accumulation drift would force epsilon tolerances or
produce false FAILs.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from lfg_core import xrpl_ops
from lfg_core.nft_index import OnchainNft

logger = logging.getLogger(__name__)

# One whole BRIX per unlisted token per epoch. Fractional rates are an
# explicit schema migration away (spec §9), not a config knob.
DRIP_AMOUNT = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS brix_accruals (
    epoch_date TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    nft_id     TEXT NOT NULL,
    owner      TEXT NOT NULL,               -- holder at evaluation time
    amount     INTEGER NOT NULL DEFAULT 1,  -- whole BRIX
    claim_id   INTEGER,                     -- NULL = unclaimed
    PRIMARY KEY (epoch_date, nft_id)
);
CREATE INDEX IF NOT EXISTS idx_accrual_owner ON brix_accruals(owner, claim_id);

CREATE TABLE IF NOT EXISTS brix_claims (
    claim_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    amount          INTEGER NOT NULL,   -- whole BRIX (Σ of bound accruals)
    state           TEXT NOT NULL,      -- pending|submitted|confirmed|failed
    tx_hash         TEXT,
    last_ledger_seq INTEGER,            -- Payment LastLedgerSequence
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- One in-flight claim per wallet, enforced by the engine across processes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_claim
    ON brix_claims(wallet) WHERE state IN ('pending', 'submitted');

CREATE TABLE IF NOT EXISTS brix_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

LAST_ACCRUED_EPOCH = "last_accrued_epoch"


@dataclass(frozen=True)
class Accrual:
    """One token's earnings for one epoch."""

    epoch_date: str
    nft_id: str
    owner: str
    amount: int = DRIP_AMOUNT


@dataclass(frozen=True)
class EvaluationResult:
    """Accrual rows for an epoch plus why everything else was skipped."""

    rows: list[Accrual]
    skipped_listed: int = 0
    skipped_burned: int = 0
    skipped_system: int = 0
    skipped_ownerless: int = 0
    unknown: int = 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the drip tables if absent. Idempotent, like init_history_db."""
    conn.executescript(_SCHEMA)
    conn.commit()


def record_accruals(conn: sqlite3.Connection, rows: Iterable[Accrual]) -> int:
    """Insert accrual rows, ignoring any (epoch_date, nft_id) already present.

    Returns the number of rows actually inserted, so a catch-up run can report
    genuinely-new accruals rather than rows it merely re-attempted.
    """
    payload = [(r.epoch_date, r.nft_id, r.owner, int(r.amount)) for r in rows]
    if not payload:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO brix_accruals (epoch_date, nft_id, owner, amount)"
        " VALUES (?, ?, ?, ?)",
        payload,
    )
    conn.commit()
    return conn.total_changes - before


def claimable(conn: sqlite3.Connection, wallet: str) -> int:
    """Unclaimed whole-BRIX balance for `wallet` (0 when nothing is owed)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_accruals WHERE owner = ? AND claim_id IS NULL",
        (wallet,),
    ).fetchone()
    return int(row[0])


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM brix_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO brix_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def classify_sell_offers(response: dict[str, Any], holder: str) -> bool:
    """True iff this token is LISTED — i.e. the CURRENT holder has a live sell
    offer on it.

    Two subtleties, both deliberate (spec §3):

    * A destination-locked offer still counts as listed. Brokered marketplaces
      (xrp.cafe and friends) list by creating a sell offer destined to the
      broker, and those are exactly the listings we must exclude.
    * An offer owned by a PREVIOUS holder does not count. Such offers survive a
      transfer on-ledger but are unfillable, so they must not suppress the new
      owner's drip.
    """
    for offer in response.get("offers") or []:
        if offer.get("owner") == holder:
            return True
    return False


def evaluate_accruals(
    live_tokens: Sequence[OnchainNft],
    listed_fn: Callable[[str], bool | None],
    system_accounts: frozenset[str],
    epoch: str,
) -> EvaluationResult:
    """Decide who earns for `epoch`. Pure: all I/O is behind `listed_fn`.

    `listed_fn` returns True (listed), False (unlisted), or None (unknown).
    Unknown **never pays** — an accrual is a monetary grant, and systematically
    paying listed NFTs through a clio outage is not recoverable, while a rare
    missed BRIX is.
    """
    rows: list[Accrual] = []
    listed = burned = system = ownerless = unknown = 0

    for token in live_tokens:
        if token.is_burned:
            burned += 1
            continue
        owner = token.owner
        if not owner:
            ownerless += 1
            continue
        if owner in system_accounts:
            system += 1
            continue
        state = listed_fn(token.nft_id)
        if state is None:
            unknown += 1
            continue
        if state:
            listed += 1
            continue
        rows.append(Accrual(epoch, token.nft_id, owner, DRIP_AMOUNT))

    return EvaluationResult(
        rows=rows,
        skipped_listed=listed,
        skipped_burned=burned,
        skipped_system=system,
        skipped_ownerless=ownerless,
        unknown=unknown,
    )


async def fetch_sell_offer_state(
    holders: dict[str, str],
    retries: int = 3,
) -> dict[str, bool | None]:
    """Look up live listing state for each `nft_id -> current holder`.

    Returns True (listed), False (unlisted), or **None (unknown)** per token.
    None is the fail-closed signal `evaluate_accruals` refuses to pay on: a
    lookup that failed is NOT evidence that a token is unlisted.

    `raise_on_error=True` is essential here for the same reason
    `backfill_market.py`'s stale-close pass needs it — the default swallows RPC
    failures into an empty list, which would read as "no offers" and pay a
    token that may well be listed.
    """
    state: dict[str, bool | None] = {}
    for nft_id, holder in holders.items():
        state[nft_id] = await _lookup_one(nft_id, holder, retries)
    return state


async def _lookup_one(nft_id: str, holder: str, retries: int) -> bool | None:
    last_error: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            offers = await xrpl_ops.get_nft_sell_offers(nft_id, raise_on_error=True)
        except Exception as exc:  # noqa: BLE001 — any failure is "unknown"
            last_error = exc
            continue
        return classify_sell_offers({"offers": offers}, holder)
    logger.warning("brix_drip: listing state unknown for %s (%s)", nft_id, last_error)
    return None


@dataclass(frozen=True)
class EpochReport:
    """What one epoch's accrual pass actually wrote."""

    epoch: str
    accrued: int
    skipped_listed: int
    skipped_burned: int
    skipped_system: int
    skipped_ownerless: int
    unknown: int


def epochs_to_accrue(last_accrued: str | None, today: str) -> list[str]:
    """UTC dates still owed an accrual pass, oldest first.

    Epochs close at midnight UTC, so the newest accruable epoch is always
    YESTERDAY — today is still in progress. With no cursor we accrue only
    yesterday (no retroactive grants, spec §9); with one we walk forward from
    it so a missed cron day self-heals on the next run.
    """
    yesterday = _add_days(today, -1)
    if last_accrued is None:
        return [yesterday]
    out: list[str] = []
    cursor = _add_days(last_accrued, 1)
    while cursor <= yesterday:
        out.append(cursor)
        cursor = _add_days(cursor, 1)
    return out


def _add_days(date_str: str, days: int) -> str:
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (day + timedelta(days=days)).strftime("%Y-%m-%d")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def run_accrual(
    conn: sqlite3.Connection,
    live_tokens: Sequence[OnchainNft],
    listed_fn: Callable[[str], bool | None],
    system_accounts: frozenset[str],
    today: str | None = None,
) -> list[EpochReport]:
    """Accrue every epoch still owed, advancing the cursor as each one lands.

    A catch-up epoch is evaluated against ownership and listing state as they
    are NOW, not as they were at that epoch's close — reconstructing historical
    per-day state from `nft_events` is heavy and, given offer-index ambiguity,
    unreliable (spec §3/§8). At 1 BRIX/day the drift is negligible.

    The cursor advances per epoch rather than once at the end, so a crash
    mid-catch-up never re-grants the epochs that already committed.
    """
    today = today or utc_today()
    reports: list[EpochReport] = []
    for epoch in epochs_to_accrue(get_meta(conn, LAST_ACCRUED_EPOCH), today):
        result = evaluate_accruals(live_tokens, listed_fn, system_accounts, epoch)
        inserted = record_accruals(conn, result.rows)
        set_meta(conn, LAST_ACCRUED_EPOCH, epoch)
        reports.append(
            EpochReport(
                epoch=epoch,
                accrued=inserted,
                skipped_listed=result.skipped_listed,
                skipped_burned=result.skipped_burned,
                skipped_system=result.skipped_system,
                skipped_ownerless=result.skipped_ownerless,
                unknown=result.unknown,
            )
        )
    return reports


@dataclass(frozen=True)
class AuditResult:
    """One conservation check's verdict, in audit_history.py's shape."""

    name: str
    ok: bool
    detail: str


def audit_distribution(
    conn: sqlite3.Connection,
    distributor: str,
    live_token_count: int,
) -> list[AuditResult]:
    """Cross-check the drip ledger against itself and against the chain.

    The DB sides are compared as exact integers — that is the whole reason
    amounts are INTEGER. Only the on-chain side comes from `brix_events.delta`,
    which is REAL because a ledger balance diff is, so it is rounded to whole
    BRIX before comparison rather than compared with an epsilon.
    """
    results: list[AuditResult] = []

    bound = int(
        conn.execute(
            "SELECT COALESCE(SUM(a.amount), 0) FROM brix_accruals a"
            " JOIN brix_claims c ON c.claim_id = a.claim_id"
            " WHERE c.state = 'confirmed'"
        ).fetchone()[0]
    )
    claimed = int(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM brix_claims WHERE state = 'confirmed'"
        ).fetchone()[0]
    )
    results.append(
        AuditResult(
            "accruals_match_claims",
            bound == claimed,
            f"accruals bound to confirmed claims={bound} confirmed claim total={claimed}",
        )
    )

    paid_out = round(
        float(
            conn.execute(
                "SELECT COALESCE(SUM(-delta), 0) FROM brix_events"
                " WHERE kind = 'claim' AND account = ?",
                (distributor,),
            ).fetchone()[0]
        )
    )
    results.append(
        AuditResult(
            "claims_match_chain",
            claimed == paid_out,
            f"confirmed claim total={claimed} on-chain distributor debits={paid_out}",
        )
    )

    orphaned = int(
        conn.execute(
            "SELECT COUNT(*) FROM brix_accruals a JOIN brix_claims c ON c.claim_id = a.claim_id"
            " WHERE c.state = 'failed'"
        ).fetchone()[0]
    )
    results.append(
        AuditResult(
            "no_orphaned_bindings",
            orphaned == 0,
            f"accruals still bound to a failed claim={orphaned} (should have been unbound)",
        )
    )

    hashless = int(
        conn.execute(
            "SELECT COUNT(*) FROM brix_claims WHERE state = 'confirmed'"
            " AND (tx_hash IS NULL OR tx_hash = '')"
        ).fetchone()[0]
    )
    results.append(
        AuditResult(
            "confirmed_claims_have_hashes",
            hashless == 0,
            f"confirmed claims with no tx_hash={hashless}",
        )
    )

    row = conn.execute(
        "SELECT epoch_date, COUNT(*) c FROM brix_accruals GROUP BY epoch_date"
        " ORDER BY c DESC LIMIT 1"
    ).fetchone()
    worst_epoch, worst_count = (row[0], int(row[1])) if row else ("-", 0)
    results.append(
        AuditResult(
            "epoch_within_supply",
            worst_count <= live_token_count,
            f"busiest epoch {worst_epoch} accrued {worst_count} of {live_token_count} live tokens",
        )
    )

    return results
