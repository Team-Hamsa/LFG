"""Persistent campaign, eligibility, claim, and burn-obligation store."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import quote
from uuid import uuid4

from lfg_core import config, db_path, history_events, history_store, market_ops

CampaignState = Literal["off", "active", "stopped", "expired", "full", "at_capacity"]
ReservationReason = Literal[
    "invalid_request",
    "wrong_network",
    "invalid_topology",
    "eligibility_unavailable",
    "ineligible",
    "campaign_off",
    "campaign_expired",
    "at_capacity",
    "already_reserved",
    "already_consumed",
    "reserved",
]
ClaimStatus = Literal[
    "reserved",
    "minting",
    "minted",
    "offered",
    "accepted",
    "released",
    "failed_terminal",
]
BurnStatus = Literal["pending", "submitting", "indeterminate", "burned", "failed_terminal"]

_ACTIVE_CLAIM_STATES = ("reserved", "minting", "minted", "offered", "accepted")
_CONSUMED_CLAIM_STATES = ("minted", "offered", "accepted", "failed_terminal")
_CAMPAIGN_DURATION_SECONDS = config.SPONSORED_MINT_DURATION_SECONDS
_CAMPAIGN_CAP = config.SPONSORED_MINT_CAP
SUPPORTED_NETWORKS = frozenset(("mainnet", "testnet"))

# The eligibility rule is "this wallet has never submitted a SourceTag-carrying
# transaction", and the historical baseline can only prove that if every
# backfill source was swept. A certification narrowed to fewer sources attests
# less than this archive is trusted to prove, so the coverage document must
# record the full set and the runtime gate refuses anything narrower (#331).
BASELINE_REQUIRED_SOURCES = frozenset(
    ("issuer", "brix", "token_issuer", "signing", "distributor", "nfts")
)
# Version 2 added the `sources` attestation; older documents cannot carry it
# and are rejected rather than trusted.
BASELINE_COVERAGE_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS free_mint_campaigns (
    id            TEXT PRIMARY KEY,
    network       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (
        status IN ('active', 'stopped', 'expired', 'full')
    ),
    started_at    INTEGER NOT NULL,
    enabled_until INTEGER NOT NULL,
    stopped_at    INTEGER,
    started_by    TEXT NOT NULL,
    stopped_by    TEXT,
    cap           INTEGER NOT NULL CHECK (cap > 0),
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_free_mint_one_active_campaign
    ON free_mint_campaigns(network) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_free_mint_campaigns_network
    ON free_mint_campaigns(network, started_at DESC);

CREATE TABLE IF NOT EXISTS free_mint_claims (
    id                     TEXT PRIMARY KEY,
    network                TEXT NOT NULL,
    wallet                 TEXT NOT NULL,
    campaign_id            TEXT NOT NULL REFERENCES free_mint_campaigns(id),
    session_id             TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (
        status IN (
            'reserved', 'minting', 'minted', 'offered',
            'accepted', 'released', 'failed_terminal'
        )
    ),
    reserved_at            INTEGER NOT NULL,
    reservation_expires_at INTEGER,
    released_at            INTEGER,
    mint_tx_hash           TEXT,
    mint_signed_tx_hash      TEXT,
    mint_signed_tx_blob      TEXT,
    mint_signed_ledger_floor INTEGER,
    mint_forwarded_at        INTEGER,
    mint_nft_number          INTEGER,
    mint_metadata_url        TEXT,
    mint_metadata_json       TEXT,
    mint_body_type           TEXT,
    mint_still_token         TEXT,
    nft_id                 TEXT,
    offer_id               TEXT,
    accept_tx_hash         TEXT,
    tagged_at              INTEGER,
    last_error             TEXT,
    created_at             INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL,
    UNIQUE (network, wallet)
);
CREATE INDEX IF NOT EXISTS idx_free_mint_claims_campaign_status
    ON free_mint_claims(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_free_mint_claims_session
    ON free_mint_claims(network, session_id);

CREATE TABLE IF NOT EXISTS free_mint_burns (
    id              TEXT PRIMARY KEY,
    claim_id        TEXT NOT NULL UNIQUE REFERENCES free_mint_claims(id),
    status          TEXT NOT NULL CHECK (
        status IN ('pending', 'submitting', 'indeterminate', 'burned', 'failed_terminal')
    ),
    memo_id         TEXT NOT NULL UNIQUE,
    amount          TEXT NOT NULL,
    source_account  TEXT NOT NULL,
    tx_hash         TEXT,
    network         TEXT NOT NULL,
    issuer          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    source_tag      INTEGER NOT NULL,
    signed_tx_hash  TEXT,
    signed_tx_blob  TEXT,
    signed_ledger_floor INTEGER,
    fulfillment     TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER,
    next_attempt_at INTEGER,
    lease_until     INTEGER,
    lease_token     TEXT,
    last_error      TEXT,
    burned_at       INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_free_mint_burns_work
    ON free_mint_burns(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS free_mint_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    network     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    at          INTEGER NOT NULL,
    campaign_id TEXT,
    result      TEXT NOT NULL,
    details     TEXT
);
CREATE INDEX IF NOT EXISTS idx_free_mint_audit_campaign
    ON free_mint_audit(campaign_id, at);
"""


@dataclass(frozen=True)
class CampaignStatus:
    campaign_id: str | None
    network: str
    state: CampaignState
    started_at: int | None
    enabled_until: int | None
    stopped_at: int | None
    cap: int
    reserved: int
    minting: int
    minted: int
    offered: int
    accepted: int
    burn_noop: int
    burn_pending: int
    burn_burned: int
    burn_retryable: int
    burn_indeterminate: int
    burn_terminal: int
    remaining: int
    countdown_seconds: int
    eligibility_available: bool | None = None
    unique_tagged_wallets: int | None = None

    tagged_sponsored_wallets: int = 0
    unique_target: int = 300
    last_operator: str | None = None
    changed_at: int | None = None


@dataclass(frozen=True)
class Claim:
    id: str
    network: str
    wallet: str
    campaign_id: str
    session_id: str
    status: ClaimStatus
    reserved_at: int
    reservation_expires_at: int | None
    released_at: int | None
    mint_signed_tx_hash: str | None
    mint_signed_tx_blob: str | None
    mint_signed_ledger_floor: int | None
    mint_forwarded_at: int | None
    mint_nft_number: int | None
    mint_metadata_url: str | None
    mint_metadata_json: str | None
    mint_body_type: str | None
    mint_still_token: str | None
    mint_tx_hash: str | None
    nft_id: str | None
    offer_id: str | None
    accept_tx_hash: str | None
    tagged_at: int | None
    last_error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class BurnObligation:
    id: str
    claim_id: str
    status: BurnStatus
    memo_id: str
    amount: str
    signed_tx_hash: str | None
    network: str
    issuer: str
    currency: str
    source_tag: int
    signed_tx_blob: str | None
    signed_ledger_floor: int | None
    fulfillment: str | None
    source_account: str
    tx_hash: str | None
    attempt_count: int
    last_attempt_at: int | None
    next_attempt_at: int | None
    lease_until: int | None
    lease_token: str | None
    last_error: str | None
    burned_at: int | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class ReservationResult:
    sponsored: bool
    reason: ReservationReason
    claim: Claim | None


@dataclass(frozen=True)
class MintingResult:
    """Outcome of the reserved -> minting compare-and-set.

    `started` is true only for the caller that performed the transition.
    A false value means the claim was already irreversible and must be
    recovered, never submitted again.
    """

    claim: Claim
    started: bool


@dataclass(frozen=True)
class RecoveryReport:
    """Durable work discovered and safely classified during service startup."""

    network: str
    campaign_state: CampaignState
    released_reservations: tuple[str, ...]
    held_minting: tuple[str, ...]
    recovered_mints: tuple[str, ...]
    missing_offers: tuple[str, ...]
    reclaimable_burns: tuple[str, ...]
    debt_count: int


