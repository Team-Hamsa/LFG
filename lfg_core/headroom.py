# lfg_core/headroom.py
"""Atomic, durable headroom reservations for MAX_COLLECTION_SIZE (#226).

supply.current_supply reads the LISTENER-populated on-chain index, which lags
real mints — so a raw "supply < MAX" check lets two concurrent jobs near the
cap each see the same headroom and collectively overshoot. This module is the
synchronous overlay that closes that lag window. The index stays the
authority; reservations only cover units the index cannot see yet.

Accounting identity (the no-overshoot proof):
every admitted unit — one a non-cap-exempt bulk job or single-mint session
was granted headroom for — is counted in exactly one place at every instant
from grant until the listener indexes its mint:

  (a) ``headroom_reservations.reserved``  — granted, not yet minted
  (b) ``headroom_pending``                — minted on-chain, not yet indexed
  (c) ``supply.current_supply``           — indexed

``try_reserve`` grants against ``MAX - (c) - ((a)+(b))`` inside one BEGIN
IMMEDIATE transaction (one writer wins; SQLite serializes concurrent
reservers). Prune-then-read ordering makes the handoff (b)->(c) safe: pending
rows already visible in the index are deleted FIRST and the supply count is
read AFTER — the index only ever gains mints, so anything pruned is
guaranteed inside the supply read (a unit can never vanish from both sides =
no undercount), while a mint indexed after the supply read is briefly counted
twice (pending + index) until the next prune — an overcount, which can only
under-admit. Conservative direction: brief double-count admissible, overshoot
never.

Two-jobs-at-the-tail: MAX=10000, indexed supply 9995, jobs A and B each ask
for 5. A's try_reserve sees outstanding 0 -> grants 5. B serializes behind A,
sees outstanding 5 -> available 0 -> grants 0 (CollectionFull). As A mints,
each unit moves (a)->(b) via retire_to_pending, so outstanding stays 5 and a
third job still gets 0 even while the index still reads 9995; once the
listener catches up, prune retires (b) in the same transaction that reads the
newer supply. Total admitted never exceeds 10000.

Crash orphans never leak: ``rebuild`` (called from the service's startup
resume sweep, BEFORE relaunching jobs) drops every row not backed by a live
resumable job record — a job that died between grant and its first persisted
record left no record, so its rows die with it; ``mint:*`` rows (single-mint
sessions, in-memory only) always die on restart except claimants in ``keep``
(sessions started in the race window before the sweep ran). ``headroom_pending``
rows are never dropped by rebuild — they are real on-chain mints and retire
only via prune once indexed.

Failure posture (payment_ledger-style contracts, nothing here ever raises):
a store error grants 0 — fail CLOSED for NEW headroom — while release/retire
failures only leave a reservation held (under-admit) until the next restart's
rebuild. Reads distinguish "provably absent" from "unreadable":
``reserved_for`` and ``outstanding`` are tri-state (None = the read itself
failed), so a transient app-DB lock never masquerades as a vanished grant.
A paid job that PROVABLY lost its reservation converts its units to mint
credits (bulk_mint_flow._fulfill_unit): the user never loses money and the
cap is never overshot.
"""

import logging
import sqlite3
import time
from collections.abc import Iterable

from lfg_core import config, nft_index, supply

# Loop-called precedent (mint_credits.add_credit runs on the event loop):
# keep the busy wait short — contention is a single process's own threads.
_BUSY_TIMEOUT_MS = 5000


def _connect(db: str) -> sqlite3.Connection:
    # isolation_level=None: explicit BEGIN IMMEDIATE / COMMIT control.
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS headroom_reservations ("
        "claimant TEXT PRIMARY KEY, "
        "reserved INTEGER NOT NULL, "
        "created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS headroom_pending ("
        "nft_id TEXT PRIMARY KEY, "
        "created_at INTEGER NOT NULL)"
    )
    return conn


