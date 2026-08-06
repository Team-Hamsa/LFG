"""Shared durable sponsored-mint test setup."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from lfg_core import config, history_store, sponsored_mint

# Fixture ledger ranges must sit above the real earliest-available ledger (32570).
L0 = history_store.EARLIEST_AVAILABLE_LEDGER


def ready_history(
    path: str,
    *,
    network: str = "mainnet",
    now: int = 101,
    close_time: int | None = None,
    sources: tuple[str, ...] | None = None,
) -> None:
    conn = history_store.init_history_db(path)
    genesis_hash = f"{network}-test-genesis"
    history_store.record_archive_baseline(
        conn,
        network=network,
        genesis_hash=genesis_hash,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 1,
        provenance="pytest-authoritative-baseline",
        completed_at=now - 1,
        coverage=json.dumps(
            {
                "version": sponsored_mint.BASELINE_COVERAGE_VERSION,
                "source_tag": config.SOURCE_TAG,
                "ledger_min": history_store.EARLIEST_AVAILABLE_LEDGER,
                "ledger_max": L0 + 1,
                "sources": sorted(
                    sponsored_mint.BASELINE_REQUIRED_SOURCES if sources is None else sources
                ),
                "accounts": {
                    "signing": config.SIGNING_ACCOUNT,
                    "token_issuer": config.TOKEN_ISSUER_ADDRESS,
                },
            }
        ),
    )
    history_store.record_validated_ledger(
        conn,
        network=network,
        genesis_hash=genesis_hash,
        ledger_index=L0 + 2,
        close_time=now if close_time is None else close_time,
        observed_at=now,
    )
    conn.close()


def prepare_and_forward(
    store: Any,
    db_path: str,
    *,
    network: str,
    wallet: str,
    session_id: str,
    now: int,
    tx_hash: str | None = None,
) -> Any:
    identity = (
        tx_hash or hashlib.sha256(f"{network}:{wallet}:{session_id}".encode()).hexdigest().upper()
    )
    store.record_mint_prepared(
        db_path,
        network=network,
        wallet=wallet,
        session_id=session_id,
        tx_hash=identity,
        tx_blob=f"BLOB:{identity}",
        signed_ledger_floor=1,
        nft_number=1,
        metadata_url="https://cdn.example/1.json",
        metadata_json=json.dumps(
            {
                "name": "LFG #1",
                "image": "https://cdn.example/1.png",
                "attributes": [
                    {"trait_type": "Body", "value": "Alien"},
                    {"trait_type": "Head", "value": "Cap"},
                ],
            }
        ),
        body_type="Alien",
        now=now,
    )
    return store.mark_mint_forwarded(
        db_path,
        network=network,
        wallet=wallet,
        session_id=session_id,
        tx_hash=identity,
        now=now,
    )