def _timestamp(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


def _require_supported_network(network: str) -> None:
    if network not in SUPPORTED_NETWORKS:
        raise ValueError(f"unsupported sponsored mint network: {network}")


def _topology_is_valid(network: str) -> bool:
    return network != "mainnet" or config.SIGNING_ACCOUNT != config.TOKEN_ISSUER_ADDRESS


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(db_path: str) -> None:
    """Create and forward-migrate the sponsored-mint schema in place."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(free_mint_burns)")}
        claim_columns = {row["name"] for row in conn.execute("PRAGMA table_info(free_mint_claims)")}
        claim_migrations = {
            "mint_signed_tx_hash": "TEXT",
            "mint_signed_tx_blob": "TEXT",
            "mint_signed_ledger_floor": "INTEGER",
            "mint_forwarded_at": "INTEGER",
            "mint_nft_number": "INTEGER",
            "mint_metadata_url": "TEXT",
            "mint_metadata_json": "TEXT",
            "mint_body_type": "TEXT",
            "mint_still_token": "TEXT",
        }
        for column, declaration in claim_migrations.items():
            if column not in claim_columns:
                conn.execute(f"ALTER TABLE free_mint_claims ADD COLUMN {column} {declaration}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_free_mint_claims_signed_hash "
            "ON free_mint_claims(mint_signed_tx_hash) WHERE mint_signed_tx_hash IS NOT NULL"
        )
        burn_migrations = {
            # The first deployed schema did not have these two columns. Add
            # them before any query or index assumes a later schema version.
            "amount": "TEXT",
            "source_account": "TEXT",
            "network": "TEXT",
            "issuer": "TEXT",
            "currency": "TEXT",
            "source_tag": "INTEGER",
            "lease_until": "INTEGER",
            "lease_token": "TEXT",
            "signed_tx_hash": "TEXT",
            "signed_tx_blob": "TEXT",
            "signed_ledger_floor": "INTEGER",
            "fulfillment": "TEXT",
        }
        for column, declaration in burn_migrations.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE free_mint_burns ADD COLUMN {column} {declaration}")
        conn.execute(
            "UPDATE free_mint_burns SET amount = COALESCE(amount, ?), "
            "source_account = COALESCE(source_account, ?), "
            "network = COALESCE(network, (SELECT network FROM free_mint_claims "
            "WHERE id = free_mint_burns.claim_id), ?), "
            "issuer = COALESCE(issuer, ?), currency = COALESCE(currency, ?), "
            "source_tag = COALESCE(source_tag, ?)",
            (
                config.MINT_PRICE_LFGO,
                config.SIGNING_ACCOUNT,
                config.XRPL_NETWORK,
                config.TOKEN_ISSUER_ADDRESS,
                config.TOKEN_CURRENCY_HEX,
                config.SOURCE_TAG,
            ),
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_free_mint_burns_signed_hash
            ON free_mint_burns(signed_tx_hash)
            WHERE signed_tx_hash IS NOT NULL
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_free_mint_burns_network_work "
            "ON free_mint_burns(network, status, next_attempt_at)"
        )
        legacy = conn.execute(
            """
            SELECT id, claim_id
            FROM free_mint_burns
            WHERE status = 'pending'
              AND attempt_count = 0
              AND last_attempt_at IS NULL
              AND tx_hash IS NULL
              AND signed_tx_hash IS NULL
              AND signed_tx_blob IS NULL
              AND signed_ledger_floor IS NULL
              AND memo_id = 'fm-' || substr(claim_id, 1, 16)
            """
        ).fetchall()
        for row in legacy:
            conn.execute(
                "UPDATE free_mint_burns SET memo_id = ? WHERE id = ?",
                (sponsored_burn_memo_id(row["claim_id"]), row["id"]),
            )


def sponsored_burn_memo_id(claim_id: str) -> str:
    """Encode all 128 claim UUID bits in a campaign-safe, 29-character slug."""

    try:
        raw = bytes.fromhex(claim_id)
    except ValueError as exc:
        raise ValueError("claim id must be a hexadecimal UUID") from exc
    if len(raw) != 16:
        raise ValueError("claim id must contain exactly 128 bits")
    encoded = base64.b32encode(raw).decode("ascii").rstrip("=").lower()
    memo_id = f"fm-{encoded}"
    assert len(memo_id) <= 32
    return memo_id


def _claim(row: sqlite3.Row | None) -> Claim | None:
    if row is None:
        return None
    columns = set(row.keys())
    return Claim(
        id=row["id"],
        network=row["network"],
        wallet=row["wallet"],
        campaign_id=row["campaign_id"],
        session_id=row["session_id"],
        status=row["status"],
        reserved_at=row["reserved_at"],
        reservation_expires_at=row["reservation_expires_at"],
        released_at=row["released_at"],
        mint_tx_hash=row["mint_tx_hash"],
        nft_id=row["nft_id"],
        mint_signed_tx_hash=row["mint_signed_tx_hash"]
        if "mint_signed_tx_hash" in columns
        else None,
        mint_signed_tx_blob=row["mint_signed_tx_blob"]
        if "mint_signed_tx_blob" in columns
        else None,
        mint_signed_ledger_floor=row["mint_signed_ledger_floor"]
        if "mint_signed_ledger_floor" in columns
        else None,
        mint_forwarded_at=row["mint_forwarded_at"] if "mint_forwarded_at" in columns else None,
        mint_nft_number=row["mint_nft_number"] if "mint_nft_number" in columns else None,
        mint_metadata_url=row["mint_metadata_url"] if "mint_metadata_url" in columns else None,
        mint_metadata_json=row["mint_metadata_json"] if "mint_metadata_json" in columns else None,
        mint_body_type=row["mint_body_type"] if "mint_body_type" in columns else None,
        mint_still_token=row["mint_still_token"] if "mint_still_token" in columns else None,
        offer_id=row["offer_id"],
        accept_tx_hash=row["accept_tx_hash"],
        tagged_at=row["tagged_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _audit(
    conn: sqlite3.Connection,
    *,
    network: str,
    actor: str,
    action: str,
    at: int,
    campaign_id: str | None,
    result: str,
    details: Mapping[str, object] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO free_mint_audit (
            network, actor, action, at, campaign_id, result, details
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            network,
            actor,
            action,
            at,
            campaign_id,
            result,
            json.dumps(details, sort_keys=True) if details is not None else None,
        ),
    )


def _latest_campaign(conn: sqlite3.Connection, network: str) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT id, network, status, started_at, enabled_until, stopped_at,
                   started_by, stopped_by, cap, created_at, updated_at
            FROM free_mint_campaigns
            WHERE network = ?
            ORDER BY started_at DESC, created_at DESC
            LIMIT 1
            """,
            (network,),
        ).fetchone(),
    )


def _claim_counts(conn: sqlite3.Connection, campaign_id: str) -> dict[str, int]:
    counts = dict.fromkeys(_ACTIVE_CLAIM_STATES, 0)
    for row in conn.execute(
        """
        SELECT status, count(*) AS count
        FROM free_mint_claims
        WHERE campaign_id = ?
          AND (
              status != 'accepted'
              OR (accept_tx_hash IS NOT NULL AND tagged_at IS NOT NULL)
          )
        GROUP BY status
        """,
        (campaign_id,),
    ):
        if row["status"] in counts:
            counts[row["status"]] = row["count"]
    return counts


def _effective_campaign(
    conn: sqlite3.Connection, network: str, now: int
) -> tuple[sqlite3.Row | None, CampaignState, dict[str, int]]:
    campaign = _latest_campaign(conn, network)
    if campaign is None:
        return None, "off", dict.fromkeys(_ACTIVE_CLAIM_STATES, 0)

    counts = _claim_counts(conn, campaign["id"])
    state: CampaignState = campaign["status"]
    if state == "active" and now >= campaign["enabled_until"]:
        conn.execute(
            """
            UPDATE free_mint_campaigns
            SET status = 'expired', updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, campaign["id"]),
        )
        _audit(
            conn,
            network=network,
            actor="system",
            action="campaign_expired",
            at=now,
            campaign_id=campaign["id"],
            result="expired",
        )
        campaign = _latest_campaign(conn, network)
        assert campaign is not None
        state = "expired"

    consumed = counts["minted"] + counts["offered"] + counts["accepted"]
    if state == "active" and consumed >= campaign["cap"]:
        conn.execute(
            """
            UPDATE free_mint_campaigns
            SET status = 'full', updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, campaign["id"]),
        )
        _audit(
            conn,
            network=network,
            actor="system",
            action="campaign_full",
            at=now,
            campaign_id=campaign["id"],
            result="full",
        )
        campaign = _latest_campaign(conn, network)
        state = "full"
    elif state == "active" and sum(counts.values()) >= campaign["cap"]:
        state = "at_capacity"
    return campaign, state, counts


