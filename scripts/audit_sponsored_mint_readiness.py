#!/usr/bin/env python3
"""Read-only preflight for the sponsored free-mint campaign."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from xrpl.core.addresscodec import is_valid_classic_address

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, db_path, history_store, sponsored_mint, xrpl_ops  # noqa: E402

_APP_TABLE_COLUMNS = {
    "free_mint_campaigns": {
        "id",
        "network",
        "status",
        "enabled_until",
        "cap",
    },
    "free_mint_claims": {
        "id",
        "network",
        "status",
        "reservation_expires_at",
        "mint_tx_hash",
        "nft_id",
        "offer_id",
    },
    "free_mint_burns": {
        "claim_id",
        "status",
        "amount",
        "source_account",
        "network",
        "issuer",
        "currency",
        "source_tag",
        "lease_until",
        "signed_tx_hash",
    },
    "free_mint_audit": {"network", "action", "at", "result"},
}
_HISTORY_COLUMNS = {
    "tx_hash",
    "ledger_index",
    "close_time",
    "account",
    "source_tag",
    "raw_json",
}

_ARCHIVE_STATE_COLUMNS = {
    "network",
    "genesis_hash",
    "baseline_complete",
    "source_tag",
    "baseline_coverage",
    "baseline_ledger_min",
    "baseline_ledger_max",
    "baseline_provenance",
    "baseline_completed_at",
    "validated_ledger_index",
    "validated_close_time",
    "heartbeat_at",
    "continuity_gap_at",
    "continuity_gap_after",
    "continuity_gap_before",
    "continuity_gap_reason",
}
BalanceFetch = Callable[[], Awaitable[Decimal | None] | Decimal | None]


def _readonly(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path) or not os.access(path, os.R_OK):
        raise FileNotFoundError(path)
    uri = f"file:{quote(os.path.abspath(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _schema_ok(conn: sqlite3.Connection, expected: dict[str, set[str]]) -> tuple[bool, str]:
    missing: list[str] = []
    for table, columns in expected.items():
        found = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
        }
        if not found:
            missing.append(f"table:{table}")
            continue
        missing.extend(f"{table}.{column}" for column in sorted(columns - found))
    return not missing, "ok" if not missing else "missing " + ", ".join(missing)


def _campaign_check(
    conn: sqlite3.Connection, *, network: str, now: int
) -> tuple[dict[str, Any], int]:
    row = conn.execute(
        """
        SELECT id, status, enabled_until, cap
        FROM free_mint_campaigns
        WHERE network = ?
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (network,),
    ).fetchone()
    if row is None:
        return {
            "ok": True,
            "state": "off",
            "campaign_id": None,
            "remaining": 0,
        }, config.SPONSORED_MINT_CAP

    state = str(row["status"])
    if state == "active" and int(row["enabled_until"]) <= now:
        state = "expired"
    active_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM free_mint_claims
            WHERE campaign_id = ?
              AND status IN ('reserved', 'minting', 'minted', 'offered', 'accepted')
            """,
            (row["id"],),
        ).fetchone()[0]
    )
    cap = int(row["cap"])
    remaining = max(0, cap - active_count)
    if state == "active" and remaining == 0:
        state = "at_capacity"
    accepting = state in {"active", "at_capacity"}
    possible_admissions = remaining if accepting else config.SPONSORED_MINT_CAP
    return (
        {
            "ok": not accepting,
            "state": state,
            "campaign_id": row["id"],
            "remaining": remaining,
        },
        possible_admissions,
    )


def _debt_check(conn: sqlite3.Connection, *, network: str) -> tuple[dict[str, Any], Decimal]:
    counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT b.status, COUNT(*) AS count
            FROM free_mint_burns AS b
            JOIN free_mint_claims AS c ON c.id = b.claim_id
            WHERE c.network = ?
              AND b.status IN ('pending', 'submitting', 'indeterminate', 'failed_terminal')
            GROUP BY b.status
            """,
            (network,),
        )
    }
    amount = Decimal("0")
    invalid_amounts = 0
    for row in conn.execute(
        """
        SELECT b.amount
        FROM free_mint_burns AS b
        JOIN free_mint_claims AS c ON c.id = b.claim_id
        WHERE c.network = ?
          AND b.status IN ('pending', 'submitting', 'indeterminate', 'failed_terminal')
        """,
        (network,),
    ):
        try:
            amount += Decimal(str(row["amount"]))
        except InvalidOperation:
            invalid_amounts += 1
    total = sum(counts.values())
    return (
        {
            "ok": total == 0 and invalid_amounts == 0,
            "pending": counts.get("pending", 0),
            "submitting": counts.get("submitting", 0),
            "indeterminate": counts.get("indeterminate", 0),
            "failed_terminal": counts.get("failed_terminal", 0),
            "amount": str(amount),
            "invalid_amounts": invalid_amounts,
        },
        amount,
    )


