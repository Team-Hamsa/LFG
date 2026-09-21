"""Wallet activation-funder intelligence for sybil-resistance gates.

A wallet's funder is the ``Account`` of the transaction that created its
AccountRoot (in practice the Payment that first sent it the reserve). Funders
never change, so lookups are cached forever in the ``wallet_funders`` table of
the app DB.

The creating transaction is identified from its metadata, never by position:
``account_tx`` lists every transaction that touches an address, so a wallet's
oldest entry can predate its creation (another account naming it as a
``SetRegularKey``, a failed payment to it), and a history-pruned node's oldest
entry is just the oldest one it kept. Lookups go to the clio endpoint
(``config.CLIO_WS_URL``, full history), never the JSON-RPC pool, whose primary
in prod is a pruned local validator. Exchange hot wallets aggregate unrelated people and are never treated
as a farm signal — that allowlist lives here so admission gates and the
ops clustering script share one list.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Known exchange/custodial hot wallets seen funding user wallets (XRPScan
# labels). An exchange funder aggregates unrelated people — never a farm
# signal, so same-exchange-funded wallets are not deduped.
EXCHANGES = {
    "rBtttd61FExHC68vsZ8dqmS3DfjFEceA1A": "Binance",
    "rDAE53VfMvftPB4ogpWGWvzkQxfht6JPxr": "Binance",
    "rMdG3ju8pgyVh29ELPWaDuA74CpWW6Fxns": "Uphold",
    "rfKsmLP6sTfVGDvga6rW6XbmSFUzc3G9f3": "Bitrue",
    "rMvCasZ9cohYrSZRNYPTZfoaaSUQMfgQ8G": "Bybit",
    "rwWr7KUZ3ZFwzgaDGjKBysADByzxvohQ3C": "Indodax",
    "rU2mEJSLqBRkYLVTv55rFTgQajkLTnT6mA": "Coins.ph",
    "r3BsMjVmAeTuAChrqHMVeLQWxt7BfQC1L1": "Indodax",
}
# Every other hot wallet XRPScan labels for the same exchanges/custodians. The
# hand-picked eight above missed most of them, so the sponsored-mint funder
# gate read every second Coinbase/Binance/KuCoin customer as a sybil sibling
# of the first (2026-09-19 campaign).
#
# The snapshot is NOT a verbatim copy of a third party's classification: it is
# XRPScan's well-known list filtered to the exchanges and custodians reviewed
# in scripts/refresh_exchange_funders.py, which regenerates this file and
# prints every added/removed account for review. An entry naming any other
# operator fails tests/test_funding.py, so a broadened upstream label cannot
# silently widen this exemption.
EXCHANGES.update(
    json.loads((Path(__file__).with_name("exchange_funders.json")).read_text())["accounts"]
)

# (funder classic address | None for a wallet with no transactions, ledger_index | None)
FunderResult = tuple[str | None, int | None]
FunderLookup = Callable[[str], FunderResult]


class FunderLookupError(Exception):
    """The funder could not be determined (RPC failure). Fail closed."""


# account_tx pages scanned for the creating transaction before giving up. It
# is usually on the first page, but an address that served as another
# account's regular key before it was funded lists every transaction it
# signed for that account first (rKDFM3…: hundreds).
ACTIVATION_PAGE_LIMIT = 100
ACTIVATION_MAX_PAGES = 50


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_funders (
            wallet       TEXT PRIMARY KEY,
            funder       TEXT,
            ledger_index INTEGER,
            looked_up_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def cached_funder(conn: sqlite3.Connection, wallet: str) -> str | None:
    row = conn.execute("SELECT funder FROM wallet_funders WHERE wallet = ?", (wallet,)).fetchone()
    return row[0] if row else None


def has_cached_funder(conn: sqlite3.Connection, wallet: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM wallet_funders WHERE wallet = ?", (wallet,)).fetchone()
        is not None
    )


def record_funder(
    conn: sqlite3.Connection, wallet: str, funder: str | None, ledger_index: int | None
) -> None:
    conn.execute(
        """
        INSERT INTO wallet_funders (wallet, funder, ledger_index) VALUES (?, ?, ?)
        ON CONFLICT(wallet) DO NOTHING
        """,
        (wallet, funder, ledger_index),
    )


def lookup_funder(wallet: str, *, url: str | None = None) -> FunderResult:
    """Resolve a wallet's activation funder from full ledger history.

    Tries each of ``config.HISTORY_WS_URLS`` (clio first) in turn, or only
    ``url`` when given, and returns the first funder found. ``actNotFound`` is
    one endpoint's view (a lagging node can miss a just-funded account), so
    (None, None) is returned only when EVERY endpoint says the account does
    not exist. Raises FunderLookupError when no endpoint found a funder and
    at least one could not answer, so callers fail closed.
    """
    from lfg_core import config

    last: FunderLookupError | None = None
    endpoints = (url,) if url else config.HISTORY_WS_URLS
    for endpoint in endpoints:
        try:
            result = _lookup_on(endpoint, wallet)
        except FunderLookupError as e:
            logging.warning(f"funder lookup for {wallet} on {endpoint} failed: {e}")
            last = e
            continue
        if result != (None, None):
            return result
    if last is not None:
        raise last
    if not endpoints:
        raise FunderLookupError("no history endpoint configured")
    return None, None


def _lookup_on(endpoint: str, wallet: str) -> FunderResult:
    """One endpoint's answer: page ``account_tx`` oldest-first until a
    transaction's metadata creates the wallet's AccountRoot. Raises
    FunderLookupError on a transport or server error, on an endpoint without
    full history, and when no creating transaction is found."""
    from xrpl.clients import WebsocketClient
    from xrpl.models.requests import AccountTx

    marker: Any = None
    try:
        with WebsocketClient(endpoint) as client:
            for _ in range(ACTIVATION_MAX_PAGES):
                resp = client.request(
                    AccountTx(
                        account=wallet, forward=True, limit=ACTIVATION_PAGE_LIMIT, marker=marker
                    )
                )
                result = resp.result
                if result.get("error") == "actNotFound":
                    return None, None
                if not resp.is_successful():
                    raise FunderLookupError(str(result.get("error") or "unsuccessful account_tx"))
                _require_full_history(result)
                found = activation_in(result.get("transactions") or [], wallet)
                if found is not None:
                    return found
                marker = result.get("marker")
                if marker is None:
                    break
    except FunderLookupError:
        raise
    except Exception as e:  # noqa: BLE001 - any transport failure fails closed
        raise FunderLookupError(str(e)) from e
    raise FunderLookupError(f"no transaction creating {wallet} found in its history")


def _require_full_history(result: dict[str, Any]) -> None:
    """Refuse a mainnet answer from a node that does not reach back to the
    earliest ledger any node serves: a wallet created before its floor would
    otherwise never find its creating transaction (or, if it was deleted and
    re-created, find the wrong one)."""
    from lfg_core import config, history_store

    if config.XRPL_NETWORK != "mainnet":
        return
    floor = result.get("ledger_index_min")
    if not isinstance(floor, int) or floor > history_store.EARLIEST_AVAILABLE_LEDGER:
        raise FunderLookupError(f"endpoint lacks full history (ledger_index_min={floor})")


def activation_in(transactions: list[dict[str, Any]], wallet: str) -> FunderResult | None:
    """(signer, ledger) of the transaction whose metadata creates `wallet`'s
    AccountRoot, or None if none of `transactions` does."""
    for t in transactions:
        meta = t.get("meta") or t.get("metaData")
        if not isinstance(meta, dict) or meta.get("TransactionResult") != "tesSUCCESS":
            continue
        for node in meta.get("AffectedNodes") or []:
            created = node.get("CreatedNode") or {}
            if (
                created.get("LedgerEntryType") == "AccountRoot"
                and (created.get("NewFields") or {}).get("Account") == wallet
            ):
                tx = t.get("tx") or t.get("tx_json") or {}
                return tx.get("Account"), t.get("ledger_index") or tx.get("ledger_index")
    return None


def warm_funder_cache(db_path: str, wallet: str, *, lookup: FunderLookup | None = None) -> bool:
    """Best-effort, login-time cache warm for the funder gate.

    Resolves and records the wallet's activation funder if it is not cached
    yet, so the sponsored-mint reservation (and the eligibility preview) find
    it locally instead of paying the RPC on the mint hot path. Returns True
    only when a new row was written. Mirrors the reservation's caching rule:
    an unfunded (None, None) result is transient and is NOT cached. Never
    raises — a lookup failure just leaves the cache cold for the reservation
    to retry.
    """
    wallet = wallet.strip()
    if not wallet:
        return False
    resolver = lookup or lookup_funder
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_schema(conn)
            if has_cached_funder(conn, wallet):
                return False
        funder, ledger = resolver(wallet)
        if funder is None and ledger is None:
            return False
        with sqlite3.connect(db_path) as conn:
            record_funder(conn, wallet, funder, ledger)
        return True
    except Exception as e:  # noqa: BLE001 - best-effort: the whole op is contained
        logging.warning(f"warm_funder_cache({wallet}) skipped: {e}")
        return False


def parse_account_tx(result: dict[str, Any], wallet: str) -> FunderResult:
    """Interpret one complete account_tx result for the wallet's funder.

    (None, None) means the account does not exist (actNotFound) — genuinely
    unfunded. An empty transactions list on an EXISTING account, or a list
    with no transaction creating it, is a partial-history answer and fails
    closed so a legitimate account is never misclassified.
    """
    if result.get("error") == "actNotFound":
        return None, None
    txs = result.get("transactions") or []
    if not txs:
        raise FunderLookupError("account_tx returned no transactions (partial-history endpoint?)")
    found = activation_in(txs, wallet)
    if found is None:
        raise FunderLookupError(f"no transaction creating {wallet} in this account_tx page")
    return found
