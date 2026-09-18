# tests/closet_archive_helpers.py
# A real ledger history archive for the #522 stale-mirror tests: Closet versions
# are archived exactly the way the listener's _record_history stores them (the
# raw tx, then the NFT events derived from it), and read back through
# history_store like the flows' guard does.

import json
import os

from lfg_core import history_events, history_store


def closet_uri(name: str) -> str:
    """The on-chain hex URI of a Closet version (one CDN JSON per version)."""
    return f"https://cdn/closets/{name}.json".encode().hex().upper()


def closet_version_tx(
    kind: str, nft_id: str, uri_hex: str, ledger_index: int, *, owner: str = "rUser"
) -> dict:
    """A validated Closet NFTokenMint/NFTokenModify as the stream normalizes it."""
    tx: dict = {
        "Account": "rIssuer",
        "URI": uri_hex,
        "hash": f"{kind}{ledger_index}{nft_id}".upper()[:64].ljust(64, "0"),
        "ledger_index": ledger_index,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    if kind == "mint":
        tx["TransactionType"] = "NFTokenMint"
        tx["meta"]["nftoken_id"] = nft_id
    else:
        tx["TransactionType"] = "NFTokenModify"
        tx["NFTokenID"] = nft_id
        tx["Owner"] = owner
    return tx


def archive_tx(hconn, tx: dict) -> None:
    history_store.insert_tx(
        hconn,
        tx_hash=tx["hash"],
        ledger_index=tx["ledger_index"],
        close_time=None,
        tx_type=tx["TransactionType"],
        account=tx.get("Account"),
        source_tag=tx.get("SourceTag"),
        raw_json=json.dumps(tx, sort_keys=True),
    )
    for ev in history_events.derive_nft_events(tx, nft_issuer="rIssuer"):
        history_store.insert_nft_event(hconn, ev)
    hconn.commit()


def closet_archive(tmp_path, nft_id: str, versions: list[tuple[str, str, int]]) -> str:
    """Create a history archive holding `versions` ((kind, uri_hex, ledger)) of
    `nft_id`; returns its path."""
    path = os.path.join(str(tmp_path), "history_archive.db")
    hconn = history_store.init_history_db(path)
    try:
        for kind, uri_hex, ledger_index in versions:
            archive_tx(hconn, closet_version_tx(kind, nft_id, uri_hex, ledger_index))
    finally:
        hconn.close()
    return path
