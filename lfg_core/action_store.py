"""Durable XRPL Action sessions and issuer Ticket leases."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

SESSION_STATES = frozenset(
    {
        "preparing",
        "awaiting_signature",
        "confirming",
        "done",
        "rejected",
        "expired",
        "failed",
        "indeterminate",
    }
)
TICKET_STATES = frozenset({"leased", "consumed", "quarantined"})
_JSON_FIELDS = frozenset(
    {"payment_json", "traits_json", "batch_json", "inner_hashes_json"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS xrpl_action_sessions (
  session_id TEXT PRIMARY KEY,
  account TEXT NOT NULL,
  user_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  network TEXT NOT NULL,
  state TEXT NOT NULL,
  campaign TEXT,
  pay_with TEXT,
  pay_amount TEXT,
  payment_json TEXT,
  nft_number INTEGER,
  metadata_url TEXT,
  image_url TEXT,
  video_url TEXT,
  traits_json TEXT,
  body_type TEXT,
  ticket_sequence INTEGER,
  offer_id TEXT,
  batch_json TEXT,
  outer_hash TEXT,
  inner_hashes_json TEXT,
  xumm_uuid TEXT,
  xumm_url TEXT,
  qr_url TEXT,
  last_ledger_sequence INTEGER,
  ledger_index INTEGER,
  nft_id TEXT,
  error_code TEXT,
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS xrpl_action_ticket_leases (
  network TEXT NOT NULL,
  account TEXT NOT NULL,
  ticket_sequence INTEGER NOT NULL,
  session_id TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  leased_at INTEGER NOT NULL,
  last_ledger_sequence INTEGER,
  outer_hash TEXT,
  PRIMARY KEY(network, account, ticket_sequence)
);
"""

_SESSION_COLUMNS = frozenset(
    {
        "state",
        "campaign",
        "pay_with",
        "pay_amount",
        "payment_json",
        "nft_number",
        "metadata_url",
        "image_url",
        "video_url",
        "traits_json",
        "body_type",
        "ticket_sequence",
        "offer_id",
        "batch_json",
        "outer_hash",
        "inner_hashes_json",
        "xumm_uuid",
        "xumm_url",
        "qr_url",
        "last_ledger_sequence",
        "ledger_index",
        "nft_id",
        "error_code",
    }
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def create_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    account: str,
    user_id: str,
    platform: str,
    network: str,
    state: str,
    created_ts: int,
    campaign: str | None = None,
) -> None:
    if state not in SESSION_STATES:
        raise ValueError(f"unknown action state: {state}")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO xrpl_action_sessions"
        " (session_id,account,user_id,platform,network,state,campaign,created_ts,updated_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            account,
            user_id,
            platform,
            network,
            state,
            campaign,
            created_ts,
            created_ts,
        ),
    )
    conn.commit()


def update_session(
    conn: sqlite3.Connection, session_id: str, *, now_ts: int, **changes: Any
) -> None:
    ensure_schema(conn)
    if not changes or not changes.keys() <= _SESSION_COLUMNS:
        raise ValueError("unsupported action session column")
    if "state" in changes and changes["state"] not in SESSION_STATES:
        raise ValueError(f"unknown action state: {changes['state']}")
    sets = ["updated_ts=?"] + [f"{key}=?" for key in changes]
    values = [now_ts]
    for key, value in changes.items():
        values.append(json.dumps(value) if key in _JSON_FIELDS and value is not None else value)
    cursor = conn.execute(
        f"UPDATE xrpl_action_sessions SET {', '.join(sets)} WHERE session_id=?",
        (*values, session_id),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise KeyError(f"unknown XRPL Action session: {session_id}")


def _row_dict(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    result = {column[0]: value for column, value in zip(cursor.description, row)}
    for key in _JSON_FIELDS:
        value = result.get(key)
        if value is not None:
            result[key] = json.loads(value)
    return result


def get_session(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Any] | None:
    ensure_schema(conn)
    cursor = conn.execute(
        "SELECT * FROM xrpl_action_sessions WHERE session_id=?", (session_id,)
    )
    row = cursor.fetchone()
    return _row_dict(cursor, row) if row is not None else None


def list_reconcilable_sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load every Ticket-bearing session whose lease may still need finality."""

    ensure_schema(conn)
    cursor = conn.execute(
        "SELECT * FROM xrpl_action_sessions"
        " WHERE ticket_sequence IS NOT NULL AND state != 'done'"
        " ORDER BY created_ts"
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def lease_ticket(
    conn: sqlite3.Connection,
    network: str,
    account: str,
    available: Sequence[int],
    session_id: str,
    now_ts: int,
) -> int | None:
    """Atomically lease the lowest ledger-available Ticket not already tracked."""

    ensure_schema(conn)
    candidates = sorted(
        value
        for value in set(available)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        used = {
            row[0]
            for row in conn.execute(
                "SELECT ticket_sequence FROM xrpl_action_ticket_leases"
                " WHERE network=? AND account=?",
                (network, account),
            )
        }
        ticket = next((value for value in candidates if value not in used), None)
        if ticket is not None:
            conn.execute(
                "INSERT INTO xrpl_action_ticket_leases"
                " (network,account,ticket_sequence,session_id,state,leased_at)"
                " VALUES (?,?,?,?, 'leased', ?)",
                (network, account, ticket, session_id, now_ts),
            )
        conn.commit()
        return ticket
    except Exception:
        conn.rollback()
        raise


def leased_ticket_sequences(
    conn: sqlite3.Connection, network: str, account: str
) -> set[int]:
    ensure_schema(conn)
    return {
        row[0]
        for row in conn.execute(
            "SELECT ticket_sequence FROM xrpl_action_ticket_leases"
            " WHERE network=? AND account=?",
            (network, account),
        )
    }


def mark_ticket(
    conn: sqlite3.Connection,
    network: str,
    account: str,
    ticket: int,
    *,
    state: str,
    last_ledger_sequence: int | None = None,
    outer_hash: str | None = None,
) -> None:
    if state not in TICKET_STATES:
        raise ValueError(f"unknown ticket state: {state}")
    ensure_schema(conn)
    cursor = conn.execute(
        "UPDATE xrpl_action_ticket_leases"
        " SET state=?, last_ledger_sequence=COALESCE(?,last_ledger_sequence),"
        " outer_hash=COALESCE(?,outer_hash)"
        " WHERE network=? AND account=? AND ticket_sequence=?",
        (
            state,
            last_ledger_sequence,
            outer_hash,
            network,
            account,
            ticket,
        ),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise KeyError(f"unknown issuer Ticket lease: {network}/{account}/{ticket}")


def release_ticket(
    conn: sqlite3.Connection, network: str, account: str, ticket: int
) -> bool:
    """Release only a normal lease; quarantined/consumed rows require audit."""

    ensure_schema(conn)
    cursor = conn.execute(
        "DELETE FROM xrpl_action_ticket_leases"
        " WHERE network=? AND account=? AND ticket_sequence=? AND state='leased'",
        (network, account, ticket),
    )
    conn.commit()
    return cursor.rowcount == 1