# `last_error` prefix the service writes on a 'minted' claim whose destination
# cannot receive the offer for a ledger-reported permanent reason (unfunded →
# tecNO_DST, or lsfDisallowIncomingNFTokenOffer → tecNO_PERMISSION; see
# lfg_service.app._recover_sponsored_offers). The NFT is still held by the
# issuer and is re-offered on a later boot once the HOLDER fixes their wallet;
# it is not a campaign blocker and must not fail readiness, or one such wallet
# blocks every future campaign (prod 2026-08-17 and 2026-08-20).
UNDELIVERABLE_ERROR_PREFIX = "destination account "


def _incomplete_check(conn: sqlite3.Connection, *, network: str) -> dict[str, Any]:
    counts = {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM free_mint_claims
            WHERE network = ? AND status IN ('reserved', 'minting', 'minted')
            GROUP BY status
            """,
            (network,),
        )
    }
    undeliverable = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM free_mint_claims
            WHERE network = ? AND status = 'minted' AND last_error LIKE ? || '%'
            """,
            (network, UNDELIVERABLE_ERROR_PREFIX),
        ).fetchone()[0]
    )
    minted = counts.get("minted", 0) - undeliverable
    return {
        "ok": counts.get("reserved", 0) + counts.get("minting", 0) + minted == 0,
        "reserved": counts.get("reserved", 0),
        "minting": counts.get("minting", 0),
        "minted": minted,
        "minted_undeliverable": undeliverable,
    }