def _prune_pending(conn: sqlite3.Connection, network: str) -> None:
    """Drop pending rows the on-chain index now contains (any burn state — a
    burned mint frees headroom by design). Must run BEFORE the supply read in
    try_reserve (see module docstring). An unreadable index prunes nothing:
    rows stay pending (over-count, under-admit — conservative)."""
    rows = conn.execute("SELECT nft_id FROM headroom_pending").fetchall()
    if not rows:
        return
    indexed: list[tuple[str]] = []
    idx = sqlite3.connect(nft_index.index_db_path(network))
    try:
        for (nft_id,) in rows:
            try:
                hit = idx.execute(
                    "SELECT 1 FROM onchain_nfts WHERE nft_id = ?", (nft_id,)
                ).fetchone()
            except sqlite3.OperationalError:
                return  # unbuilt index: nothing is provably indexed
            if hit:
                indexed.append((nft_id,))
    finally:
        idx.close()
    if indexed:
        conn.executemany("DELETE FROM headroom_pending WHERE nft_id = ?", indexed)


def try_reserve(db: str, claimant: str, qty: int, network: str) -> int:
    """Atomically reserve up to `qty` units of headroom for `claimant`.

    Returns the granted amount (0..qty), computed as
    min(qty, MAX_COLLECTION_SIZE - current_supply - outstanding) inside one
    BEGIN IMMEDIATE transaction — one writer wins, so two concurrent calls can
    never collectively grant past MAX. A claimant reserves at most once (the
    row is claimant-keyed). Store error -> 0: fail closed for new grants."""
    if qty <= 0:
        return 0
    conn = None
    try:
        conn = _connect(db)
        conn.execute("BEGIN IMMEDIATE")
        _prune_pending(conn, network)
        current = supply.current_supply(network)  # AFTER prune — see docstring
        reserved = conn.execute(
            "SELECT COALESCE(SUM(reserved), 0) FROM headroom_reservations"
        ).fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM headroom_pending").fetchone()[0]
        available = config.MAX_COLLECTION_SIZE - current - int(reserved) - int(pending)
        granted = max(0, min(qty, available))
        if granted > 0:
            conn.execute(
                "INSERT INTO headroom_reservations (claimant, reserved, created_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(claimant) DO UPDATE SET reserved = excluded.reserved",
                (claimant, granted, int(time.time())),
            )
        conn.execute("COMMIT")
        return granted
    except Exception:
        logging.exception(f"headroom.try_reserve failed for {claimant}")
        _rollback(conn)
        return 0
    finally:
        if conn is not None:
            conn.close()


def release(db: str, claimant: str, qty: int | None = None) -> None:
    """Give back reservation: `qty` units, or the claimant's whole row when
    None. Idempotent (missing row / already-zero is a no-op); never raises —
    a failed release only under-admits until the next restart's rebuild."""
    conn = None
    try:
        conn = _connect(db)
        conn.execute("BEGIN IMMEDIATE")
        if qty is None:
            conn.execute("DELETE FROM headroom_reservations WHERE claimant = ?", (claimant,))
        else:
            conn.execute(
                "UPDATE headroom_reservations SET reserved = reserved - ? WHERE claimant = ?",
                (qty, claimant),
            )
            conn.execute(
                "DELETE FROM headroom_reservations WHERE claimant = ? AND reserved <= 0",
                (claimant,),
            )
        conn.execute("COMMIT")
    except Exception:
        logging.exception(f"headroom.release failed for {claimant}")
        _rollback(conn)
    finally:
        if conn is not None:
            conn.close()


def retire_to_pending(db: str, claimant: str, nft_id: str) -> None:
    """Move one reserved unit to the pending set the instant its mint lands
    on-chain: the reservation's job is done, but the mint is invisible to
    current_supply until the listener indexes it, so it must keep counting
    against headroom (as a pending row) until prune sees it indexed. One
    transaction; idempotent on nft_id; never raises."""
    conn = None
    try:
        conn = _connect(db)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO headroom_pending (nft_id, created_at) VALUES (?, ?)",
            (nft_id, int(time.time())),
        )
        # Decrement only when the pending row is NEW: a replayed retire for an
        # already-pending nft_id must not shrink the reservation twice.
        if cur.rowcount == 1:
            conn.execute(
                "UPDATE headroom_reservations SET reserved = reserved - 1 "
                "WHERE claimant = ? AND reserved > 0",
                (claimant,),
            )
            conn.execute(
                "DELETE FROM headroom_reservations WHERE claimant = ? AND reserved <= 0",
                (claimant,),
            )
        conn.execute("COMMIT")
    except Exception:
        logging.exception(f"headroom.retire_to_pending failed for {claimant} / {nft_id}")
        _rollback(conn)
    finally:
        if conn is not None:
            conn.close()


