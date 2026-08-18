# lfg_core/mint_credits.py
# Last-resort tail for bulk minting (#215): a unit that is permanently
# undeliverable (cap-hit race, exhausted retries) becomes a durable credit
# the user can redeem later with no re-payment.
#
# Idempotent crediting (#220 round 5): callers that may legitimately retry
# the same credit (burn2mint orphan-payload recovery, whose crash window sits
# between add_credit committing and the journal being retired) pass a
# `source_key` — a stable identifier for the credited event (e.g. the orphan
# payload uuid). The key is recorded in a dedup ledger (`mint_credit_sources`)
# in the SAME transaction as the credit upsert, so re-running recovery for the
# same event is a no-op instead of a duplicate credit. Existing call sites
# (the bulk-mint lost-reservation tail) pass no key and keep their additive
# behavior unchanged — each of their calls is a distinct failed unit, not a
# retry of a prior credit.
import sqlite3


def ensure_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mint_credits ("
            "discord_id TEXT NOT NULL, network TEXT NOT NULL, "
            "credits INTEGER NOT NULL DEFAULT 0, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (discord_id, network))"
        )
        # Self-migrating dedup ledger for source-keyed credits (see module
        # docstring): one row per already-credited event.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mint_credit_sources ("
            "source_key TEXT PRIMARY KEY, "
            "discord_id TEXT NOT NULL, network TEXT NOT NULL, "
            "credits INTEGER NOT NULL, "
            "credited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.commit()
    finally:
        conn.close()


def add_credit(
    db_path: str, discord_id: str, network: str, n: int = 1, source_key: str | None = None
) -> int:
    """Add n credits for (discord_id, network); returns the new balance.

    With `source_key`, the credit is IDEMPOTENT on that key: the dedup row and
    the balance upsert commit in one transaction, so a retry for an
    already-credited event changes nothing (and returns the current balance).
    Without a key, behavior is the original additive upsert."""
    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if source_key is not None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO mint_credit_sources "
                "(source_key, discord_id, network, credits) VALUES (?,?,?,?)",
                (source_key, discord_id, network, n),
            )
            if cur.rowcount == 0:
                # Already credited for this event: no-op.
                conn.commit()
                row = conn.execute(
                    "SELECT credits FROM mint_credits WHERE discord_id=? AND network=?",
                    (discord_id, network),
                ).fetchone()
                return int(row[0]) if row else 0
        conn.execute(
            "INSERT INTO mint_credits (discord_id, network, credits) VALUES (?,?,?) "
            "ON CONFLICT(discord_id, network) DO UPDATE SET "
            "credits = credits + excluded.credits, updated_at = CURRENT_TIMESTAMP",
            (discord_id, network, n),
        )
        conn.commit()
        row = conn.execute(
            "SELECT credits FROM mint_credits WHERE discord_id=? AND network=?",
            (discord_id, network),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def get_credits(db_path: str, discord_id: str, network: str) -> int:
    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT credits FROM mint_credits WHERE discord_id=? AND network=?",
            (discord_id, network),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