async def build_report(
    *,
    network: str,
    app_db: str,
    history_db: str,
    now: int | None = None,
    balance_fetch: BalanceFetch | None = None,
) -> dict[str, Any]:
    """Build the automation report without opening either SQLite file writable."""

    if network != config.XRPL_NETWORK:
        raise ValueError(
            f"audit network {network!r} must match configured XRPL_NETWORK {config.XRPL_NETWORK!r}"
        )

    timestamp = int(time.time()) if now is None else int(now)
    checks: dict[str, dict[str, Any]] = {}
    possible_admissions = config.SPONSORED_MINT_CAP
    debt_amount = Decimal("0")

    try:
        with _readonly(app_db) as conn:
            schema_ok, detail = _schema_ok(conn, _APP_TABLE_COLUMNS)
            checks["schema"] = {"ok": schema_ok, "detail": detail}
            if schema_ok:
                checks["campaign"], possible_admissions = _campaign_check(
                    conn, network=network, now=timestamp
                )
                checks["debt"], debt_amount = _debt_check(conn, network=network)
                checks["incomplete_claims"] = _incomplete_check(conn, network=network)
            else:
                raise sqlite3.DatabaseError(detail)
    except (OSError, sqlite3.Error) as exc:
        checks.setdefault("schema", {"ok": False, "detail": str(exc)})
        checks.setdefault("campaign", {"ok": False, "state": "unknown", "detail": str(exc)})
        checks.setdefault("debt", {"ok": False, "detail": str(exc)})
        checks.setdefault("incomplete_claims", {"ok": False, "detail": str(exc)})

    archive_ok = False
    latest_close_time: int | None = None
    heartbeat_at: int | None = None
    unique_count: int | None = None
    baseline_coverage_raw: str | None = None
    effective_exclusions = sponsored_mint.excluded_wallets()
    try:
        with _readonly(history_db) as conn:
            schema_ok, detail = _schema_ok(
                conn,
                {"xrpl_txs": _HISTORY_COLUMNS, "archive_state": _ARCHIVE_STATE_COLUMNS},
            )
            if not schema_ok:
                raise sqlite3.DatabaseError(detail)
            state = history_store.get_archive_state(conn, network)
            binding_ok = bool(
                state is not None
                and state.source_tag == config.SOURCE_TAG
                and state.continuity_gap_at is None
                and state.continuity_gap_reason is None
            )
            archive_ok = binding_ok and sponsored_mint.archive_is_usable(
                history_db, network=network, now=timestamp
            )
            checks["archive"] = {
                "ok": archive_ok,
                "detail": detail if archive_ok else "provenance incomplete, mismatched, or stale",
                "network": state.network if state else None,
                "genesis_hash": state.genesis_hash if state else None,
                "baseline_complete": state.baseline_complete if state else False,
                "source_tag": state.source_tag if state else None,
                "baseline_ledger_min": state.baseline_ledger_min if state else None,
                "baseline_ledger_max": state.baseline_ledger_max if state else None,
                "baseline_provenance": state.baseline_provenance if state else None,
                "validated_ledger_index": state.validated_ledger_index if state else None,
                "baseline_coverage": state.baseline_coverage if state else None,
                "continuity_gap_at": state.continuity_gap_at if state else None,
                "continuity_gap_after": state.continuity_gap_after if state else None,
                "continuity_gap_before": state.continuity_gap_before if state else None,
                "continuity_gap_reason": state.continuity_gap_reason if state else None,
                "expected_source_tag": config.SOURCE_TAG,
            }
            latest_close_time = state.validated_close_time if state else None
            heartbeat_at = state.heartbeat_at if state else None
            baseline_coverage_raw = state.baseline_coverage if state else None
            excluded = tuple(sorted(effective_exclusions))
            marks = ",".join("?" for _ in excluded)
            exclusion_sql = f"AND trim(account) NOT IN ({marks})" if excluded else ""
            row = conn.execute(
                f"""
                SELECT COUNT(DISTINCT account) AS count
                FROM xrpl_txs
                WHERE source_tag = ? AND account IS NOT NULL AND trim(account) != ''
                {exclusion_sql}
                """,  # noqa: S608
                (config.SOURCE_TAG, *excluded),
            ).fetchone()
            unique_count = int(row["count"] or 0)
    except (OSError, sqlite3.Error) as exc:
        checks.setdefault("archive", {"ok": False, "detail": str(exc)})

    # A certification narrowed with --sources attests less than the archive
    # is trusted to prove; that is a FAIL, not a warning (#331). None means
    # no attestation at all (missing, unparseable, or pre-#331 version-1
    # coverage document, which cannot carry the sources field).
    swept_sources = sponsored_mint.baseline_coverage_sources(baseline_coverage_raw)
    missing_sources = sorted(sponsored_mint.BASELINE_REQUIRED_SOURCES - set(swept_sources or ()))
    checks["baseline_sources"] = {
        "ok": bool(swept_sources is not None and not missing_sources),
        "swept": swept_sources,
        "missing": missing_sources if swept_sources is not None else None,
        "required": sorted(sponsored_mint.BASELINE_REQUIRED_SOURCES),
    }

    close_age = timestamp - latest_close_time if latest_close_time is not None else None
    heartbeat_age = timestamp - heartbeat_at if heartbeat_at is not None else None
    max_age_seconds = config.SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS
    freshness_ok = (
        archive_ok
        and close_age is not None
        and heartbeat_age is not None
        and -60 <= close_age <= max_age_seconds
        and -60 <= heartbeat_age <= max_age_seconds
    )
    checks["listener_freshness"] = {
        "ok": freshness_ok,
        "latest_close_time": latest_close_time,
        "age_seconds": close_age,
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age,
        "max_age_seconds": max_age_seconds,
    }
    checks["unique_count"] = {
        "ok": archive_ok and unique_count is not None,
        "count": unique_count,
        "target": 300,
    }

    # Structural check (#334): the operator must have consciously declared a
    # non-empty, well-formed exclusion list. WHICH wallets belong in it is a
    # deployment fact, prescribed in docs/ops/sponsored-free-mint.md — the
    # operator reviews the reported sets, the audit only validates shape.
    configured_exclusions = {
        value.strip() for value in config.SPONSORED_MINT_EXCLUDED_WALLETS if value.strip()
    }
    invalid_exclusions = {
        value for value in configured_exclusions if not is_valid_classic_address(value)
    }
    exclusions_ok = bool(configured_exclusions) and not invalid_exclusions
    checks["exclusions"] = {
        "ok": exclusions_ok,
        "configured": sorted(configured_exclusions),
        "effective": sorted(effective_exclusions),
        "invalid": sorted(invalid_exclusions),
    }

    required_balance = debt_amount + Decimal(config.MINT_PRICE_LFGO) * possible_admissions
    if config.SIGNING_ACCOUNT == config.TOKEN_ISSUER_ADDRESS:
        testnet_noop = network == "testnet"
        checks["balance"] = {
            "ok": testnet_noop,
            "state": "not_applicable_testnet_self_issuer"
            if testnet_noop
            else "invalid_mainnet_self_issuer",
            "balance": None,
            "required": str(required_balance),
            "possible_admissions": possible_admissions,
        }
    else:
        if balance_fetch is None:

            async def balance_fetch() -> Decimal | None:
                return await xrpl_ops.get_trustline_balance(
                    config.SIGNING_ACCOUNT,
                    config.TOKEN_CURRENCY_HEX,
                    config.TOKEN_ISSUER_ADDRESS,
                )

        try:
            balance_result = balance_fetch()
            balance = (
                await balance_result if inspect.isawaitable(balance_result) else balance_result
            )
        except Exception:
            balance = None
        checks["balance"] = {
            "ok": balance is not None and balance >= required_balance,
            "state": "available" if balance is not None else "lookup_failed",
            "balance": str(balance) if balance is not None else None,
            "required": str(required_balance),
            "possible_admissions": possible_admissions,
        }

    return {
        "ok": all(bool(check.get("ok")) for check in checks.values()),
        "network": network,
        "app_db": os.path.abspath(app_db),
        "history_db": os.path.abspath(history_db),
        "checked_at": timestamp,
        "checks": checks,
    }