def _burn_counts(conn: sqlite3.Connection, campaign_id: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT b.status, b.fulfillment, COUNT(*) AS count
        FROM free_mint_burns AS b
        JOIN free_mint_claims AS c ON c.id = b.claim_id
        WHERE c.campaign_id = ?
        GROUP BY b.status, b.fulfillment
        """,
        (campaign_id,),
    ).fetchall()
    counts = {"retryable": 0, "indeterminate": 0, "terminal": 0, "burned": 0, "noop": 0}
    for row in rows:
        count = int(row["count"] or 0)
        if row["status"] in ("pending", "submitting"):
            counts["retryable"] += count
        elif row["status"] == "indeterminate":
            counts["indeterminate"] += count
        elif row["status"] == "failed_terminal":
            counts["terminal"] += count
        elif row["status"] == "burned" and row["fulfillment"] == "ledger_burn":
            counts["burned"] += count
        elif row["status"] == "burned" and row["fulfillment"] == "self_issuer_noop":
            counts["noop"] += count
    return counts


def _tagged_sponsored_wallet_count(conn: sqlite3.Connection, campaign_id: str) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS count
        FROM free_mint_claims
        WHERE campaign_id = ? AND status = 'accepted' AND tagged_at IS NOT NULL
        """,
        (campaign_id,),
    ).fetchone()
    return int(row["count"] or 0)


def _last_campaign_operator(
    conn: sqlite3.Connection, network: str, campaign_id: str
) -> tuple[str | None, int | None]:
    row = conn.execute(
        """
        SELECT actor, at
        FROM free_mint_audit
        WHERE network = ? AND campaign_id = ?
          AND action IN ('campaign_start', 'campaign_stop', 'campaign_expired', 'campaign_full')
        ORDER BY at DESC, id DESC
        LIMIT 1
        """,
        (network, campaign_id),
    ).fetchone()
    return (row["actor"], row["at"]) if row is not None else (None, None)


def _status(
    conn: sqlite3.Connection,
    *,
    network: str,
    now: int,
    eligibility_available: bool | None = None,
    unique_tagged_wallets: int | None = None,
) -> CampaignStatus:
    campaign, state, counts = _effective_campaign(conn, network, now)
    if campaign is None:
        return CampaignStatus(
            campaign_id=None,
            network=network,
            state="off",
            started_at=None,
            enabled_until=None,
            stopped_at=None,
            cap=_CAMPAIGN_CAP,
            reserved=0,
            minting=0,
            minted=0,
            offered=0,
            accepted=0,
            burn_pending=0,
            burn_burned=0,
            burn_noop=0,
            remaining=0,
            countdown_seconds=0,
            eligibility_available=eligibility_available,
            burn_retryable=0,
            burn_indeterminate=0,
            burn_terminal=0,
            unique_tagged_wallets=unique_tagged_wallets,
        )
    burns = _burn_counts(conn, campaign["id"])
    tagged_sponsored_wallets = _tagged_sponsored_wallet_count(conn, campaign["id"])
    last_operator, changed_at = _last_campaign_operator(conn, network, campaign["id"])
    used = sum(counts.values())
    return CampaignStatus(
        campaign_id=campaign["id"],
        network=network,
        state=state,
        started_at=campaign["started_at"],
        enabled_until=campaign["enabled_until"],
        stopped_at=campaign["stopped_at"],
        cap=campaign["cap"],
        reserved=counts["reserved"],
        minting=counts["minting"],
        minted=counts["minted"],
        offered=counts["offered"],
        accepted=counts["accepted"],
        burn_pending=burns["retryable"] + burns["indeterminate"] + burns["terminal"],
        burn_burned=burns["burned"],
        burn_noop=burns["noop"],
        burn_retryable=burns["retryable"],
        burn_indeterminate=burns["indeterminate"],
        burn_terminal=burns["terminal"],
        remaining=max(0, campaign["cap"] - used),
        countdown_seconds=max(0, campaign["enabled_until"] - now)
        if state in ("active", "at_capacity")
        else 0,
        eligibility_available=eligibility_available,
        unique_tagged_wallets=unique_tagged_wallets,
        tagged_sponsored_wallets=tagged_sponsored_wallets,
        last_operator=last_operator,
        changed_at=changed_at,
    )


def audit_archive_reverify(
    db_path: str, *, network: str, actor: str, result: str, now: int | None = None
) -> None:
    """Durable free_mint_audit row for an automated archive re-verification."""
    _require_supported_network(network)
    timestamp = _timestamp(now)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _audit(
            conn,
            network=network,
            actor=actor.strip() or "system",
            action="archive_reverify",
            at=timestamp,
            campaign_id=None,
            result=result,
        )


def start_campaign(
    db_path: str, *, network: str, actor: str, now: int | None = None
) -> CampaignStatus:
    _require_supported_network(network)
    actor = actor.strip()
    if not actor:
        raise ValueError("actor is required")
    timestamp = _timestamp(now)
    if not _topology_is_valid(network):
        raise ValueError(
            "mainnet sponsored mint requires a signing account distinct from the token issuer"
        )
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current, state, _ = _effective_campaign(conn, network, timestamp)
        if current is not None and state in ("active", "at_capacity"):
            _audit(
                conn,
                network=network,
                actor=actor,
                action="campaign_start",
                at=timestamp,
                campaign_id=current["id"],
                result="already_active",
            )
            return _status(conn, network=network, now=timestamp)

        campaign_id = uuid4().hex
        enabled_until = timestamp + _CAMPAIGN_DURATION_SECONDS
        conn.execute(
            """
            INSERT INTO free_mint_campaigns (
                id, network, status, started_at, enabled_until, stopped_at,
                started_by, stopped_by, cap, created_at, updated_at
            ) VALUES (?, ?, 'active', ?, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (
                campaign_id,
                network,
                timestamp,
                enabled_until,
                actor,
                _CAMPAIGN_CAP,
                timestamp,
                timestamp,
            ),
        )
        _audit(
            conn,
            network=network,
            actor=actor,
            action="campaign_start",
            at=timestamp,
            campaign_id=campaign_id,
            result="started",
        )
        return _status(conn, network=network, now=timestamp)


def stop_campaign(
    db_path: str, *, network: str, actor: str, now: int | None = None
) -> CampaignStatus:
    _require_supported_network(network)
    actor = actor.strip()
    if not actor:
        raise ValueError("actor is required")
    timestamp = _timestamp(now)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        campaign, state, _ = _effective_campaign(conn, network, timestamp)
        if campaign is not None and state in ("active", "at_capacity"):
            conn.execute(
                """
                UPDATE free_mint_campaigns
                SET status = 'stopped', stopped_at = ?, stopped_by = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (timestamp, actor, timestamp, campaign["id"]),
            )
            result = "stopped"
        else:
            result = "already_inactive"
        _audit(
            conn,
            network=network,
            actor=actor,
            action="campaign_stop",
            at=timestamp,
            campaign_id=campaign["id"] if campaign is not None else None,
            result=result,
        )
        return _status(conn, network=network, now=timestamp)


def baseline_coverage_sources(raw: object) -> list[str] | None:
    """Parse the swept-source attestation out of a coverage document.

    Returns the sorted source names a certification run attested to sweeping,
    or None when the document is missing, unparseable, from an unknown or
    older schema version (which cannot carry the attestation), or carries a
    malformed `sources` field. Callers must treat None as "not attested",
    never as "assume the full set"."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        coverage = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(coverage, dict) or coverage.get("version") != BASELINE_COVERAGE_VERSION:
        return None
    sources = coverage.get("sources")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        return None
    return sorted(sources)


def baseline_coverage_accounts(raw: object) -> dict[str, str] | None:
    """Parse the covered-accounts attestation out of a coverage document.

    Returns the concrete account each named source was swept for, or None when
    the document is missing, unparseable, from a version that cannot carry the
    attestation, or carries a malformed `accounts` field. As with
    `baseline_coverage_sources`, callers must treat None as "not attested"."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        coverage = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(coverage, dict) or coverage.get("version") != BASELINE_COVERAGE_VERSION:
        return None
    accounts = coverage.get("accounts")
    if not isinstance(accounts, dict) or not all(
        isinstance(name, str) and isinstance(account, str) for name, account in accounts.items()
    ):
        return None
    return dict(accounts)


