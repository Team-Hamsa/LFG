# lfg_core/free_mint.py
# One free mint per platform-identity (see
# docs/superpowers/specs/2026-07-13-free-mint-newcomers-design.md).
# Reserve -> confirm/release claim ledger + newcomer eligibility. Reads
# identity/wallet history and the on-chain ownership index via raw SQL so
# lfg_core never imports lfg_service.

import logging
import os
import sqlite3

from lfg_core import config, nft_index
from lfg_core.user_db import DATABASE

_ACTIVE = ("reserved", "claimed")


def ensure_tables() -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS free_mint_claims (
                platform          TEXT NOT NULL,
                platform_user_id  TEXT NOT NULL,
                network           TEXT NOT NULL,
                wallet            TEXT NOT NULL,
                nft_number        INTEGER,
                status            TEXT NOT NULL,
                claimed_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, platform_user_id, network)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def wallets_for_identity(platform: str, platform_user_id: str) -> set[str]:
    conn = sqlite3.connect(DATABASE)
    try:
        wallets = {
            r[0]
            for r in conn.execute(
                "SELECT wallet FROM wallet_links WHERE platform = ? AND platform_user_id = ?",
                (platform, platform_user_id),
            )
        }
        row = conn.execute(
            "SELECT wallet FROM identities WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        ).fetchone()
        if row and row[0]:
            wallets.add(row[0])
        return wallets
    finally:
        conn.close()


def _active_claim_count(conn: sqlite3.Connection, network: str) -> int:
    """Number of free mints already handed out (reserved + claimed) on this
    network. A released reservation is deleted, so it frees a slot."""
    placeholders = ",".join("?" for _ in _ACTIVE)
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM free_mint_claims "
            f"WHERE network = ? AND status IN ({placeholders})",
            (network, *_ACTIVE),
        ).fetchone()[0]
    )


def active_claim_count(network: str) -> int:
    conn = sqlite3.connect(DATABASE)
    try:
        return _active_claim_count(conn, network)
    finally:
        conn.close()


def cap_reached(network: str) -> bool:
    """True once the giveaway cap (config.FREE_MINT_CAP) is exhausted for this
    network. Fail closed: a DB error reads as 'cap reached' (no free mint)."""
    try:
        return active_claim_count(network) >= config.FREE_MINT_CAP
    except Exception as e:
        logging.warning(f"free_mint.cap_reached fail-closed: {e}")
        return True


def _has_active_claim(conn: sqlite3.Connection, platform: str, uid: str, network: str) -> bool:
    row = conn.execute(
        "SELECT status FROM free_mint_claims "
        "WHERE platform = ? AND platform_user_id = ? AND network = ?",
        (platform, uid, network),
    ).fetchone()
    return bool(row) and row[0] in _ACTIVE


def _owns_live_character(wallets: set[str], network: str) -> bool:
    if not wallets:
        return False
    path = nft_index.index_db_path(network)
    # Fail closed: a missing index is "unknown ownership", not "owns nothing".
    # init_db would silently create an empty DB, so guard before opening.
    if not os.path.exists(path):
        raise FileNotFoundError(f"on-chain index not found: {path}")
    conn = nft_index.init_db(path)
    try:
        placeholders = ",".join("?" for _ in wallets)
        n = conn.execute(
            f"SELECT COUNT(*) FROM onchain_nfts WHERE is_burned = 0 AND owner IN ({placeholders})",
            tuple(wallets),
        ).fetchone()[0]
        return bool(n > 0)
    finally:
        conn.close()


def is_eligible(platform: str, platform_user_id: str, network: str) -> bool:
    """Fail closed: any error (missing index, DB fault) -> not eligible."""
    try:
        conn = sqlite3.connect(DATABASE)
        try:
            if _has_active_claim(conn, platform, platform_user_id, network):
                return False
            # Giveaway cap: no free mints left on this network. Authoritative
            # enforcement is atomic in reserve_claim; this is the UX-facing
            # gate so a capped-out user is shown the paid path up front.
            if _active_claim_count(conn, network) >= config.FREE_MINT_CAP:
                return False
        finally:
            conn.close()
        wallets = wallets_for_identity(platform, platform_user_id)
        return not _owns_live_character(wallets, network)
    except Exception as e:
        # Don't log the per-user identifiers on the fail-closed path (matches
        # identity.link's error log, which also omits them).
        logging.warning(f"free_mint.is_eligible fail-closed for platform={platform}: {e}")
        return False


def reserve_claim(platform: str, platform_user_id: str, network: str, wallet: str) -> bool:
    """Atomically reserve the single claim row, enforcing the giveaway cap.
    True iff this call created the reservation.

    The count-then-insert runs inside a single BEGIN IMMEDIATE transaction so
    the write lock serializes concurrent reservers: two identities racing at
    cap-1 cannot both read "under cap" and both insert (which would overshoot
    config.FREE_MINT_CAP). Returns False when the identity already has a claim
    (PK conflict) OR the cap is full."""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.isolation_level = None  # manage the transaction explicitly
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _active_claim_count(conn, network) >= config.FREE_MINT_CAP:
                conn.execute("ROLLBACK")
                return False
            cur = conn.execute(
                "INSERT OR IGNORE INTO free_mint_claims "
                "(platform, platform_user_id, network, wallet, status) "
                "VALUES (?, ?, ?, ?, 'reserved')",
                (platform, platform_user_id, network, wallet),
            )
            won = cur.rowcount == 1
            conn.execute("COMMIT")
            return won
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def confirm_claim(
    platform: str, platform_user_id: str, network: str, wallet: str, nft_number: int
) -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        cur = conn.execute(
            "UPDATE free_mint_claims SET status='claimed', wallet=?, nft_number=?, "
            "claimed_at=CURRENT_TIMESTAMP "
            "WHERE platform=? AND platform_user_id=? AND network=?",
            (wallet, nft_number, platform, platform_user_id, network),
        )
        conn.commit()
        if cur.rowcount == 0:
            # The reserved row vanished before we could confirm it (e.g. an
            # admin revoke raced this in-flight mint). The free NFT was minted
            # but is now unrecorded, silently reopening eligibility — surface it
            # loudly rather than let the "one free mint" guarantee break quietly.
            logging.warning(
                "free_mint.confirm_claim recorded no row (network=%s, nft=%s) — "
                "reserved claim gone, eligibility may reopen",
                network,
                nft_number,
            )
    finally:
        conn.close()


def release_claim(platform: str, platform_user_id: str, network: str) -> None:
    """Free a reserved claim so the identity can retry. Only releases a still-
    reserved row; a confirmed claim is permanent."""
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            "DELETE FROM free_mint_claims "
            "WHERE platform=? AND platform_user_id=? AND network=? AND status='reserved'",
            (platform, platform_user_id, network),
        )
        conn.commit()
    finally:
        conn.close()
