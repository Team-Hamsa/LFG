# lfg_core/closet_reconcile.py
# Detect (and describe the repair for) Closet ownership anomalies: a
# closet_tokens row keyed to a project signing account, or one Closet NFToken
# claimed by more than one owner (#383).
#
# Logic lives here so the nightly audit and the one-off reconciler share it,
# mirroring how supply_reconcile backs scripts/reconcile_supply_*.py. Reads
# only; the reconciler does the writing.

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from lfg_core import economy_store


@dataclass(frozen=True)
class ClosetRow:
    owner: str
    nft_id: str
    status: str


@dataclass(frozen=True)
class ClosetOwnershipReport:
    """`project_rows` are unambiguously bogus and safely deletable: a Closet is
    soulbound user inventory and the issuer never plays. `duplicate_tokens`
    maps an nft_id to every owner claiming it — once the project rows are gone,
    a REMAINING duplicate is user↔user and must not be guessed at (Closet
    tokens are absent from onchain_nfts, so only clio can arbitrate)."""

    project_rows: list[ClosetRow] = field(default_factory=list)
    duplicate_tokens: dict[str, list[str]] = field(default_factory=dict)

    @property
    def unresolved_duplicates(self) -> dict[str, list[str]]:
        """Duplicates that deleting the project rows would NOT resolve."""
        bogus = {r.owner for r in self.project_rows}
        out: dict[str, list[str]] = {}
        for nft_id, owners in self.duplicate_tokens.items():
            remaining = [o for o in owners if o not in bogus]
            if len(remaining) > 1:
                out[nft_id] = remaining
        return out

    @property
    def ok(self) -> bool:
        return not self.project_rows and not self.duplicate_tokens


def audit_closet_ownership(conn: sqlite3.Connection) -> ClosetOwnershipReport:
    """Every closet_tokens anomaly the #383 invariant forbids."""
    accounts = economy_store.project_accounts()
    project_rows = [
        ClosetRow(owner=str(r[0]), nft_id=str(r[1]), status=str(r[2]))
        for r in conn.execute("SELECT owner, nft_id, status FROM closet_tokens ORDER BY owner")
        if str(r[0]) in accounts
    ]
    duplicate_tokens: dict[str, list[str]] = {}
    for nft_id, owners in conn.execute(
        "SELECT nft_id, GROUP_CONCAT(owner) FROM closet_tokens "
        "WHERE nft_id IS NOT NULL GROUP BY nft_id HAVING COUNT(*) > 1"
    ):
        duplicate_tokens[str(nft_id)] = sorted(str(owners).split(","))
    return ClosetOwnershipReport(project_rows=project_rows, duplicate_tokens=duplicate_tokens)


def repair_closet_ownership(conn: sqlite3.Connection, report: ClosetOwnershipReport) -> int:
    """Delete every project-account Closet row (and its loose assets/bodies).
    Returns the number of owners scrubbed. Leaves user↔user duplicates alone —
    `report.unresolved_duplicates` is the caller's cue to stop and investigate."""
    for row in report.project_rows:
        economy_store.delete_closet(conn, row.owner)
    return len(report.project_rows)