def _baseline_coverage_is_bound(row: Mapping[str, Any]) -> bool:
    raw = row["baseline_coverage"]
    if not isinstance(raw, str) or not raw:
        return False
    try:
        coverage = json.loads(raw)
    except (TypeError, ValueError):
        return False
    # An unknown or older coverage version is rejected outright — version 1
    # documents could not record which sources were swept, so they cannot
    # attest what this gate is trusted to prove (#331).
    if not isinstance(coverage, dict) or coverage.get("version") != BASELINE_COVERAGE_VERSION:
        return False
    sources = baseline_coverage_sources(raw)
    if sources is None or not BASELINE_REQUIRED_SOURCES.issubset(sources):
        return False
    accounts = coverage.get("accounts")
    if not isinstance(accounts, dict):
        return False
    expected_accounts = {
        "signing": config.SIGNING_ACCOUNT,
        "token_issuer": config.TOKEN_ISSUER_ADDRESS,
    }
    return (
        coverage.get("source_tag") == config.SOURCE_TAG
        # Not 1 — ledgers 1-32569 were lost in 2012 and no node serves them, so
        # a genuinely complete sweep starts at the earliest ledger that exists.
        and coverage.get("ledger_min")
        == row["baseline_ledger_min"]
        == history_store.EARLIEST_AVAILABLE_LEDGER
        and coverage.get("ledger_max") == row["baseline_ledger_max"]
        and all(accounts.get(name) == account for name, account in expected_accounts.items())
    )


def _archive_connection_is_usable(
    conn: sqlite3.Connection,
    *,
    schema: str,
    network: str,
    timestamp: int,
) -> bool:
    """Validate provenance in an existing SQLite read snapshot."""

    if schema not in {"main", "eligibility"} or network not in SUPPORTED_NETWORKS:
        return False
    table = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = 'xrpl_txs'"
    ).fetchone()
    state_table = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = 'archive_state'"
    ).fetchone()
    if table is None or state_table is None:
        return False
    columns = {row[1] for row in conn.execute(f"PRAGMA {schema}.table_info(xrpl_txs)")}
    if not {"account", "source_tag"}.issubset(columns):
        return False
    conn.execute(f"SELECT account, source_tag FROM {schema}.xrpl_txs LIMIT 1").fetchall()
    if conn.execute(f"SELECT COUNT(*) FROM {schema}.archive_state").fetchone()[0] != 1:
        return False
    row = conn.execute(
        f"""
        SELECT genesis_hash, source_tag, baseline_complete, baseline_ledger_min,
               baseline_ledger_max, baseline_provenance, baseline_coverage,
               baseline_completed_at, validated_ledger_index, validated_close_time,
               heartbeat_at, continuity_gap_at, continuity_gap_after,
               continuity_gap_before, continuity_gap_reason
        FROM {schema}.archive_state WHERE network = ?
        """,
        (network,),
    ).fetchone()
    if row is None:
        return False
    expected_genesis = config.SPONSORED_MINT_ARCHIVE_GENESIS_HASHES.get(network, "")
    if not row["genesis_hash"] or (expected_genesis and row["genesis_hash"] != expected_genesis):
        return False
    if (
        row["source_tag"] != config.SOURCE_TAG
        or row["continuity_gap_at"] is not None
        or row["continuity_gap_after"] is not None
        or row["continuity_gap_before"] is not None
        or row["continuity_gap_reason"] is not None
        or not _baseline_coverage_is_bound(row)
        or row["baseline_complete"] != 1
        or row["baseline_ledger_min"] is None
        or row["baseline_ledger_max"] is None
        or row["baseline_ledger_max"] < row["baseline_ledger_min"]
        or not row["baseline_provenance"]
        or row["baseline_completed_at"] is None
        or row["validated_ledger_index"] is None
        or row["validated_ledger_index"] < row["baseline_ledger_max"]
        or row["validated_close_time"] is None
        or row["heartbeat_at"] is None
    ):
        return False
    max_lag = config.SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS
    heartbeat_age = timestamp - row["heartbeat_at"]
    close_age = timestamp - row["validated_close_time"]
    return bool(-60 <= heartbeat_age <= max_lag and -60 <= close_age <= max_lag)


def _archive_eligibility_snapshot(
    conn: sqlite3.Connection,
    *,
    network: str,
    wallet: str,
    timestamp: int,
) -> tuple[bool, bool]:
    """Return (usable, tagged) from the attached, transaction-locked archive."""

    if not _archive_connection_is_usable(
        conn, schema="eligibility", network=network, timestamp=timestamp
    ):
        return False, False
    tagged = (
        conn.execute(
            """
            SELECT 1 FROM eligibility.xrpl_txs
            WHERE account = ? AND source_tag = ? LIMIT 1
            """,
            (wallet.strip(), config.SOURCE_TAG),
        ).fetchone()
        is not None
    )
    # Keep a second explicit health check adjacent to the app-DB mutation.
    usable = _archive_connection_is_usable(
        conn, schema="eligibility", network=network, timestamp=timestamp
    )
    return usable, tagged


def archive_is_usable(
    history_path: str,
    *,
    network: str | None = None,
    now: int | None = None,
) -> bool:
    """Return whether eligibility has complete, matching, fresh provenance."""

    selected_network = config.XRPL_NETWORK if network is None else network
    if selected_network not in SUPPORTED_NETWORKS:
        return False
    if not os.path.isfile(history_path) or not os.access(history_path, os.R_OK):
        return False
    uri = f"file:{quote(os.path.abspath(history_path))}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            return _archive_connection_is_usable(
                conn,
                schema="main",
                network=selected_network,
                timestamp=_timestamp(now),
            )
    except (OSError, sqlite3.Error):
        return False


def excluded_wallets() -> frozenset[str]:
    values = (
        *config.SPONSORED_MINT_EXCLUDED_WALLETS,
        config.SIGNING_ACCOUNT,
        config.TOKEN_ISSUER_ADDRESS,
    )
    return frozenset(value.strip() for value in values if value and value.strip())


def observe_sponsored_acceptance(tx: dict[str, Any], meta: dict[str, Any], *, network: str) -> bool:
    """Record one validated tagged sponsored-offer acceptance.

    The archive callers invoke this only after a new raw transaction was
    inserted. The defensive validation here keeps replay and live paths
    identical and makes direct callers harmless for unrelated firehose rows.
    """
    if network not in SUPPORTED_NETWORKS:
        return False
    if tx.get("validated") is not True:
        return False
    if tx.get("TransactionType") != "NFTokenAcceptOffer":
        return False
    if tx.get("SourceTag") != config.SOURCE_TAG:
        return False
    if meta.get("TransactionResult") != "tesSUCCESS":
        return False
    wallet = tx.get("Account")
    tx_hash = tx.get("hash")
    offer_id = tx.get("NFTokenSellOffer")
    if (
        not isinstance(wallet, str)
        or not isinstance(tx_hash, str)
        or not isinstance(offer_id, str)
        or not offer_id.strip()
    ):
        return False
    wallet = wallet.strip()
    tx_hash = tx_hash.strip()
    if not wallet or not tx_hash or wallet in excluded_wallets():
        return False
    timestamp = history_events.tx_unix_time({**tx, "meta": meta})
    try:
        claim = record_acceptance(
            db_path.app_db_path(network),
            network,
            wallet,
            tx_hash,
            timestamp,
            offer_id=offer_id,
        )
    except ValueError:
        # A different already-recorded acceptance must never be overwritten.
        return False
    return claim is not None


def _unique_tagged_wallets(
    history_path: str, *, network: str | None = None, now: int | None = None
) -> int | None:
    if not archive_is_usable(history_path, network=network, now=now):
        return None
    uri = f"file:{quote(os.path.abspath(history_path))}?mode=ro"
    excluded = tuple(sorted(excluded_wallets()))
    exclusions = ",".join("?" for _ in excluded)
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            row = conn.execute(
                f"""
                SELECT count(DISTINCT account) AS count
                FROM xrpl_txs
                WHERE source_tag = ? AND account IS NOT NULL
                  AND trim(account) != ''
                  AND trim(account) NOT IN ({exclusions})
                """,
                (config.SOURCE_TAG, *excluded),
            ).fetchone()
            return int(row[0] or 0)
    except (OSError, sqlite3.Error):
        return None


