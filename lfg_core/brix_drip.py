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

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Request

from lfg_core import config, epoch_state, history_store, xrpl_ops

logger = logging.getLogger(__name__)

# One whole BRIX per unlisted token per epoch. Fractional rates are an
# explicit schema migration away (spec §9), not a config knob.
DRIP_AMOUNT = 1

# An epoch whose listing state is unknown for more than this fraction of the
# eligible, non-system, live tokens is DEFERRED rather than accrued (#411 I1).
# Without it a stale derived table (nft_events missing offer_index/offer_flags)
# makes the nightly certify, write ~nothing, and ADVANCE the cursor — a silent
# zero-pay day that the PK then makes permanent. Deferring costs a day of
# latency; advancing costs the day's drip forever.
UNKNOWN_DEFER_FRACTION = 0.10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS brix_accruals (
    epoch_date TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    nft_id     TEXT NOT NULL,
    owner      TEXT NOT NULL,               -- holder at evaluation time
    amount     INTEGER NOT NULL DEFAULT 1,  -- whole BRIX
    claim_id   INTEGER,                     -- NULL = unclaimed
    source     TEXT,                        -- NULL = nightly; else the writer
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

# Hash of mainnet ledger 32570 — the earliest ledger any node still serves, and
# a permanent chain fingerprint. Hardcoded on purpose: mainnet's identity can
# never change, so the wrong-chain guard must not depend on an archive having
# been certified first. Testnet has no equivalent constant (a reset changes
# it), so there the archive's recorded value is the only truth.
CHAIN_CHECK_TIMEOUT_SECONDS = 30.0

MAINNET_GENESIS_HASH = "4109C6F2045FC7EFF4CDE8F9905D19C28820D86304080FF886B299F0206E42B5"


@dataclass(frozen=True)
class Accrual:
    """One token's earnings for one epoch."""

    epoch_date: str
    nft_id: str
    owner: str
    amount: int = DRIP_AMOUNT
    source: str | None = None
    """Provenance of the row: NULL for the nightly job, a marker such as
    `brix_backfill.BACKFILL_SOURCE` for a historical backfill. Rollback needs
    to tell one from the other — the backfill window overlaps epochs the
    nightly already wrote."""


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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(brix_accruals)")}
    if "source" not in cols:
        # Self-migrating, like history_store.init_history_db: pre-existing DBs
        # carry nightly-written rows, which are exactly the NULL-source rows.
        conn.execute("ALTER TABLE brix_accruals ADD COLUMN source TEXT")
    conn.commit()


def record_accruals(conn: sqlite3.Connection, rows: Iterable[Accrual]) -> int:
    """Insert accrual rows, ignoring any (epoch_date, nft_id) already present.

    Returns the number of rows actually inserted, so a catch-up run can report
    genuinely-new accruals rather than rows it merely re-attempted.
    """
    payload = [(r.epoch_date, r.nft_id, r.owner, int(r.amount), r.source) for r in rows]
    if not payload:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO brix_accruals (epoch_date, nft_id, owner, amount, source)"
        " VALUES (?, ?, ?, ?, ?)",
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


class TokenLike(Protocol):
    """What the evaluator needs from a token — `nft_index.OnchainNft` (live
    index) and `epoch_state.EpochToken` (archive replay) both satisfy it."""

    @property
    def nft_id(self) -> str: ...
    @property
    def owner(self) -> str | None: ...
    @property
    def is_burned(self) -> bool: ...


def evaluate_accruals(
    live_tokens: Sequence[TokenLike],
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
    deferred: str | None = None
    """Certification reason; when set, accrued == 0 and cursor did NOT move."""
    skipped_ineligible: int = 0
    """Replayed tokens that are not in the collection index (#411 C1) — Closet
    and trait tokens live in `nft_events` but were never drip-eligible."""
    owner_drift: int = 0
    """Tokens whose replayed owner at the archive's LATEST state disagreed with
    the index, checked on the newest epoch only (#411 C2). That means a stale
    derived table, not a legitimate post-close transfer. Forced to unknown
    listing state, so they never pay."""


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


@dataclass(frozen=True)
class EpochEvaluation:
    """One epoch decided but NOT yet written (#411 I1 needs the numbers before
    the write, so evaluation and persistence are two steps)."""

    result: EvaluationResult
    skipped_ineligible: int = 0
    owner_drift: int = 0
    deferred: str | None = None


def eligible_tokens(
    tokens: Mapping[str, epoch_state.EpochToken],
    eligible: Mapping[str, str | None],
) -> tuple[list[epoch_state.EpochToken], int]:
    """Replayed tokens restricted to the collection index, plus the drop count.

    `nft_events` covers every taxon we archive — Closet (soulbound) and trait
    tokens included — while `onchain_nfts` holds only the collection. Scoping
    here (rather than inside the pure `epoch_state` replay) keeps the replay
    free of the index DB; callers build `eligible` from
    `nft_index.collection_owners`.
    """
    kept = [t for nft_id, t in tokens.items() if nft_id in eligible]
    return kept, len(tokens) - len(kept)


def evaluate_epoch(
    epoch: str,
    tokens: Mapping[str, epoch_state.EpochToken],
    system_accounts: frozenset[str],
    *,
    eligible: Mapping[str, str | None],
    network: str = "",
    current: Mapping[str, epoch_state.EpochToken] | None = None,
) -> EpochEvaluation:
    """Decide one epoch from replayed state. Pure — writes nothing.

    `current` is the replay advanced to the archive's LATEST state (today's
    close bound), supplied only for the newest epoch. Drift compares THAT owner
    against the index — never the epoch-close owner. A token legitimately
    transferred after the epoch closed replays the new owner at `current` too,
    so it is not drift and still pays its close-time holder; a mismatch here
    means the derived table is stale (a tesSUCCESS accept in `xrpl_txs` with no
    `nft_events` row), and paying that replay would misattribute the drip.
    """
    kept, skipped_ineligible = eligible_tokens(tokens, eligible)

    drift: set[str] = set()
    if current is not None:
        # Checked over burned tokens too, not just live ones: the index
        # (`nft_index.collection_owners`) keeps `owner` on burned rows, and a
        # missing archived transfer followed by a recorded burn leaves the
        # replay holding the stale pre-transfer owner while `tok.live` is
        # False — excluding burned rows here would let that stale-derived-
        # table signal escape drift detection entirely (Greptile #411 P1).
        drift = {
            nft_id
            for nft_id, tok in current.items()
            if nft_id in eligible and eligible[nft_id] is not None and tok.owner != eligible[nft_id]
        }

    def _listed(nft_id: str) -> bool | None:
        if nft_id in drift:
            return None  # unknown → never paid
        return tokens[nft_id].listed

    result = evaluate_accruals(
        kept,
        listed_fn=_listed,
        system_accounts=system_accounts,
        epoch=epoch,
    )
    # Everything actually in scope for a pay/no-pay decision this epoch.
    considered = len(result.rows) + result.skipped_listed + result.unknown
    deferred: str | None = None
    net = network or "<net>"
    if not eligible:
        # A missing/misconfigured index DB is CREATED empty by init_db, which
        # would make every token ineligible, every count zero, the guard below
        # a no-op — and the cursor would still advance over an epoch nobody
        # can ever be paid for. Zero eligible tokens is never a real state.
        deferred = (
            f"no eligible tokens (eligible map empty) — check onchain_{net}.db / ONCHAIN_DB_PATH"
        )
    elif considered == 0 and skipped_ineligible > 0:
        deferred = (
            f"no eligible tokens (all {skipped_ineligible} replayed tokens ineligible) — "
            f"check onchain_{net}.db / ONCHAIN_DB_PATH"
        )
    elif considered and result.unknown > UNKNOWN_DEFER_FRACTION * considered:
        deferred = (
            f"listing state unknown for {result.unknown} of {considered} eligible tokens — "
            f"run scripts/derive_history_events.py --network {net}"
        )
    return EpochEvaluation(
        result=result,
        skipped_ineligible=skipped_ineligible,
        owner_drift=len(drift),
        deferred=deferred,
    )


def accrue_epoch(
    conn: sqlite3.Connection,
    epoch: str,
    tokens: Mapping[str, epoch_state.EpochToken],
    system_accounts: frozenset[str],
    *,
    eligible: Mapping[str, str | None],
    network: str = "",
    current: Mapping[str, epoch_state.EpochToken] | None = None,
) -> EpochReport:
    """Evaluate + write one epoch from replayed state. Cursor untouched —
    the nightly runner advances it, the #412 backfill never does.

    A mass-unknown epoch is deferred instead of written (see
    `UNKNOWN_DEFER_FRACTION`): nothing is inserted and the caller must leave
    the cursor behind it."""
    ev = evaluate_epoch(
        epoch,
        tokens,
        system_accounts,
        eligible=eligible,
        network=network,
        current=current,
    )
    result = ev.result
    inserted = 0 if ev.deferred else record_accruals(conn, result.rows)
    return EpochReport(
        epoch=epoch,
        accrued=inserted,
        skipped_listed=result.skipped_listed,
        skipped_burned=result.skipped_burned,
        skipped_system=result.skipped_system,
        skipped_ownerless=result.skipped_ownerless,
        unknown=result.unknown,
        deferred=ev.deferred,
        skipped_ineligible=ev.skipped_ineligible,
        owner_drift=ev.owner_drift,
    )


def run_archive_accrual(
    conn: sqlite3.Connection,
    network: str,
    system_accounts: frozenset[str],
    today: str | None = None,
    *,
    eligible: Mapping[str, str | None],
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
    owed = epochs_to_accrue(get_meta(conn, LAST_ACCRUED_EPOCH), today)
    newest = owed[-1] if owed else None
    # A SEPARATE throwaway replay advanced to the archive's latest state, so
    # the newest epoch's drift check compares index owner against the CURRENT
    # replayed owner (a stale derived table) rather than the epoch-close owner
    # (which a legitimate post-close transfer would make differ). The walking
    # replay above must not be disturbed — it is mid-window.
    current: Mapping[str, epoch_state.EpochToken] | None = None
    if newest is not None:
        current = replay_factory(conn).advance_to(today)
    for epoch in owed:
        reason = certify(conn, network, epoch)
        if reason is not None:
            reports.append(EpochReport(epoch, 0, 0, 0, 0, 0, 0, deferred=reason))
            break
        report = accrue_epoch(
            conn,
            epoch,
            replay.advance_to(epoch),
            system_accounts,
            eligible=eligible,
            network=network,
            current=current if epoch == newest else None,
        )
        reports.append(report)
        if report.owner_drift:
            logger.warning(
                "brix_drip: %s tokens' current replayed owner disagrees with onchain_nfts at %s — "
                "they were NOT paid; the derived table is incomplete, run "
                "scripts/derive_history_events.py --network %s",
                report.owner_drift,
                epoch,
                network,
            )
        if report.deferred:
            # Mass-unknown epoch: nothing written, cursor stays behind it so a
            # later run (after a rederive) completes it. Stop the walk.
            break
        set_meta(conn, LAST_ACCRUED_EPOCH, epoch)
    return reports


@dataclass(frozen=True)
class AuditResult:
    """One conservation check's verdict, in audit_history.py's shape.

    `skipped` is a third state, distinct from both PASS and FAIL. A check that
    could not run is neither evidence of health nor of drift, and collapsing it
    into either is wrong in a different way: reporting FAIL trains operators to
    ignore the audit, while reporting PASS hides that nothing was verified.
    """

    name: str
    ok: bool
    detail: str
    skipped: bool = False


def audit_distribution(
    conn: sqlite3.Connection,
    distributor: str,
    token_supply_ceiling: int,
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
    # The ceiling is every token EVER minted, not the currently-live count.
    # Tokens get burned, so a past epoch's legitimate accrual count can easily
    # exceed today's live supply — comparing against it would report false
    # drift and fail the audit for a perfectly correct history. Total-ever-
    # minted is monotonic and still catches what this check is for: an epoch
    # with more accrual rows than tokens could possibly exist.
    if token_supply_ceiling <= 0 and worst_count > 0:
        # Accruals exist for tokens the index does not know about. That is not
        # an un-runnable check — it is a genuine contradiction (an empty,
        # unavailable, or wrong-network index), and it must fail loudly.
        results.append(
            AuditResult(
                "epoch_within_supply",
                False,
                f"the on-chain index reports no tokens, yet epoch {worst_epoch} "
                f"accrued {worst_count} rows — the index is empty, unavailable, "
                f"or pointed at the wrong network",
            )
        )
    elif token_supply_ceiling <= 0:
        # No tokens AND no accruals: a fresh install with nothing to verify
        # yet. Genuinely un-runnable rather than wrong, so report it as such
        # instead of claiming a pass.
        results.append(
            AuditResult(
                "epoch_within_supply",
                True,
                "no tokens in the index and no accruals recorded — nothing to check yet",
                skipped=True,
            )
        )
    else:
        results.append(
            AuditResult(
                "epoch_within_supply",
                worst_count <= token_supply_ceiling,
                f"busiest epoch {worst_epoch} accrued {worst_count} of "
                f"{token_supply_ceiling} tokens ever minted",
            )
        )

    return results


async def verify_endpoint_chain(conn: sqlite3.Connection, network: str) -> str | None:
    """Confirm the configured JSON-RPC endpoint really is on `network`'s chain.

    Matching `--network` against `XRPL_NETWORK` is not sufficient on its own:
    `XRPL_JSON_RPC_URL` can override the endpoint independently, so both names
    can agree while the socket points at the other chain. Then every one of our
    NFT ids is unknown there, `nft_sell_offers` reports no offers, and — because
    UNLISTED is the state that pays — listed tokens get granted BRIX.

    The anchor is ledger 32570's hash: stable forever on a given chain,
    different across chains, and already recorded per network by the archive.

    Returns None when the endpoint is verified (or cannot be verified because
    the archive has no recorded identity yet), or an error string to refuse on.
    """
    expected = MAINNET_GENESIS_HASH if network == "mainnet" else ""
    if not expected:
        # Testnet's identity is not a constant — a testnet reset changes ledger
        # 32570's hash — so the archive's recorded value is the only truth
        # available there.
        state = history_store.get_archive_state(conn, network)
        expected = (state.genesis_hash or "").strip() if state else ""
    if not expected:
        # Testnet with an uncertified archive: nothing trustworthy to compare
        # against. Warn rather than fabricate a verdict, and rather than refuse
        # — coupling the drip to archive certification would block staging for
        # an unrelated feature, and the stakes here are test BRIX. Mainnet
        # never reaches this branch.
        logger.warning(
            "brix_drip: no recorded chain identity for %s; the endpoint's chain is UNVERIFIED",
            network,
        )
        return None

    client = JsonRpcClient(config.JSON_RPC_URL)

    async def request_fn(req: dict[str, Any]) -> dict[str, Any]:
        # xrpl-py's JsonRpcClient exposes no timeout, so an unresponsive
        # endpoint would hang the accrual run indefinitely instead of failing
        # it. Bound it here.
        response = await asyncio.wait_for(
            asyncio.to_thread(client.request, Request.from_dict(req)),
            timeout=CHAIN_CHECK_TIMEOUT_SECONDS,
        )
        if not response.is_successful():
            raise RuntimeError(f"{req['method']} failed: {response.result}")
        result: dict[str, Any] = response.result
        return result

    snapshot = await history_store.fetch_endpoint_snapshot(request_fn)
    # Ledger hashes are hex; servers and stored values differ in case, and a
    # case-only mismatch would reject a perfectly correct endpoint.
    if snapshot.genesis_hash.upper() != expected.upper():
        return (
            f"endpoint {config.JSON_RPC_URL} is not on the {network} chain "
            f"(ledger {history_store.EARLIEST_AVAILABLE_LEDGER} hash "
            f"{snapshot.genesis_hash[:16]}… != recorded {expected[:16]}…)"
        )
    return None


class ClaimError(RuntimeError):
    """Base for claim-opening refusals."""


class NothingToClaim(ClaimError):
    """The wallet has no unclaimed accruals."""


class ClaimInFlight(ClaimError):
    """The wallet already has an open (pending/submitted) claim."""


OPEN_STATES = ("pending", "submitted")


def open_claim(
    conn: sqlite3.Connection, wallet: str, last_ledger_seq: int | None = None
) -> tuple[int, int]:
    """Open a claim for every currently-unclaimed accrual of `wallet`.

    `last_ledger_seq` is written IN THIS TRANSACTION, not afterwards. Recovery
    skips claims with a NULL deadline, so a claim that briefly exists without
    one is unrecoverable if the process dies in that gap — accruals bound
    forever and the wallet blocked by claim_in_flight. Two separate writes can
    never be atomic across a crash, so the deadline is part of the insert and
    the bad state is unrepresentable rather than merely unlikely.

    Runs as ONE immediate transaction so the balance read, the claim insert and
    the row binding cannot interleave with a competing claim. Two racing claims
    resolve deterministically: sqlite serializes the writes, and whichever
    arrives second is rejected by `idx_one_open_claim` — never by both binding
    the same rows.

    Returns `(claim_id, amount)`.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Check for an open claim BEFORE the balance. The loser of a race has
        # already had its rows bound by the winner, so a balance-first check
        # would report "nothing to claim" — technically true, but it hides the
        # real reason and tells the holder their BRIX vanished. The unique
        # index still backstops this across processes.
        open_claim_row = conn.execute(
            f"SELECT claim_id FROM brix_claims WHERE wallet = ?"  # noqa: S608 — literal tuple
            f" AND state IN ({','.join('?' * len(OPEN_STATES))})",
            (wallet, *OPEN_STATES),
        ).fetchone()
        if open_claim_row is not None:
            conn.rollback()
            raise ClaimInFlight(f"{wallet} already has an open claim")
        amount = claimable(conn, wallet)
        if amount <= 0:
            conn.rollback()
            raise NothingToClaim(f"{wallet} has no unclaimed BRIX")
        cur = conn.execute(
            "INSERT INTO brix_claims (wallet, amount, state, last_ledger_seq)"
            " VALUES (?, ?, 'pending', ?)",
            (wallet, amount, last_ledger_seq),
        )
        claim_id = int(cur.lastrowid or 0)
        conn.execute(
            "UPDATE brix_accruals SET claim_id = ? WHERE owner = ? AND claim_id IS NULL",
            (claim_id, wallet),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ClaimInFlight(f"{wallet} already has an open claim") from exc
    except ClaimError:
        raise
    except Exception:
        conn.rollback()
        raise
    return claim_id, amount


def record_submission(
    conn: sqlite3.Connection,
    claim_id: int,
    tx_hash: str | None,
    last_ledger_seq: int | None,
) -> None:
    """Persist what we know about an in-flight payout.

    Written as early as possible: `last_ledger_seq` is what bounds the
    ambiguous window, so a crash immediately after submit still leaves recovery
    enough to reach a verdict.
    """
    conn.execute(
        "UPDATE brix_claims SET state = 'submitted', tx_hash = COALESCE(?, tx_hash),"
        " last_ledger_seq = COALESCE(?, last_ledger_seq),"
        " updated_at = CURRENT_TIMESTAMP WHERE claim_id = ?",
        (tx_hash, last_ledger_seq, claim_id),
    )
    conn.commit()


def settle_claim(
    conn: sqlite3.Connection,
    claim_id: int,
    outcome: str,
    tx_hash: str | None = None,
) -> None:
    """Apply a payout outcome ("confirmed" | "failed" | "unknown").

    "failed" is the ONLY outcome that unbinds accruals — and it must only ever
    be passed for a validated, definitive failure. "unknown" deliberately
    leaves the rows bound and the claim open: the payment may have landed, and
    unbinding would let the holder claim the same BRIX twice.
    """
    if outcome == "unknown":
        conn.execute(
            "UPDATE brix_claims SET state = 'submitted', tx_hash = COALESCE(?, tx_hash),"
            " updated_at = CURRENT_TIMESTAMP WHERE claim_id = ?",
            (tx_hash, claim_id),
        )
        conn.commit()
        return
    if outcome not in ("confirmed", "failed"):
        raise ValueError(f"unknown claim outcome: {outcome!r}")

    conn.execute(
        "UPDATE brix_claims SET state = ?, tx_hash = COALESCE(?, tx_hash),"
        " updated_at = CURRENT_TIMESTAMP WHERE claim_id = ?",
        (outcome, tx_hash, claim_id),
    )
    if outcome == "failed":
        conn.execute("UPDATE brix_accruals SET claim_id = NULL WHERE claim_id = ?", (claim_id,))
    conn.commit()


def recover(
    conn: sqlite3.Connection,
    finder: Callable[[int], str | None],
    validated_ledger_index: int,
) -> dict[int, str]:
    """Resolve claims left open by a crash, using the chain as the authority.

    For each open claim, `finder(claim_id)` looks for its memo-tagged payout:

    * found            -> confirmed (with the real tx hash).
    * absent, AND the validated ledger has passed the claim's
      LastLedgerSequence -> failed, accruals unbound. Past that ledger the XRPL
      guarantees the transaction can never validate, so absence is proof.
    * anything else (absence while the tx could still land, a NULL
      LastLedgerSequence, or a lookup that raised) -> left untouched.

    That last branch is the important one: absence alone is not failure, and
    guessing would either double-pay or strand a holder's balance. Returns only
    the claims actually transitioned.
    """
    outcomes: dict[int, str] = {}
    rows = conn.execute(
        f"SELECT claim_id, last_ledger_seq FROM brix_claims"  # noqa: S608 — literal tuple below
        f" WHERE state IN ({','.join('?' * len(OPEN_STATES))})",
        OPEN_STATES,
    ).fetchall()

    for row in rows:
        claim_id = int(row[0])
        last_ledger_seq = row[1]
        try:
            tx_hash = finder(claim_id)
        except Exception:
            logging.warning(
                "brix_drip.recover: lookup failed for claim %s", claim_id, exc_info=True
            )
            continue
        if tx_hash:
            settle_claim(conn, claim_id, "confirmed", tx_hash=tx_hash)
            outcomes[claim_id] = "confirmed"
            continue
        if last_ledger_seq is None or validated_ledger_index <= int(last_ledger_seq):
            continue
        settle_claim(conn, claim_id, "failed")
        outcomes[claim_id] = "failed"
    return outcomes


async def recover_from_chain(conn: sqlite3.Connection) -> dict[int, str]:
    """Reconcile every open claim against the ledger.

    Wraps `recover` with the two async lookups it needs. A lookup that FAILED
    stays distinguishable from one that found nothing: conflating them would
    mark a genuinely paid claim failed and unbind it, letting the same BRIX be
    claimed twice — so failures are surfaced as a raising finder, which keeps
    recover()'s "never guess" branch in charge.
    """
    validated = await xrpl_ops.current_validated_ledger_index()
    if validated is None:
        # The "definitively failed" test is unanswerable without a validated
        # ledger index. Do nothing rather than half-decide mid-outage.
        return {}

    open_claims = [
        (int(r[0]), str(r[1]), int(r[2]), None if r[3] is None else int(r[3]))
        for r in conn.execute(
            f"SELECT claim_id, wallet, amount, last_ledger_seq FROM brix_claims"  # noqa: S608
            f" WHERE state IN ({','.join('?' * len(OPEN_STATES))})",
            OPEN_STATES,
        ).fetchall()
    ]

    found: dict[int, str | None] = {}
    failed_lookups: set[int] = set()
    for claim_id, wallet, amount, last_ledger_seq in open_claims:
        try:
            # wallet + amount are what make the memo trustworthy: a payout is
            # only OURS if it went to this claim's wallet for at least this
            # much BRIX, from the distributor, validated and successful.
            found[claim_id] = await xrpl_ops.find_claim_payment(
                claim_id, wallet=wallet, amount=amount, min_ledger=last_ledger_seq
            )
        except Exception:
            logger.warning("brix recover: account_tx lookup failed for claim %s", claim_id)
            failed_lookups.add(claim_id)

    def finder(claim_id: int) -> str | None:
        if claim_id in failed_lookups:
            raise RuntimeError(f"account_tx lookup failed for claim {claim_id}")
        return found.get(claim_id)

    return recover(conn, finder, validated)
