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
from collections.abc import Callable, Mapping
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
    ineligible: int = 0
    """Replayed tokens outside the collection index (#411 C1) — Closet /
    trait tokens, never drip-eligible."""


@dataclass(frozen=True)
class GapPlan:
    epochs: list[EpochLine]
    total_brix: int
    wallets: dict[str, int]
    nfts: int
    deferred: list[tuple[str, str]]
    written: int = 0
    top: list[tuple[str, int]] = field(default_factory=list)
    owner_drift: list[str] = field(default_factory=list)
    """nft_ids whose replayed owner at `end` disagrees with `onchain_nfts`
    (#411 C2): the derived table is missing events, so the replay would credit
    the wrong wallet. Reported on a dry run; refuses an --apply."""
    refused: str | None = None
    """Set when --apply was asked for but nothing was written."""


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
    eligible: Mapping[str, str | None],
    certify: Callable[[sqlite3.Connection, str, str], str | None] = epoch_state.certify_epoch,
    replay_factory: Callable[[sqlite3.Connection], Any] = epoch_state.EpochReplay,
    top_n: int = 20,
) -> GapPlan:
    """Walk start..end through the same per-epoch path as the nightly job.

    Uncertified epochs are reported and SKIPPED (not a stop — a healed
    historical gap must not hide the epochs after it). On a dry run, rows that
    already exist are subtracted so the report shows what is still owed.

    `apply=True` is REFUSED (nothing written, `refused` set) when the archive
    shows owner drift against the collection index or any unknown listing
    state — both mean `nft_events` is incomplete and the reconstruction would
    misattribute BRIX. Re-derive, then re-run.
    """
    # Owner-drift pre-check (#411 C2), BEFORE anything is written: a second,
    # throwaway replay advanced straight to `end` (one pass, ~0.2s on mainnet)
    # so an --apply can be refused rather than half-applied.
    drift_replay = replay_factory(hconn)
    end_state = drift_replay.advance_to(end)
    owner_drift = sorted(
        nft_id
        for nft_id, tok in end_state.items()
        if nft_id in eligible
        and tok.live
        and eligible[nft_id] is not None
        and tok.owner != eligible[nft_id]
    )

    def _walk(
        write: bool,
    ) -> tuple[list[EpochLine], Counter[str], set[str], list[tuple[str, str]], int]:
        """One pass over the window. `write=False` computes only — the numbers
        the refusal decision needs. `write=True` inserts each epoch's rows as
        it goes, so the whole window's Accruals are never held in memory at
        once (~1.5M rows on mainnet)."""
        replay = replay_factory(hconn)
        lines: list[EpochLine] = []
        wallets: Counter[str] = Counter()
        nft_ids: set[str] = set()
        skipped: list[tuple[str, str]] = []
        inserted = 0
        for epoch in epoch_range(start, end):
            reason = certify(hconn, network, epoch)
            if reason is not None:
                skipped.append((epoch, reason))
                lines.append(EpochLine(epoch, 0, 0, 0, deferred=reason))
                continue
            tokens = replay.advance_to(epoch)
            kept, ineligible = brix_drip.eligible_tokens(tokens, eligible)

            # The replay's tri-state `listed` IS the fail-closed signal
            # evaluate_accruals wants (True/False/None) — passed straight through.
            def _listed(nft_id: str, _tokens: dict[str, Any] = tokens) -> bool | None:
                listed: bool | None = _tokens[nft_id].listed
                return listed

            result = brix_drip.evaluate_accruals(
                kept,
                listed_fn=_listed,
                system_accounts=system_accounts,
                epoch=epoch,
            )
            existing = _already_accrued(hconn, epoch)
            fresh = [r for r in result.rows if r.nft_id not in existing]
            if write:
                # INSERT OR IGNORE keeps this idempotent even though pass 2
                # re-reads `existing` a second time.
                inserted += brix_drip.record_accruals(hconn, fresh)
            for r in fresh:
                wallets[r.owner] += int(r.amount)
                nft_ids.add(r.nft_id)
            lines.append(
                EpochLine(
                    epoch,
                    sum(int(r.amount) for r in fresh),
                    result.skipped_listed,
                    result.unknown,
                    ineligible=ineligible,
                )
            )
        return lines, wallets, nft_ids, skipped, inserted

    # Pass 1 is always a dry walk, so a refusal writes NOTHING. Both refusal
    # conditions mean the archive itself is untrustworthy for this window:
    # missing derived events (owner drift) or unreconstructable listing state.
    lines, wallets, nft_ids, deferred, _ = _walk(write=False)
    unknown_total = sum(line.unknown for line in lines)
    refused: str | None = None
    written = 0
    if apply and owner_drift:
        refused = (
            f"{len(owner_drift)} token(s) replay a different owner than onchain_nfts — "
            f"run scripts/derive_history_events.py --network {network} first"
        )
    elif apply and unknown_total:
        refused = (
            f"{unknown_total} token-epoch(s) have unknown listing state — "
            f"run scripts/derive_history_events.py --network {network} first"
        )
    elif apply:
        # Pass 2: re-walk with a fresh replay, writing per epoch.
        lines, wallets, nft_ids, deferred, written = _walk(write=True)

    total = sum(wallets.values())
    return GapPlan(
        epochs=lines,
        total_brix=total,
        wallets=dict(wallets),
        nfts=len(nft_ids),
        deferred=deferred,
        written=written,
        top=wallets.most_common(top_n),
        owner_drift=owner_drift,
        refused=refused,
    )
