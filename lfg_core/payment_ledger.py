"""Consumed-payment ledger (issue #196).

Records the tx hash of every on-ledger payment that has satisfied a
wait_for_payment call, so that (a) one payment can never satisfy two
sessions and (b) a payment that arrives with no session listening — a
duplicate sign of the reusable static link, or a payment validating just
after the session timed out — survives as a credit the sender's next
session consumes, instead of being silently kept.

The bootstrap floor is the moment this ledger first initialised: payments
validated before it predate consumed-tracking (they were matched but never
recorded), so they are never spendable as credits.
"""

import logging
import sqlite3
import time

from lfg_core import config


def _db_path() -> str:
    return config.DB_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consumed_payments (
            tx_hash     TEXT PRIMARY KEY,
            sender      TEXT NOT NULL,
            destination TEXT NOT NULL,
            consumed_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_ledger_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO payment_ledger_meta (key, value) VALUES ('bootstrap_ts', ?)",
        (str(int(time.time())),),
    )
    # Self-migrating claimant tag (#228): try_consume durably commits BEFORE
    # the claiming flow can persist its own paid state, so a crash in that gap
    # leaves the claim recorded only here. Tagging the row with an exact
    # claimant id (e.g. "bulk:<job_id>") lets the resumed flow recognise its
    # own pre-crash claim instead of reading the dedup miss as "never paid".
    cols = {row[1] for row in conn.execute("PRAGMA table_info(consumed_payments)")}
    if "claimant" not in cols:
        conn.execute("ALTER TABLE consumed_payments ADD COLUMN claimant TEXT")
    conn.commit()
    return conn


def init_ledger() -> None:
    """Create the ledger tables and stamp the bootstrap floor on first run."""
    _connect().close()


def bootstrap_floor() -> float:
    """Unix time before which payments are never eligible as credits."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM payment_ledger_meta WHERE key = 'bootstrap_ts'"
        ).fetchone()
        return float(row[0])
    finally:
        conn.close()


def try_consume(tx_hash: str, sender: str, destination: str, claimant: str | None = None) -> bool:
    """Atomically claim a payment by tx hash. Returns True if this call
    consumed it, False if it was already consumed. `claimant` (optional, #228)
    tags the row with the exact flow that claimed it so a crash-resumed flow
    can find its own claim via find_claimed."""
    conn = None
    try:
        conn = _connect()
        cur = conn.execute(
            "INSERT OR IGNORE INTO consumed_payments"
            " (tx_hash, sender, destination, consumed_at, claimant) VALUES (?, ?, ?, ?, ?)",
            (tx_hash, sender, destination, int(time.time()), claimant),
        )
        conn.commit()
        consumed = cur.rowcount == 1
        if consumed:
            logging.info(f"Consumed payment {tx_hash} from {sender} to {destination}")
        return consumed
    except sqlite3.Error:
        # Fail closed: if we cannot prove the claim, do not mint against it.
        logging.exception(f"payment_ledger.try_consume failed for {tx_hash}")
        return False
    finally:
        if conn is not None:
            conn.close()


def find_claimed(claimant: str) -> bool:
    """True if any consumed-payment row carries this claimant tag (#228).

    try_consume commits durably before the claiming flow can persist its own
    paid state, so a crash/cancel in that gap leaves the claim visible only
    here — the resumed flow's re-watch then misses the payment (dedup) and
    must reconcile against this before terminalizing as unpaid. The tag is
    exact (one flow instance), so a match can never be another session's
    claim. Returns False — no proof of payment — on DB error."""
    conn = None
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT 1 FROM consumed_payments WHERE claimant = ? LIMIT 1", (claimant,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        logging.exception(f"payment_ledger.find_claimed failed for {claimant}")
        return False
    finally:
        if conn is not None:
            conn.close()