def reserved_for(db: str, claimant: str) -> int | None:
    """Units the claimant still holds. Tri-state read (same contract as
    ``outstanding`` / payment_ledger.find_claimed): an int is a SUCCESSFUL
    read — 0 means the row is provably absent (orphan-rebuild dropped it) —
    while None means the read itself failed (e.g. a transient app-DB lock).
    Callers must treat None as UNPROVABLE, never as "gone": fail closed for
    admitting a mint (never mint under an unprovable grant) but retry rather
    than convert a paid, still-valid grant into a mint credit on a blip
    (bulk_mint_flow._fulfill_unit)."""
    conn = None
    try:
        conn = _connect(db)
        row = conn.execute(
            "SELECT reserved FROM headroom_reservations WHERE claimant = ?", (claimant,)
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        logging.exception(f"headroom.reserved_for failed for {claimant}")
        return None
    finally:
        if conn is not None:
            conn.close()


def outstanding(db: str) -> int | None:
    """Total units currently counted against headroom (reserved + pending).
    Tri-state read (payment_ledger.find_claimed style): None = the read
    itself failed — callers must fail toward safety (treat availability as
    unprovable), never as 0."""
    conn = None
    try:
        conn = _connect(db)
        reserved = conn.execute(
            "SELECT COALESCE(SUM(reserved), 0) FROM headroom_reservations"
        ).fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM headroom_pending").fetchone()[0]
        return int(reserved) + int(pending)
    except Exception:
        logging.exception("headroom.outstanding failed")
        return None
    finally:
        if conn is not None:
            conn.close()


def rebuild(
    db: str,
    jobs: Iterable[tuple[str, int, list[str]]],
    keep: Iterable[str] = (),
) -> None:
    """Startup reconstruction (one transaction, never raises): drop every
    reservation row not re-asserted by a live resumable job (`jobs` =
    (claimant, still-reserved units, minted nft_ids), see
    bulk_mint_flow.headroom_snapshot) and not in `keep` (claimants created in
    the startup race window before the resume sweep ran — including live
    single-mint ``mint:*`` sessions, which are in-memory only and therefore
    always orphans from any PREVIOUS process). Minted-but-maybe-unindexed
    units are re-asserted as pending rows (INSERT OR IGNORE — exact, the
    pre-crash retire may or may not have landed); pending rows are never
    dropped here, prune retires them once indexed."""
    conn = None
    try:
        conn = _connect(db)
        conn.execute("BEGIN IMMEDIATE")
        job_list = list(jobs)
        survivors = set(keep) | {claimant for claimant, _, _ in job_list}
        if survivors:
            marks = ",".join("?" for _ in survivors)
            conn.execute(
                f"DELETE FROM headroom_reservations WHERE claimant NOT IN ({marks})",
                tuple(survivors),
            )
        else:
            conn.execute("DELETE FROM headroom_reservations")
        now = int(time.time())
        for claimant, reserved, minted in job_list:
            if reserved > 0:
                conn.execute(
                    "INSERT INTO headroom_reservations (claimant, reserved, created_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(claimant) DO UPDATE SET reserved = excluded.reserved",
                    (claimant, reserved, now),
                )
            else:
                conn.execute("DELETE FROM headroom_reservations WHERE claimant = ?", (claimant,))
            for nft_id in minted:
                conn.execute(
                    "INSERT OR IGNORE INTO headroom_pending (nft_id, created_at) VALUES (?, ?)",
                    (nft_id, now),
                )
        conn.execute("COMMIT")
    except Exception:
        logging.exception("headroom.rebuild failed")
        _rollback(conn)
    finally:
        if conn is not None:
            conn.close()


def _rollback(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