def campaign_status(
    db_path: str,
    history_path: str,
    *,
    network: str,
    now: int | None = None,
) -> CampaignStatus:
    _require_supported_network(network)
    timestamp = _timestamp(now)
    ensure_schema(db_path)
    usable = _topology_is_valid(network) and archive_is_usable(
        history_path, network=network, now=timestamp
    )
    unique = (
        _unique_tagged_wallets(history_path, network=network, now=timestamp) if usable else None
    )
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _status(
            conn,
            network=network,
            now=timestamp,
            eligibility_available=usable,
            unique_tagged_wallets=unique,
        )


def reserve_if_eligible(
    db_path: str,
    history_path: str,
    *,
    network: str,
    wallet: str,
    session_id: str,
    now: int | None = None,
) -> ReservationResult:
    wallet = wallet.strip()
    session_id = session_id.strip()
    if not wallet or not session_id:
        return ReservationResult(False, "invalid_request", None)
    if network not in SUPPORTED_NETWORKS:
        return ReservationResult(False, "wrong_network", None)
    if not _topology_is_valid(network):
        return ReservationResult(False, "invalid_topology", None)

    ensure_schema(db_path)
    timestamp = _timestamp(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _claim_for_session(conn, network, wallet, session_id)
        if existing is not None and existing.status in _ACTIVE_CLAIM_STATES:
            return ReservationResult(True, "reserved", existing)

        # Advisory fast path. The campaign/cap is checked again in the final
        # write transaction because the archive lives in a separate database.
        campaign, state, _ = _effective_campaign(conn, network, timestamp)
        if campaign is None or state in ("off", "stopped"):
            return ReservationResult(False, "campaign_off", None)
        if state == "expired":
            return ReservationResult(False, "campaign_expired", None)
        if state in ("full", "at_capacity"):
            return ReservationResult(False, "at_capacity", None)

    if wallet in excluded_wallets():
        return ReservationResult(False, "ineligible", None)
    if not os.path.isfile(history_path) or not os.access(history_path, os.R_OK | os.W_OK):
        return ReservationResult(False, "eligibility_unavailable", None)

    final_conn: sqlite3.Connection | None = None
    try:
        final_conn = _connect(db_path)
        final_conn.execute(
            "ATTACH DATABASE ? AS eligibility",
            (os.path.abspath(history_path),),
        )
        # IMMEDIATE reserves both the app DB and attached history DB. The
        # listener therefore cannot invalidate continuity between this one
        # archive snapshot and the claim commit below.
        final_conn.execute("BEGIN IMMEDIATE")
    except (OSError, sqlite3.Error):
        if final_conn is not None:
            final_conn.close()
        return ReservationResult(False, "eligibility_unavailable", None)
    with final_conn as conn:
        usable, ineligible = _archive_eligibility_snapshot(
            conn,
            network=network,
            wallet=wallet,
            timestamp=timestamp,
        )
        if not usable:
            return ReservationResult(False, "eligibility_unavailable", None)
        if ineligible:
            return ReservationResult(False, "ineligible", None)
        campaign, state, counts = _effective_campaign(conn, network, timestamp)
        if campaign is None or state in ("off", "stopped"):
            return ReservationResult(False, "campaign_off", None)
        if state == "expired":
            return ReservationResult(False, "campaign_expired", None)
        if state == "full":
            return ReservationResult(False, "at_capacity", None)

        existing_row = conn.execute(
            """
            SELECT id, network, wallet, campaign_id, session_id, status,
                   reserved_at, reservation_expires_at, released_at,
                   mint_tx_hash, nft_id, offer_id, accept_tx_hash, tagged_at,
                   last_error, created_at, updated_at
            FROM free_mint_claims
            WHERE network = ? AND wallet = ?
            """,
            (network, wallet),
        ).fetchone()
        existing = _claim(existing_row)
        if existing is not None:
            if existing.nft_id is not None or existing.status in _CONSUMED_CLAIM_STATES:
                return ReservationResult(False, "already_consumed", None)
            if existing.status in ("reserved", "minting"):
                if existing.session_id == session_id:
                    return ReservationResult(True, "reserved", existing)
                return ReservationResult(False, "already_reserved", None)

        if state == "at_capacity" or sum(counts.values()) >= campaign["cap"]:
            return ReservationResult(False, "at_capacity", None)

        if existing is not None and existing.status == "released":
            previous: dict[str, object] = {
                "campaign_id": existing.campaign_id,
                "session_id": existing.session_id,
                "wallet": existing.wallet,
                "released_at": existing.released_at,
                "reason": existing.last_error,
            }
            conn.execute(
                """
                UPDATE free_mint_claims
                SET campaign_id = ?, session_id = ?, status = 'reserved',
                    reserved_at = ?, reservation_expires_at = ?, released_at = NULL,
                    mint_tx_hash = NULL, mint_signed_tx_hash = NULL,
                    mint_signed_tx_blob = NULL, mint_signed_ledger_floor = NULL,
                    mint_forwarded_at = NULL, mint_nft_number = NULL,
                    mint_metadata_url = NULL, mint_metadata_json = NULL,
                    mint_body_type = NULL, mint_still_token = NULL,
                    nft_id = NULL, offer_id = NULL,
                    accept_tx_hash = NULL, tagged_at = NULL, last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'released'
                """,
                (
                    campaign["id"],
                    session_id,
                    timestamp,
                    campaign["enabled_until"],
                    timestamp,
                    existing.id,
                ),
            )
            _audit(
                conn,
                network=network,
                actor=session_id,
                action="claim_reacquired",
                at=timestamp,
                campaign_id=existing.campaign_id,
                result="reserved",
                details=previous,
            )
            claim_id = existing.id
        else:
            claim_id = uuid4().hex
            conn.execute(
                """
                INSERT INTO free_mint_claims (
                    id, network, wallet, campaign_id, session_id, status,
                    reserved_at, reservation_expires_at, released_at,
                    mint_tx_hash, nft_id, offer_id, accept_tx_hash, tagged_at,
                    last_error, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, 'reserved', ?, ?, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
                )
                """,
                (
                    claim_id,
                    network,
                    wallet,
                    campaign["id"],
                    session_id,
                    timestamp,
                    campaign["enabled_until"],
                    timestamp,
                    timestamp,
                ),
            )
            _audit(
                conn,
                network=network,
                actor=session_id,
                action="claim_reserved",
                at=timestamp,
                campaign_id=campaign["id"],
                result="reserved",
                details={"wallet": wallet},
            )
        row = conn.execute(
            """
            SELECT id, network, wallet, campaign_id, session_id, status,
                   reserved_at, reservation_expires_at, released_at,
                   mint_tx_hash, nft_id, offer_id, accept_tx_hash, tagged_at,
                   last_error, created_at, updated_at
            FROM free_mint_claims WHERE id = ?
            """,
            (claim_id,),
        ).fetchone()
        return ReservationResult(True, "reserved", _claim(row))


def release_reservation(
    db_path: str,
    *,
    network: str,
    wallet: str,
    session_id: str,
    reason: str,
    now: int | None = None,
) -> bool:
    timestamp = _timestamp(now)
    wallet = wallet.strip()
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, campaign_id, status
            FROM free_mint_claims
            WHERE network = ? AND wallet = ? AND session_id = ?
            """,
            (network, wallet, session_id),
        ).fetchone()
        if row is None:
            return False
        if row["status"] == "released":
            return True
        if row["status"] != "reserved":
            return False
        conn.execute(
            """
            UPDATE free_mint_claims
            SET status = 'released', released_at = ?, last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'reserved'
            """,
            (timestamp, reason, timestamp, row["id"]),
        )
        _audit(
            conn,
            network=network,
            actor=session_id,
            action="claim_released",
            at=timestamp,
            campaign_id=row["campaign_id"],
            result="released",
            details={"reason": reason, "wallet": wallet},
        )
        return True


def rebind_reservation(
    db_path: str,
    *,
    network: str,
    wallet: str,
    expected_session_id: str,
    new_session_id: str,
    now: int | None = None,
) -> Claim | None:
    """Atomically attach an orphaned reversible promise to a new session.

    Campaign stop/expiry does not revoke an already promised mint. Only the
    exact old session can be replaced, and only while the claim is reversible.
    """

    timestamp = _timestamp(now)
    wallet = wallet.strip()
    expected_session_id = expected_session_id.strip()
    new_session_id = new_session_id.strip()
    if network not in SUPPORTED_NETWORKS or not all((wallet, expected_session_id, new_session_id)):
        return None
    ensure_schema(db_path)
    if not _topology_is_valid(network):
        return None
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, campaign_id FROM free_mint_claims
            WHERE network = ? AND wallet = ? AND session_id = ? AND status = 'reserved'
            """,
            (network, wallet, expected_session_id),
        ).fetchone()
        if row is None:
            return None
        cursor = conn.execute(
            """
            UPDATE free_mint_claims SET session_id = ?, updated_at = ?
            WHERE id = ? AND session_id = ? AND status = 'reserved'
            """,
            (new_session_id, timestamp, row["id"], expected_session_id),
        )
        if cursor.rowcount != 1:
            return None
        _audit(
            conn,
            network=network,
            actor=new_session_id,
            action="claim_rebound",
            at=timestamp,
            campaign_id=row["campaign_id"],
            result="reserved",
            details={"previous_session_id": expected_session_id, "wallet": wallet},
        )
        return _claim_for_session(conn, network, wallet, new_session_id)


