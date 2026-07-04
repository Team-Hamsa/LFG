#!/usr/bin/env python3
"""Rebuild nft_events / brix_events from the raw xrpl_txs archive.

  python scripts/derive_history_events.py --network mainnet [--distributor rXXX]

Derived tables are droppable: this clears and rebuilds them in one pass.
Also invoked by backfill_history.py --derive-only."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, history_events, history_store  # noqa: E402

BRIX_HEX = "4252495800000000000000000000000000000000"


def rederive(
    hconn: Any,
    network: str,
    *,
    distributor: str | None = None,
    oconn: Any = None,
    nft_issuer: str | None = None,
    brix_issuer: str | None = None,
) -> dict[str, int]:
    from lfg_core import nft_index

    nft_issuer = nft_issuer or config.SWAP_ISSUER_ADDRESS
    brix_issuer = brix_issuer or config.SWAP_OFFER_ISSUER
    if oconn is None:
        oconn = nft_index.init_db(nft_index.index_db_path(network))
    numbers = dict(oconn.execute("SELECT nft_id, nft_number FROM onchain_nfts"))

    history_store.clear_derived(hconn)
    n_nft = n_brix = 0
    for row in hconn.execute("SELECT raw_json FROM xrpl_txs ORDER BY ledger_index"):
        tx = json.loads(row["raw_json"])
        for ev in history_events.derive_nft_events(tx, nft_issuer=nft_issuer):
            ev["nft_number"] = numbers.get(ev["nft_id"])
            history_store.insert_nft_event(hconn, ev)
            n_nft += 1
        for ev in history_events.derive_brix_events(
            tx, brix_issuer=brix_issuer, brix_hex=BRIX_HEX, distributor=distributor
        ):
            history_store.insert_brix_event(hconn, ev)
            n_brix += 1
    hconn.commit()
    return {"nft_events": n_nft, "brix_events": n_brix}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Rebuild derived history events.")
    parser.add_argument("--network", default=config.XRPL_NETWORK)
    parser.add_argument("--distributor")
    args = parser.parse_args()
    hconn = history_store.init_history_db(history_store.history_db_path(args.network))
    counts = rederive(hconn, args.network, distributor=args.distributor)
    print(f"[{args.network}] derived: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
