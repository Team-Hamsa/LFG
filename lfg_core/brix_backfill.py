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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

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
    return {
        r[0]
        for r in conn.execute("SELECT nft_id FROM brix_accruals WHERE epoch_date = ?", (epoch,))
    }


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

        def _listed(nft_id: str, _tokens: dict[str, Any] = tokens) -> bool | None:
            return bool(_tokens[nft_id].listed) if _tokens[nft_id].listed is not None else None

        result = brix_drip.evaluate_accruals(
            list(tokens.values()),
            listed_fn=_listed,
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
        lines.append(
            EpochLine(
                epoch, sum(int(r.amount) for r in fresh), result.skipped_listed, result.unknown
            )
        )
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
