# Durable WalletConnect sign requests (#447). One row per request the app
# asked a Joey session to sign; the client posts the outcome back and the
# server verifies it on-ledger. Lives in the app DB next to identities.
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from lfg_core.user_db import DATABASE as _DEFAULT_DB

DATABASE = _DEFAULT_DB
STATES = ("pending", "signed", "rejected", "failed", "mismatch", "expired", "cancelled", "consumed")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table() -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sign_requests (
                id          TEXT PRIMARY KEY,
                wallet      TEXT NOT NULL,
                purpose     TEXT NOT NULL,
                txjson      TEXT,
                nonce       TEXT,
                state       TEXT NOT NULL DEFAULT 'pending',
                txid        TEXT,
                result_json TEXT,
                ip          TEXT,
                created_at  REAL NOT NULL,
                expires_at  REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sign_requests_wallet ON sign_requests(wallet, state)"
        )
        conn.commit()
    finally:
        conn.close()


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["txjson"] = json.loads(d["txjson"]) if d["txjson"] else None
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    return d


def create(
    *,
    wallet: str,
    purpose: str,
    txjson: dict[str, Any] | None,
    nonce: str | None,
    ttl_seconds: int,
    ip: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    rid = "wc-" + uuid.uuid4().hex
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO sign_requests (id, wallet, purpose, txjson, nonce, state, ip, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                rid,
                wallet,
                purpose,
                json.dumps(txjson) if txjson is not None else None,
                nonce,
                ip,
                now,
                now + ttl_seconds,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    got = get(rid)
    assert got is not None
    return got


def get(request_id: str) -> dict[str, Any] | None:
    conn = _conn()
    try:
        return _row(
            conn.execute("SELECT * FROM sign_requests WHERE id = ?", (request_id,)).fetchone()
        )
    finally:
        conn.close()


def set_state(
    request_id: str,
    state: str,
    *,
    txid: str | None = None,
    result: dict[str, Any] | None = None,
    expect: str | None = "pending",
) -> bool:
    if state not in STATES:
        raise ValueError(f"unknown sign_requests state {state!r}")
    conn = _conn()
    try:
        sql = "UPDATE sign_requests SET state = ?, txid = COALESCE(?, txid), result_json = COALESCE(?, result_json) WHERE id = ?"
        args: list[Any] = [
            state,
            txid,
            json.dumps(result) if result is not None else None,
            request_id,
        ]
        if expect is not None:
            sql += " AND state = ?"
            args.append(expect)
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def expire_stale(now: float | None = None) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE sign_requests SET state = 'expired' WHERE state = 'pending' AND expires_at < ?",
            (now if now is not None else time.time(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
