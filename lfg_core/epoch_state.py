"""Owner-of-record and listed-state at an epoch close, replayed from the
history archive (#411 option 2; shared foundation for #412).

Pure over `history_<net>.db`: no network, no XRPL client, no clock. The
nightly drip and the gap backfill both ask the same question — "who held
this token, unlisted, when UTC day D closed?" — and `nft_events` already
records everything needed to answer it back to 2023.

Rules mirror what the live path (`brix_drip.classify_sell_offers`) decides:
a sell offer counts as a listing only while its CREATOR is still the current
holder; destination-locked sell offers count (brokered marketplaces list that
way); buy offers never count.

Fail-closed by construction: any event the replay cannot interpret (a legacy
row with no `offer_index`/`offer_flags`, a cancel that cannot be matched while
offers are open) makes that token's `listed` **None**, which
`brix_drip.evaluate_accruals` never pays. Re-deriving the archive
(`scripts/derive_history_events.py`) populates the columns and clears it.

Scope note (#411 I3): the replay deliberately takes only `hconn` and therefore
enumerates every `nft_id` the archive knows — including soulbound Closet and
tradeable trait tokens, which are NOT drip-eligible. Collection scoping is the
CALLER's job, done against `onchain_nfts` (`nft_index.collection_owners`) in
`brix_drip` / `brix_backfill`, which keeps this module pure over one DB.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from lfg_core import history_store

_LSF_SELL = 1


@dataclass(frozen=True)
class EpochToken:
    """One token's state as of an epoch close."""

    nft_id: str
    owner: str | None
    listed: bool | None
    live: bool

    @property
    def is_burned(self) -> bool:
        return not self.live


def epoch_close_ts(epoch: str) -> int:
    """Unix ts of the instant epoch `epoch` closes — the NEXT day's 00:00:00Z.

    Exclusive upper bound: an event AT this ts belongs to the next epoch."""
    day = datetime.strptime(epoch, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int((day + timedelta(days=1)).timestamp())


@dataclass
class _TokenState:
    owner: str | None = None
    live: bool = False
    unknown: bool = False
    # offer_index -> creator, for OPEN sell offers on this token
    sell_offers: dict[str, str] | None = None

    def offers(self) -> dict[str, str]:
        if self.sell_offers is None:
            self.sell_offers = {}
        return self.sell_offers

    def snapshot(self, nft_id: str) -> EpochToken:
        if self.unknown:
            listed: bool | None = None
        else:
            listed = any(creator == self.owner for creator in self.offers().values())
        return EpochToken(nft_id=nft_id, owner=self.owner, listed=listed, live=self.live)


class EpochReplay:
    """Incremental replay: call `advance_to(epoch)` with non-decreasing epochs
    and it consumes only the events since the previous call. One pass over the
    archive serves a whole window (the #412 backfill walks ~340 epochs)."""

    def __init__(self, hconn: sqlite3.Connection) -> None:
        self._conn = hconn
        self._tokens: dict[str, _TokenState] = {}
        self._consumed_until: int | None = None  # exclusive ts bound already applied

    def advance_to(self, epoch: str) -> dict[str, EpochToken]:
        # Assumption: `close_time` is monotonic non-decreasing in
        # `ledger_index` (true on the XRPL — a ledger never closes before its
        # parent). Under it, slicing the event stream by ts in increasing
        # chunks yields exactly the same end state as one `ts < bound` pass,
        # which is what makes this incremental replay equal the one-shot
        # `state_at_epoch` while costing one archive scan for a whole window.
        bound = epoch_close_ts(epoch)
        if self._consumed_until is not None and bound < self._consumed_until:
            raise ValueError(
                f"EpochReplay cannot rewind: {epoch} is before the last epoch replayed"
            )
        lower = self._consumed_until
        rows = self._conn.execute(
            "SELECT nft_id, event, from_addr, to_addr, offer_index, offer_flags"
            " FROM nft_events WHERE ts < ?"
            + (" AND ts >= ?" if lower is not None else "")
            + " ORDER BY ledger_index, ts, rowid",
            (bound,) if lower is None else (bound, lower),
        )
        for row in rows:
            self._apply(row)
        self._consumed_until = bound
        return {nid: st.snapshot(nid) for nid, st in self._tokens.items()}

    def _apply(self, row: Any) -> None:
        nft_id = row["nft_id"]
        if not nft_id:
            return
        st = self._tokens.setdefault(nft_id, _TokenState())
        event = row["event"]
        if event == "mint":
            st.live = True
            st.owner = row["to_addr"]
            return
        if event in ("transfer", "sale"):
            st.live = True
            if row["to_addr"]:
                st.owner = row["to_addr"]
            self._close_offer(st, row["offer_index"])
            return
        if event == "burn":
            st.live = False
            return
        if event == "offer_create":
            flags = row["offer_flags"]
            if flags is None:
                st.unknown = True  # legacy row: bid or listing? cannot tell
                return
            if not (int(flags) & _LSF_SELL):
                return  # buy offer: never a listing
            if not row["offer_index"]:
                st.unknown = True  # legacy row: can never be matched to its close
                return
            if not st.live:
                st.live = True  # archive predates this token's mint row
            st.offers()[str(row["offer_index"])] = row["from_addr"]
            return
        if event == "offer_cancel":
            self._close_offer(st, row["offer_index"])
            return
        # modify and anything else: no ownership/listing effect

    @staticmethod
    def _close_offer(st: _TokenState, offer_index: Any) -> None:
        if offer_index:
            st.offers().pop(str(offer_index), None)
            return
        # NULL offer_index: we can't tell which offer (if any) closed. That
        # only matters if some OPEN offer's creator equals the CURRENT owner
        # (the only case where it could flip `listed`) — e.g. a cancel/sale
        # closing the current holder's own sell offer. If no open offer
        # matches the current owner, the outcome is unaffected either way and
        # we needn't fail closed.
        if any(creator == st.owner for creator in st.offers().values()):
            st.unknown = True


def state_at_epoch(hconn: sqlite3.Connection, epoch: str) -> dict[str, EpochToken]:
    """Per-`nft_id` owner / listed / live as of the close of `epoch`."""
    return EpochReplay(hconn).advance_to(epoch)


def certify_epoch(hconn: sqlite3.Connection, network: str, epoch: str) -> str | None:
    """Why `epoch` is NOT payable from this archive, or None when it is.

    The replay is only as good as the archive's continuity, so an epoch pays
    only when the same provenance sponsored-mint eligibility fails closed on
    holds: certified baseline, no recorded continuity gap, and the listener has
    validated past the epoch's close (it has SEEN the whole epoch). Anything
    else is deferred — nothing written, cursor left behind — and the next run
    completes it once the listener's auto catch-up (#402) heals the archive.
    """
    state = history_store.get_archive_state(hconn, network)
    if state is None:
        return "no archive_state row"
    if not state.baseline_complete:
        return "baseline not complete"
    if (
        state.continuity_gap_at is not None
        or state.continuity_gap_after is not None
        or state.continuity_gap_before is not None
        or state.continuity_gap_reason is not None
    ):
        return f"continuity gap recorded ({state.continuity_gap_reason or 'unbounded'})"
    close = epoch_close_ts(epoch)
    if state.validated_close_time is None or state.validated_close_time < close:
        seen = (
            datetime.fromtimestamp(state.validated_close_time, tz=timezone.utc).isoformat()
            if state.validated_close_time is not None
            else "never"
        )
        return f"archive validated through {seen} — epoch {epoch} not yet closed in archive"
    return None