def _human_lines(report: dict[str, Any]) -> list[str]:
    checks = report["checks"]
    ordered = (
        ("schema", f"schema {checks['schema'].get('detail', '')}".strip()),
        ("campaign", f"campaign state={checks['campaign'].get('state', 'unknown')}"),
        ("archive", f"archive {checks['archive'].get('detail', '')}".strip()),
        (
            "baseline_sources",
            "baseline sources "
            f"swept={','.join(checks['baseline_sources'].get('swept') or []) or 'none'} "
            f"missing={','.join(checks['baseline_sources'].get('missing') or []) or 'none'}",
        ),
        (
            "listener_freshness",
            "latest archived ledger time="
            f"{checks['listener_freshness'].get('latest_close_time')} "
            f"age={checks['listener_freshness'].get('age_seconds')}s",
        ),
        ("unique_count", f"unique count={checks['unique_count'].get('count')}"),
        (
            "exclusions",
            "configured exclusions="
            f"{','.join(checks['exclusions'].get('configured', [])) or 'none'} "
            "invalid="
            f"{','.join(checks['exclusions'].get('invalid', [])) or 'none'}",
        ),
        (
            "balance",
            f"signing-wallet LFGO balance={checks['balance'].get('balance')} "
            f"required={checks['balance'].get('required')}",
        ),
        (
            "debt",
            "burn debt "
            f"pending={checks['debt'].get('pending', '?')} "
            f"submitting={checks['debt'].get('submitting', '?')} "
            f"indeterminate={checks['debt'].get('indeterminate', '?')}",
        ),
        (
            "incomplete_claims",
            "incomplete claims "
            f"reserved={checks['incomplete_claims'].get('reserved', '?')} "
            f"minting={checks['incomplete_claims'].get('minting', '?')} "
            f"minted={checks['incomplete_claims'].get('minted', '?')} "
            f"minted_undeliverable={checks['incomplete_claims'].get('minted_undeliverable', '?')}"
            " (holder must fund / allow incoming NFT offers; not a blocker)",
        ),
    )
    return [f"{'PASS' if checks[key].get('ok') else 'FAIL'} {detail}" for key, detail in ordered]


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only sponsored free-mint readiness audit.")
    parser.add_argument(
        "--network",
        choices=sorted(sponsored_mint.SUPPORTED_NETWORKS),
        default=config.XRPL_NETWORK,
        help="XRPL network name",
    )
    parser.add_argument("--app-db", help="override the per-network application DB")
    parser.add_argument("--history-db", help="override the per-network history archive")
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    args = parser.parse_args(argv)
    if args.network != config.XRPL_NETWORK:
        parser.error(
            f"--network {args.network!r} must match configured XRPL_NETWORK {config.XRPL_NETWORK!r}"
        )

    report = await build_report(
        network=args.network,
        app_db=args.app_db or db_path.app_db_path(args.network),
        history_db=args.history_db or history_store.history_db_path(args.network),
    )
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for line in _human_lines(report):
            print(line)
        print(f"{'PASS' if report['ok'] else 'FAIL'} sponsored free-mint readiness")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