def _claim_for_session(
    conn: sqlite3.Connection, network: str, wallet: str, session_id: str
) -> Claim | None:
    row = conn.execute(
        """
        SELECT * FROM free_mint_claims
        WHERE network = ? AND wallet = ? AND session_id = ?
        """,
        (network, wallet.strip(), session_id),
    ).fetchone()
    return _claim(row)


def claim_for_session(db_path: str, *, network: str, wallet: str, session_id: str) -> Claim | None:
    """Return the durable claim bound to one mint session."""

    ensure_schema(db_path)
    with _connect(db_path) as conn:
        return _claim_for_session(conn, network, wallet, session_id)


def record_mint_prepared(
    db_path: str,
    *,
    network: str,
    wallet: str,
    session_id: str,
    tx_hash: str,
    tx_blob: str,
    signed_ledger_floor: int,
    nft_number: int,
    metadata_url: str,
    metadata_json: str,
    body_type: str,
    still_token: str | None = None,
    now: int | None = None,
) -> Claim | None:
    """Persist the exact signed mint and its claim correlation before forwarding.

    `still_token` is the image-archive staging token of the session that
    composed the still (#330) — the only key that can promote or discard
    `pending/<edition>.<token>.png` after the claim is rebound to a fresh
    session id on resume. Optional so legacy callers stay valid; a NULL
    token simply skips archive promotion on resume."""

    timestamp = _timestamp(now)
    tx_hash = tx_hash.strip()
    tx_blob = tx_blob.strip()
    metadata_url = metadata_url.strip()
    body_type = body_type.strip()
    still_token = still_token.strip() if still_token else None
    if still_token == "":
        still_token = None
    if (
        network not in SUPPORTED_NETWORKS
        or not wallet.strip()
        or not session_id.strip()
        or not tx_hash
        or not tx_blob
        or signed_ledger_floor <= 0
        or nft_number <= 0
        or not metadata_url
        or not metadata_json.strip()
        or not body_type
    ):
        raise ValueError("complete prepared mint identity and correlation are required")
    try:
        json.loads(metadata_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("prepared mint metadata_json must be valid JSON") from exc

    expected = (
        tx_hash,
        tx_blob,
        signed_ledger_floor,
        nft_number,
        metadata_url,
        metadata_json,
        body_type,
    )
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = _claim_for_session(conn, network, wallet, session_id)
        if claim is None or claim.status != "reserved":
            return None
        persisted = (
            claim.mint_signed_tx_hash,
            claim.mint_signed_tx_blob,
            claim.mint_signed_ledger_floor,
            claim.mint_nft_number,
            claim.mint_metadata_url,
            claim.mint_metadata_json,
            claim.mint_body_type,
        )
        if any(value is not None for value in persisted):
            # `mint_still_token` is deliberately outside this equality check:
            # a pre-#330 journal retried by post-#330 code must stay idempotent
            # even though its persisted token is NULL.
            if persisted != expected:
                raise ValueError("prepared mint conflicts with the persisted claim")
            return claim
        cursor = conn.execute(
            """
            UPDATE free_mint_claims
            SET mint_signed_tx_hash = ?, mint_signed_tx_blob = ?,
                mint_signed_ledger_floor = ?, mint_nft_number = ?,
                mint_metadata_url = ?, mint_metadata_json = ?, mint_body_type = ?,
                mint_still_token = ?, last_error = NULL, updated_at = ?
            WHERE id = ? AND status = 'reserved'
              AND mint_signed_tx_hash IS NULL AND mint_signed_tx_blob IS NULL
              AND mint_signed_ledger_floor IS NULL
            """,
            (*expected, still_token, timestamp, claim.id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("prepared mint journal compare-and-set failed")
        _audit(
            conn,
            network=network,
            actor=session_id,
            action="claim_mint_prepared",
            at=timestamp,
            campaign_id=claim.campaign_id,
            result="prepared",
            details={
                "tx_hash": tx_hash,
                "signed_ledger_floor": signed_ledger_floor,
                "nft_number": nft_number,
            },
        )
        return _claim_for_session(conn, network, wallet, session_id)


def mark_mint_forwarded(
    db_path: str,
    *,
    network: str,
    wallet: str,
    session_id: str,
    tx_hash: str,
    now: int | None = None,
) -> MintingResult | None:
    """Cross the irreversible boundary for one already-journaled identity."""

    timestamp = _timestamp(now)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = _claim_for_session(conn, network, wallet, session_id)
        if claim is None:
            return None
        complete_identity = (
            claim.mint_signed_tx_hash
            and claim.mint_signed_tx_blob
            and claim.mint_signed_ledger_floor
            and claim.mint_nft_number
            and claim.mint_metadata_url
            and claim.mint_metadata_json
            and claim.mint_body_type
        )
        if not complete_identity:
            return None
        if claim.mint_signed_tx_hash != tx_hash:
            raise ValueError("forwarded mint hash conflicts with the prepared claim")
        if claim.status != "reserved":
            if claim.status in ("minting", "minted", "offered", "accepted", "failed_terminal"):
                return MintingResult(claim=claim, started=False)
            return None
        cursor = conn.execute(
            """
            UPDATE free_mint_claims
            SET status = 'minting', mint_forwarded_at = ?, updated_at = ?
            WHERE id = ? AND status = 'reserved' AND mint_signed_tx_hash = ?
            """,
            (timestamp, timestamp, claim.id, tx_hash),
        )
        if cursor.rowcount != 1:
            return None
        _audit(
            conn,
            network=network,
            actor=session_id,
            action="claim_mint_forwarded",
            at=timestamp,
            campaign_id=claim.campaign_id,
            result="minting",
            details={"tx_hash": tx_hash},
        )
        updated = _claim_for_session(conn, network, wallet, session_id)
        assert updated is not None
        return MintingResult(claim=updated, started=True)


def mint_recovery_claims(db_path: str, *, network: str) -> tuple[Claim, ...]:
    """Return every irreversible mint with its exact prepared identity."""

    ensure_schema(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM free_mint_claims
            WHERE network = ? AND status = 'minting'
            ORDER BY created_at, id
            """,
            (network,),
        ).fetchall()
    return tuple(claim for row in rows if (claim := _claim(row)) is not None)


def reversible_reservation_for_wallet(db_path: str, *, network: str, wallet: str) -> Claim | None:
    """Find a promised but not-yet-forwarded claim, even after campaign close."""

    ensure_schema(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM free_mint_claims
            WHERE network = ? AND wallet = ? AND status = 'reserved'
            """,
            (network, wallet.strip()),
        ).fetchone()
    return _claim(row)


def reset_validated_mint_failure(
    db_path: str, *, network: str, wallet: str, session_id: str, error: str
) -> Claim | None:
    """Retire a definitively failed forwarded identity back to its promise."""

    timestamp = _timestamp(None)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = _claim_for_session(conn, network, wallet, session_id)
        if claim is None or claim.status != "minting" or not claim.mint_signed_tx_hash:
            return None
        conn.execute(
            """
            UPDATE free_mint_claims SET status = 'reserved',
                mint_signed_tx_hash = NULL, mint_signed_tx_blob = NULL,
                mint_signed_ledger_floor = NULL, mint_forwarded_at = NULL,
                mint_nft_number = NULL, mint_metadata_url = NULL,
                mint_metadata_json = NULL, mint_body_type = NULL,
                mint_still_token = NULL, last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'minting'
            """,
            (error, timestamp, claim.id),
        )
        return _claim_for_session(conn, network, wallet, session_id)


def headroom_snapshots(db_path: str, *, network: str) -> list[tuple[str, int, list[str]]]:
    """Reconstruct cap accounting for irreversible sponsored claims.

    A confirmed NFT is reasserted in the pending set until the index observes
    it. A minting/otherwise-consumed claim without an NFT ID is uncertain and
    conservatively retains one reservation. Reversible `reserved` claims are
    intentionally omitted: they remain durable promises and can be rebound to
    a replacement session without consuming headroom.

    Unlike `headroom.rebuild`, errors intentionally propagate. Startup then
    skips the entire rebuild, preserving the pre-crash overlay and failing
    closed instead of deleting possibly-live reservations.
    """
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT session_id, status, nft_id
            FROM free_mint_claims
            WHERE network = ?
              AND status IN ('minting', 'minted', 'offered', 'accepted', 'failed_terminal')
            """,
            (network,),
        ).fetchall()
    snapshots = []
    for row in rows:
        nft_id = row["nft_id"]
        snapshots.append(
            (
                f"mint:{row['session_id']}",
                0 if nft_id else 1,
                [nft_id] if nft_id else [],
            )
        )
    return snapshots


def _enqueue_missing_burn(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    timestamp: int,
) -> None:
    """Restore absent debt without modifying any existing obligation."""

    conn.execute(
        """
        INSERT INTO free_mint_burns (
            id, claim_id, status, memo_id, amount, source_account,
            network, issuer, currency, source_tag, tx_hash, attempt_count,
            last_attempt_at, next_attempt_at, last_error,
            burned_at, created_at, updated_at
        ) VALUES (
            ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, NULL, 0,
            NULL, ?, NULL, NULL, ?, ?
        )
        ON CONFLICT(claim_id) DO NOTHING
        """,
        (
            uuid4().hex,
            claim_id,
            sponsored_burn_memo_id(claim_id),
            config.MINT_PRICE_LFGO,
            config.SIGNING_ACCOUNT,
            conn.execute(
                "SELECT network FROM free_mint_claims WHERE id = ?", (claim_id,)
            ).fetchone()[0],
            config.TOKEN_ISSUER_ADDRESS,
            config.TOKEN_CURRENCY_HEX,
            config.SOURCE_TAG,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def recover_incomplete_claims(
    db_path: str,
    history_path: str,
    *,
    network: str,
) -> RecoveryReport:
    """Classify persisted sponsored work before the service accepts requests.

    Only an expired `reserved` row with no irreversible identifiers is released.
    `minting` is uncertain and always remains consumed/held here; promotion to
    `minted` happens only in `_recover_sponsored_mint_submissions`
    (lfg_service/app.py), which reconciles each forwarded transaction against
    the ledger and runs immediately before this classification at startup.
    Existing burn rows are never updated or deleted; missing debt is only added.
    Expired submitting leases are reported for the existing burn worker to reclaim.
    """

    timestamp = _timestamp(None)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _, campaign_state, _ = _effective_campaign(conn, network, timestamp)

        # Reversible reservations are durable promises. Expiry closes new
        # admissions; it does not revoke a claim whose in-memory session died.
        # A later request rebinds it transactionally, or an explicit cancel
        # releases it. This tuple is therefore ALWAYS empty by design — it
        # stays in the report so callers keep a stable shape and so the
        # deliberate no-release policy is visible at the call site rather than
        # looking like an unimplemented branch. `reservation_expires_at` is the
        # campaign's end, not a reservation TTL; nothing reads it back.
        released: tuple[str, ...] = ()

        # `minting` claims are never auto-promoted here. The only writer that
        # sets `mint_tx_hash`/`nft_id` (`record_minted_and_enqueue_burn`) flips
        # the status to 'minted' in the same UPDATE, so a 'minting' row can
        # never carry corroborable mint evidence; ledger reconciliation of
        # forwarded transactions is `_recover_sponsored_mint_submissions`'s
        # job. Like `released`, this tuple is ALWAYS empty by design and stays
        # in the report so callers keep a stable shape.
        recovered: tuple[str, ...] = ()

        consumed_rows = conn.execute(
            """
            SELECT id
            FROM free_mint_claims
            WHERE network = ?
              AND status IN ('minted', 'offered', 'accepted', 'failed_terminal')
              AND nft_id IS NOT NULL
            """,
            (network,),
        ).fetchall()
        for row in consumed_rows:
            _enqueue_missing_burn(conn, claim_id=row["id"], timestamp=timestamp)

        held = tuple(
            row["id"]
            for row in conn.execute(
                """
                SELECT id
                FROM free_mint_claims
                WHERE network = ? AND status = 'minting'
                ORDER BY created_at, id
                """,
                (network,),
            )
        )
        missing_offers = tuple(
            row["id"]
            for row in conn.execute(
                """
                SELECT id
                FROM free_mint_claims
                WHERE network = ? AND status = 'minted'
                  AND nft_id IS NOT NULL AND offer_id IS NULL
                ORDER BY created_at, id
                """,
                (network,),
            )
        )
        reclaimable = tuple(
            row["id"]
            for row in conn.execute(
                """
                SELECT b.id
                FROM free_mint_burns AS b
                JOIN free_mint_claims AS c ON c.id = b.claim_id
                WHERE c.network = ?
                  AND b.status = 'submitting'
                  AND b.lease_until IS NOT NULL AND b.lease_until <= ?
                ORDER BY b.created_at, b.id
                """,
                (network, timestamp),
            )
        )
        debt_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM free_mint_burns AS b
                JOIN free_mint_claims AS c ON c.id = b.claim_id
                WHERE c.network = ?
                  AND b.status IN ('pending', 'submitting', 'indeterminate', 'failed_terminal')
                """,
                (network,),
            ).fetchone()[0]
        )

    return RecoveryReport(
        network=network,
        campaign_state=campaign_state,
        released_reservations=released,
        held_minting=held,
        recovered_mints=recovered,
        missing_offers=missing_offers,
        reclaimable_burns=reclaimable,
        debt_count=debt_count,
    )


def nft_projection_recovery_claims(db_path: str, *, network: str) -> tuple[Claim, ...]:
    """Return consumed claims whose canonical NFT projection must exist."""

    ensure_schema(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM free_mint_claims
            WHERE network = ?
              AND status IN ('minted', 'offered', 'accepted')
              AND nft_id IS NOT NULL
            ORDER BY created_at, id
            """,
            (network,),
        ).fetchall()
    return tuple(claim for row in rows if (claim := _claim(row)) is not None)


def offer_recovery_claims(db_path: str, *, network: str) -> tuple[Claim, ...]:
    """Return claims needing offer creation or archived-acceptance replay."""

    ensure_schema(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, network, wallet, campaign_id, session_id, status,
                   reserved_at, reservation_expires_at, released_at,
                   mint_tx_hash, nft_id, offer_id, accept_tx_hash, tagged_at,
                   last_error, created_at, updated_at
            FROM free_mint_claims
            WHERE network = ? AND nft_id IS NOT NULL
              AND (
                    (status = 'minted' AND offer_id IS NULL)
                 OR (status = 'offered' AND offer_id IS NOT NULL)
              )
            ORDER BY created_at, id
            """,
            (network,),
        ).fetchall()
    return tuple(claim for row in rows if (claim := _claim(row)) is not None)


def record_minted_and_enqueue_burn(
    db_path: str,
    *,
    network: str,
    wallet: str,
    session_id: str,
    mint_tx_hash: str,
    nft_id: str,
    now: int | None = None,
) -> Claim | None:
    timestamp = _timestamp(now)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = _claim_for_session(conn, network, wallet, session_id)
        if claim is None:
            return None
        if claim.status in ("minted", "offered", "accepted"):
            if claim.mint_tx_hash != mint_tx_hash or claim.nft_id != nft_id:
                raise ValueError("confirmed mint conflicts with the persisted claim")
            return claim
        if claim.status != "minting":
            return None
        if claim.mint_signed_tx_hash != mint_tx_hash:
            raise ValueError("confirmed mint hash conflicts with the prepared transaction")
        conn.execute(
            """
            UPDATE free_mint_claims
            SET status = 'minted', mint_tx_hash = ?, nft_id = ?,
                last_error = NULL, updated_at = ?
            WHERE id = ? AND status = 'minting'
            """,
            (mint_tx_hash, nft_id, timestamp, claim.id),
        )
        _enqueue_missing_burn(conn, claim_id=claim.id, timestamp=timestamp)
        _audit(
            conn,
            network=network,
            actor=session_id,
            action="claim_minted",
            at=timestamp,
            campaign_id=claim.campaign_id,
            result="minted",
            details={"mint_tx_hash": mint_tx_hash, "nft_id": nft_id},
        )
        _effective_campaign(conn, network, timestamp)
        return _claim_for_session(conn, network, wallet, session_id)


def record_offer(
    db_path: str,
    *,
    network: str,
    wallet: str,
    session_id: str,
    offer_id: str | None,
    error: str | None = None,
    now: int | None = None,
    history_path: str | None = None,
) -> Claim | None:
    timestamp = _timestamp(now)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = _claim_for_session(conn, network, wallet, session_id)
        if claim is None or claim.status not in ("minted", "offered", "accepted"):
            return None
        if offer_id is None:
            if error is not None and claim.last_error != error:
                conn.execute(
                    """
                    UPDATE free_mint_claims
                    SET last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error, timestamp, claim.id),
                )
            return _claim_for_session(conn, network, wallet, session_id)
        if claim.offer_id is not None:
            if claim.offer_id != offer_id:
                raise ValueError("offer conflicts with the persisted claim")
            if history_path is not None:
                conn.commit()
                replayed = replay_archived_acceptance(
                    db_path,
                    history_path,
                    network=network,
                    wallet=wallet,
                    offer_id=offer_id,
                    nft_id=claim.nft_id,
                    now=timestamp,
                )
                return replayed or claim
            return claim
        conn.execute(
            """
            UPDATE free_mint_claims
            SET status = CASE WHEN status = 'accepted' THEN status ELSE 'offered' END,
                offer_id = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (offer_id, timestamp, claim.id),
        )
        _audit(
            conn,
            network=network,
            actor=session_id,
            action="claim_offered",
            at=timestamp,
            campaign_id=claim.campaign_id,
            result="offered",
            details={"offer_id": offer_id},
        )
        updated = _claim_for_session(conn, network, wallet, session_id)
        if history_path is not None:
            conn.commit()
            replayed = replay_archived_acceptance(
                db_path,
                history_path,
                network=network,
                wallet=wallet,
                offer_id=offer_id,
                nft_id=claim.nft_id,
                now=timestamp,
            )
            return replayed or updated
        return updated


def record_acceptance(
    db_path: str,
    network: str,
    wallet: str,
    tx_hash: str,
    now: int | None = None,
    *,
    offer_id: str | None = None,
) -> Claim | None:
    timestamp = _timestamp(now)
    wallet = wallet.strip()
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, network, wallet, campaign_id, session_id, status,
                   reserved_at, reservation_expires_at, released_at,
                   mint_tx_hash, nft_id, offer_id, accept_tx_hash, tagged_at,
                   last_error, created_at, updated_at
            FROM free_mint_claims
            WHERE network = ? AND wallet = ?
            """,
            (network, wallet),
        ).fetchone()
        claim = _claim(row)
        if (
            claim is None
            or claim.status not in ("minted", "offered", "accepted")
            or not claim.offer_id
            or offer_id != claim.offer_id
        ):
            return None
        if claim.accept_tx_hash is not None:
            if claim.accept_tx_hash != tx_hash:
                raise ValueError("acceptance conflicts with the persisted claim")
            return claim
        conn.execute(
            """
            UPDATE free_mint_claims
            SET status = 'accepted', accept_tx_hash = ?, tagged_at = ?,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (tx_hash, timestamp, timestamp, claim.id),
        )
        _audit(
            conn,
            network=network,
            actor=wallet,
            action="claim_accepted",
            at=timestamp,
            campaign_id=claim.campaign_id,
            result="accepted",
            details={"tx_hash": tx_hash},
        )
        updated = conn.execute(
            """
            SELECT id, network, wallet, campaign_id, session_id, status,
                   reserved_at, reservation_expires_at, released_at,
                   mint_tx_hash, nft_id, offer_id, accept_tx_hash, tagged_at,
                   last_error, created_at, updated_at
            FROM free_mint_claims WHERE id = ?
            """,
            (claim.id,),
        ).fetchone()
        return _claim(updated)


