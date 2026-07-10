"""Leaderboard queries: period math + user/NFT boards over history events.

Boards operate over two connections:
- `hconn`: per-network history DB (`history_store`) — `nft_events`, `brix_events`.
- `oconn`: per-network on-chain index DB (`nft_index`) — `onchain_nfts`.

Boards (this module):
- `users_nfts`: NFTs held per wallet. All-time (`start_ts == 0`) reads the
  live on-chain index (`onchain_nfts`) for a point-in-time census. Windowed
  queries instead net acquisitions/dispositions from `nft_events` in the
  window (mint/transfer/sale in +1, transfer/sale/burn out -1), keeping only
  positive totals — this is a *delta within the window*, not a live balance.
- `users_swaps`: trait swaps per receiving wallet — `modify` events PLUS
  non-assemble "rebirth" deliveries (see below). Legacy trait swaps are
  burn+remint, not NFTokenModify — mainnet has ZERO modify events — so
  counting only `modify` left the swap boards empty there.
- `users_builds`: count of "rebirth" deliveries — an issuer -> user transfer
  of a token that is a re-mint of a previously burned edition (same
  `nft_number`, different `nft_id`, burned before the current token's mint)
  — whose mint carries the provenance memo `action=assemble` (#54). Rebirths
  without that memo are legacy remint swaps (or admin re-offers), not
  builds; they belong to the swap boards instead.
- `nft_swaps`: swaps per *edition* (`modify` + non-assemble rebirths, keyed
  on `nft_number` since a remint swap changes the token's `nft_id`).

`system_accounts` (nft_issuer, brix_issuer, distributor, amm_account) are
always excluded from user-keyed boards via `wallet NOT IN (...)`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

Row = dict[str, Any]
BoardFn = Callable[
    [sqlite3.Connection, sqlite3.Connection, int, int, frozenset[str], int], list[Row]
]

_LIMIT = 25


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def period_bounds(period: str, start: str | None, *, now: int) -> tuple[int, int]:
    """Return (start_ts, end_ts) unix UTC seconds for a named period.

    `period` in {today, week, month, year, all}. `start` (ISO date, e.g.
    "2026-06-30") anchors a specific past occurrence of that period; when
    omitted, the *current* occurrence (relative to `now`) is used and
    `end_ts == now`. Weeks are Monday-anchored. `all` ignores `start` and
    returns (0, now).
    """
    if period == "all":
        return 0, now

    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    anchor = _parse_date(start) if start is not None else now_dt

    if period == "today":
        day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = int(day_start.timestamp())
        end_ts = now if start is None else int((day_start + timedelta(days=1)).timestamp())
        return start_ts, end_ts

    if period == "week":
        day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        monday = day_start - timedelta(days=day_start.weekday())
        start_ts = int(monday.timestamp())
        end_ts = now if start is None else int((monday + timedelta(days=7)).timestamp())
        return start_ts, end_ts

    if period == "month":
        month_start = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        y, m = month_start.year, month_start.month
        next_y, next_m = y + m // 12, m % 12 + 1
        next_month_start = month_start.replace(year=next_y, month=next_m)
        start_ts = int(month_start.timestamp())
        end_ts = now if start is None else int(next_month_start.timestamp())
        return start_ts, end_ts

    if period == "year":
        year_start = anchor.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        next_year_start = year_start.replace(year=year_start.year + 1)
        start_ts = int(year_start.timestamp())
        end_ts = now if start is None else int(next_year_start.timestamp())
        return start_ts, end_ts

    raise ValueError(f"unknown period: {period!r}")


def _exclude_clause(col: str, system_accounts: frozenset[str]) -> tuple[str, list[str]]:
    if not system_accounts:
        return "", []
    placeholders = ",".join("?" * len(system_accounts))
    return f" AND {col} NOT IN ({placeholders})", list(system_accounts)


def _row(
    *,
    wallet: str | None = None,
    nft_id: str | None = None,
    nft_number: int | None = None,
    value: float,
) -> Row:
    return {"wallet": wallet, "nft_id": nft_id, "nft_number": nft_number, "value": value}


def _board_users_nfts(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    if start_ts == 0:
        excl, params = _exclude_clause("owner", system_accounts)
        sql = (
            "SELECT owner, COUNT(*) AS value FROM onchain_nfts"
            " WHERE is_burned=0 AND owner IS NOT NULL"
            + excl
            + " GROUP BY owner ORDER BY value DESC LIMIT ?"
        )
        cur = oconn.execute(sql, (*params, limit))
        return [_row(wallet=r["owner"], value=r["value"]) for r in cur.fetchall()]

    excl_to, params_to = _exclude_clause("to_addr", system_accounts)
    excl_from, params_from = _exclude_clause("from_addr", system_accounts)
    sql = f"""
        SELECT wallet, SUM(delta) AS value FROM (
            SELECT to_addr AS wallet, 1 AS delta FROM nft_events
            WHERE event IN ('mint','transfer','sale') AND to_addr IS NOT NULL
              AND ts >= ? AND ts < ?{excl_to}
            UNION ALL
            SELECT from_addr AS wallet, -1 AS delta FROM nft_events
            WHERE event IN ('transfer','sale','burn') AND from_addr IS NOT NULL
              AND ts >= ? AND ts < ?{excl_from}
        ) GROUP BY wallet HAVING SUM(delta) > 0 ORDER BY value DESC LIMIT ?
    """
    query_params: tuple[Any, ...] = (
        start_ts,
        end_ts,
        *params_to,
        start_ts,
        end_ts,
        *params_from,
        limit,
    )
    cur = hconn.execute(sql, query_params)
    return [_row(wallet=r["wallet"], value=r["value"]) for r in cur.fetchall()]


# A "rebirth" is an issuer -> user delivery of a token whose edition was
# previously burned under a different nft_id: the on-chain shape of BOTH a
# legacy burn+remint trait swap AND an economy assemble. The provenance memo
# on the mint (#54) is the only distinguisher: assembles are stamped
# action=assemble; legacy remints predate memos entirely (NULL).
_REBIRTH_IS_ASSEMBLE = "m.memo_action = 'assemble'"
_REBIRTH_NOT_ASSEMBLE = "(m.memo_action IS NULL OR m.memo_action != 'assemble')"


def _rebirth_sql(select_cols: str, group_by: str, memo_cond: str, issuer_ph: str, excl: str) -> str:
    """Rebirth-delivery subquery. Param order: (*issuers, start_ts, end_ts,
    *exclude_params). Any system account counts as a delivering issuer —
    broader than strictly the NFT issuer, but harmless since only the NFT
    issuer mints/burns NFTs."""
    return f"""
        SELECT {select_cols}, COUNT(*) AS value
        FROM nft_events t
        JOIN nft_events m ON m.nft_id = t.nft_id AND m.event = 'mint'
        WHERE t.event IN ('transfer','sale') AND t.from_addr IN ({issuer_ph})
          AND t.ts >= ? AND t.ts < ?
          AND {memo_cond}
          AND EXISTS (SELECT 1 FROM nft_events b
                      WHERE b.event='burn' AND b.nft_number = t.nft_number
                        AND b.nft_id != t.nft_id AND b.ts < m.ts){excl}
        GROUP BY {group_by}
    """


def _board_users_swaps(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    excl_mod, excl_mod_params = _exclude_clause("to_addr", system_accounts)
    issuers = list(system_accounts) if system_accounts else [None]
    issuer_ph = ",".join("?" * len(issuers))
    excl_rb, excl_rb_params = _exclude_clause("t.to_addr", system_accounts)
    rebirths = _rebirth_sql(
        "t.to_addr AS wallet", "t.to_addr", _REBIRTH_NOT_ASSEMBLE, issuer_ph, excl_rb
    )
    sql = f"""
        SELECT wallet, SUM(value) AS value FROM (
            SELECT to_addr AS wallet, COUNT(*) AS value FROM nft_events
             WHERE event='modify' AND ts>=? AND ts<?{excl_mod} GROUP BY to_addr
            UNION ALL
            {rebirths}
        ) GROUP BY wallet ORDER BY value DESC LIMIT ?
    """
    params = (
        start_ts,
        end_ts,
        *excl_mod_params,
        *issuers,
        start_ts,
        end_ts,
        *excl_rb_params,
        limit,
    )
    cur = hconn.execute(sql, params)
    return [_row(wallet=r["wallet"], value=r["value"]) for r in cur.fetchall()]


def _board_users_builds(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    issuers = list(system_accounts) if system_accounts else [None]
    excl, excl_params = _exclude_clause("t.to_addr", system_accounts)
    issuer_ph = ",".join("?" * len(issuers))
    sql = (
        _rebirth_sql("t.to_addr AS wallet", "t.to_addr", _REBIRTH_IS_ASSEMBLE, issuer_ph, excl)
        + " ORDER BY value DESC LIMIT ?"
    )
    params = (*issuers, start_ts, end_ts, *excl_params, limit)
    cur = hconn.execute(sql, params)
    return [_row(wallet=r["wallet"], value=r["value"]) for r in cur.fetchall()]


def _board_nft_swaps(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    issuers = list(system_accounts) if system_accounts else [None]
    issuer_ph = ",".join("?" * len(issuers))
    rebirths = _rebirth_sql(
        "t.nft_id AS nft_id, t.nft_number AS nft_number",
        "t.nft_id",
        _REBIRTH_NOT_ASSEMBLE,
        issuer_ph,
        "",
    )
    # Keyed per edition (a remint swap changes the token's nft_id); rows with
    # no known edition fall back to keying on nft_id.
    sql = f"""
        SELECT MAX(nft_id) AS nft_id, MAX(nft_number) AS nft_number, SUM(value) AS value FROM (
            SELECT nft_id, nft_number, COUNT(*) AS value FROM nft_events
             WHERE event='modify' AND ts>=? AND ts<? GROUP BY nft_id
            UNION ALL
            {rebirths}
        ) GROUP BY COALESCE(nft_number, nft_id) ORDER BY value DESC LIMIT ?
    """
    cur = hconn.execute(sql, (start_ts, end_ts, *issuers, start_ts, end_ts, limit))
    return [
        _row(nft_id=r["nft_id"], nft_number=r["nft_number"], value=r["value"])
        for r in cur.fetchall()
    ]


def _snapshot_ts(snap_date: str) -> int:
    return int(datetime.strptime(snap_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


_SNAPSHOT_COLUMNS = frozenset({"brix", "lp_tokens"})


def _snapshot_values(
    hconn: sqlite3.Connection, column: str, as_of_ts: int | None
) -> dict[str, float]:
    """account -> value for `column` at the latest snapshot <= as_of_ts (None = latest overall)."""
    if column not in _SNAPSHOT_COLUMNS:
        raise ValueError(f"invalid snapshot column: {column!r}")
    rows = hconn.execute(f"SELECT snap_date, account, {column} AS value FROM balance_snapshots")
    latest: dict[str, tuple[str, float]] = {}
    for r in rows.fetchall():
        snap_date = r["snap_date"]
        if as_of_ts is not None and _snapshot_ts(snap_date) > as_of_ts:
            continue
        account = r["account"]
        prev = latest.get(account)
        if prev is None or snap_date > prev[0]:
            latest[account] = (snap_date, r["value"])
    return {account: value for account, (_, value) in latest.items()}


def _board_balance_snapshot(
    column: str,
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    if start_ts == 0:
        values = _snapshot_values(hconn, column, None)
        rows = [
            _row(wallet=account, value=value)
            for account, value in values.items()
            if account not in system_accounts
        ]
    else:
        end_values = _snapshot_values(hconn, column, end_ts)
        start_values = _snapshot_values(hconn, column, start_ts)
        rows = []
        for account, end_value in end_values.items():
            if account in system_accounts:
                continue
            delta = end_value - start_values.get(account, 0)
            if delta > 0:
                rows.append(_row(wallet=account, value=delta))
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:limit]


def _board_brix_rich(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    return _board_balance_snapshot("brix", hconn, oconn, start_ts, end_ts, system_accounts, limit)


def _board_brix_lp(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    return _board_balance_snapshot(
        "lp_tokens", hconn, oconn, start_ts, end_ts, system_accounts, limit
    )


def _board_brix_earned(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
    earn_sources: frozenset[str] | None = None,
) -> list[Row]:
    sources = earn_sources if earn_sources is not None else system_accounts
    source_list = list(sources) if sources else [None]
    placeholders = ",".join("?" * len(source_list))
    excl, excl_params = _exclude_clause("account", system_accounts)
    sql = f"""
        SELECT account, SUM(delta) AS value FROM brix_events
        WHERE delta > 0
          AND (kind IN ('airdrop','claim')
               OR (kind='payment' AND counterparty IN ({placeholders})))
          AND ts >= ? AND ts < ?
          {excl}
        GROUP BY account ORDER BY value DESC LIMIT ?
    """
    params = (*source_list, start_ts, end_ts, *excl_params, limit)
    cur = hconn.execute(sql, params)
    return [_row(wallet=r["account"], value=r["value"]) for r in cur.fetchall()]


def _nft_rarity(
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    start_ts: int,
    end_ts: int,
    system_accounts: frozenset[str],
    limit: int,
) -> list[Row]:
    import json as _j
    from collections import Counter

    rows = oconn.execute(
        "SELECT nft_id, nft_number, attributes_json FROM onchain_nfts"
        " WHERE is_burned=0 AND attributes_json IS NOT NULL AND attributes_json != ''"
    ).fetchall()
    token_traits: dict[str, tuple[int | None, list[tuple[str, str]]]] = {}
    freq: Counter[tuple[str, str]] = Counter()
    for r in rows:
        try:
            attrs = _j.loads(r["attributes_json"])
        except ValueError:
            continue
        pairs = [
            (str(t.get("trait_type")), str(t.get("value")))
            for t in attrs
            if isinstance(t, dict) and t.get("trait_type") is not None
        ]
        token_traits[r["nft_id"]] = (r["nft_number"], pairs)
        freq.update(pairs)
    n_live = len(token_traits) or 1
    scored = [
        _row(
            nft_id=nft_id,
            nft_number=number,
            value=round(sum(n_live / freq[p] for p in pairs), 2),
        )
        for nft_id, (number, pairs) in token_traits.items()
        if pairs
    ]
    scored.sort(key=lambda x: x["value"], reverse=True)
    return scored[:limit]


BOARDS: dict[str, BoardFn] = {
    "users_nfts": _board_users_nfts,
    "users_swaps": _board_users_swaps,
    "users_builds": _board_users_builds,
    "nft_swaps": _board_nft_swaps,
    "brix_rich": _board_brix_rich,
    "brix_lp": _board_brix_lp,
    "brix_earned": _board_brix_earned,
    "nft_rarity": _nft_rarity,
}


def compute(
    board: str,
    hconn: sqlite3.Connection,
    oconn: sqlite3.Connection,
    *,
    start_ts: int,
    end_ts: int,
    network: str,
    system_accounts: frozenset[str],
    limit: int = _LIMIT,
    earn_sources: frozenset[str] | None = None,
) -> list[Row]:
    """Compute a leaderboard's rows, sorted desc, top `limit` (default 25)."""
    fn = BOARDS.get(board)
    if fn is None:
        raise ValueError(f"unknown board: {board!r}")
    if board == "brix_earned":
        return _board_brix_earned(
            hconn, oconn, start_ts, end_ts, system_accounts, limit, earn_sources
        )
    return fn(hconn, oconn, start_ts, end_ts, system_accounts, limit)
