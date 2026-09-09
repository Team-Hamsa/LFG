"""Wallet activation-funder intelligence for sybil-resistance gates.

A wallet's funder is the ``Account`` of the earliest transaction that
activated it (a Payment with the wallet as ``Destination``). Funders never
change, so lookups are cached forever in the ``wallet_funders`` table of the
app DB. Exchange hot wallets aggregate unrelated people and are never treated
as a farm signal — that allowlist lives here so admission gates and the
ops clustering script share one list.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
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

# (funder classic address | None for a wallet with no transactions, ledger_index | None)
FunderResult = tuple[str | None, int | None]
FunderLookup = Callable[[str], FunderResult]


class FunderLookupError(Exception):
    """The funder could not be determined (RPC failure). Fail closed."""


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


def lookup_funder(wallet: str) -> FunderResult:
    """Resolve a wallet's activation funder from the ledger (one RPC).

    Raises FunderLookupError on any transport/endpoint failure so callers
    fail closed. Returns (None, None) for a wallet with no transactions.
    """
    from xrpl.clients import JsonRpcClient
    from xrpl.models.requests import AccountTx

    from lfg_core import config

    try:
        client = JsonRpcClient(config.JSON_RPC_URL)
        resp = client.request(AccountTx(account=wallet, forward=True, limit=1))
        result = resp.result
    except Exception as e:  # noqa: BLE001 - any transport failure fails closed
        raise FunderLookupError(str(e)) from e
    if result.get("error") != "actNotFound" and not resp.is_successful():
        raise FunderLookupError(str(result.get("error") or "unsuccessful account_tx"))
    return parse_account_tx(result, wallet)


def parse_account_tx(result: dict[str, Any], wallet: str) -> FunderResult:
    """Interpret an account_tx result for the wallet's activation funder.

    (None, None) means the account does not exist (actNotFound) — genuinely
    unfunded. An empty transactions list on an EXISTING account is a
    partial-history endpoint, not an unfunded wallet, and fails closed so a
    legitimate old account is never misclassified.
    """
    if result.get("error") == "actNotFound":
        return None, None
    txs = result.get("transactions") or []
    if not txs:
        raise FunderLookupError("account_tx returned no transactions (partial-history endpoint?)")
    t = txs[0]
    tx = t.get("tx") or t.get("tx_json") or {}
    ledger = t.get("ledger_index") or tx.get("ledger_index")
    if tx.get("TransactionType") == "Payment" and tx.get("Destination") == wallet:
        return tx.get("Account"), ledger
    # Activated some other way (e.g. first tx is its own) — no funder signal.
    return None, ledger