def _acceptance_offer_matches_claim(
    meta: dict[str, Any],
    *,
    offer_id: str,
    nft_id: str,
    wallet: str,
) -> bool:
    nodes = meta.get("AffectedNodes")
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if not isinstance(node, dict):
            continue
        deleted = node.get("DeletedNode")
        if (
            not isinstance(deleted, dict)
            or deleted.get("LedgerEntryType") != "NFTokenOffer"
            or deleted.get("LedgerIndex") != offer_id
        ):
            continue
        fields = deleted.get("FinalFields")
        if not isinstance(fields, dict):
            continue
        flags = fields.get("Flags")
        return (
            isinstance(flags, int)
            and not isinstance(flags, bool)
            and bool(flags & market_ops.LSF_SELL_NFTOKEN)
            and fields.get("NFTokenID") == nft_id
            and fields.get("Owner") == config.SIGNING_ACCOUNT
            and fields.get("Destination") == wallet
            and fields.get("Amount") == "0"
        )
    return False


def replay_archived_acceptance(
    db_path: str,
    history_path: str,
    *,
    network: str,
    wallet: str,
    offer_id: str | None,
    nft_id: str | None = None,
    session_id: str | None = None,
    now: int | None = None,
) -> Claim | None:
    """Project an exact archived acceptance, including a pre-offer-record crash."""

    uri = f"file:{quote(os.path.abspath(history_path))}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            if not _archive_connection_is_usable(
                conn,
                schema="main",
                network=network,
                timestamp=_timestamp(now),
            ):
                return None
            rows = conn.execute(
                """
                SELECT tx_hash, close_time, raw_json
                FROM xrpl_txs
                WHERE account = ? AND source_tag = ?
                  AND tx_type = 'NFTokenAcceptOffer'
                ORDER BY ledger_index, tx_hash
                """,
                (wallet.strip(), config.SOURCE_TAG),
            ).fetchall()
            match: tuple[str, str, int] | None = None
            for row in rows:
                try:
                    raw = json.loads(row["raw_json"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(raw, dict):
                    continue
                tx = raw.get("tx", raw)
                if not isinstance(tx, dict):
                    continue
                meta = tx.get("meta")
                if not isinstance(meta, dict):
                    meta = raw.get("meta")
                tx_offer_id = tx.get("NFTokenSellOffer")
                if (
                    tx.get("validated") is not True
                    or tx.get("TransactionType") != "NFTokenAcceptOffer"
                    or tx.get("Account") != wallet.strip()
                    or tx.get("SourceTag") != config.SOURCE_TAG
                    or not isinstance(tx_offer_id, str)
                    or not tx_offer_id
                    or (offer_id is not None and tx_offer_id != offer_id)
                    or not isinstance(meta, dict)
                    or meta.get("TransactionResult") != "tesSUCCESS"
                    or (
                        offer_id is None
                        and (
                            nft_id is None
                            or not _acceptance_offer_matches_claim(
                                meta,
                                offer_id=tx_offer_id,
                                nft_id=nft_id,
                                wallet=wallet.strip(),
                            )
                        )
                    )
                ):
                    continue
                tx_hash = tx.get("hash") or row["tx_hash"]
                accepted_at = row["close_time"]
                if accepted_at is None:
                    accepted_at = history_events.tx_unix_time({**tx, "meta": meta})
                if isinstance(tx_hash, str) and tx_hash and isinstance(accepted_at, int):
                    match = (tx_offer_id, tx_hash, accepted_at)
                    break
    except (OSError, sqlite3.Error):
        return None
    if match is None:
        return None
    matched_offer, tx_hash, accepted_at = match
    if offer_id is None:
        if not session_id:
            return None
        offered = record_offer(
            db_path,
            network=network,
            wallet=wallet,
            session_id=session_id,
            offer_id=matched_offer,
            now=accepted_at,
        )
        if offered is None or offered.offer_id != matched_offer:
            return None
    try:
        return record_acceptance(
            db_path,
            network,
            wallet,
            tx_hash,
            accepted_at,
            offer_id=matched_offer,
        )
    except (TypeError, ValueError):
        return None
