#!/usr/bin/env python3
"""One-time (resumable, idempotent) ledger-history backfill.

  python scripts/backfill_history.py --network mainnet
  python scripts/backfill_history.py --network mainnet --distributor rXXX
  python scripts/backfill_history.py --network mainnet --derive-only

Sources: account_tx over the NFT issuer, the BRIX issuer, and (if given) the
airdrop distributor; clio nft_history per nft_id known to onchain_<net>.db.
Pagination markers persist to backfill_state after every page, so Ctrl-C and
re-run is always safe. Derivation (Task 5) rebuilds nft_events/brix_events
from the raw rows."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from xrpl.asyncio.clients import AsyncWebsocketClient  # noqa: E402
from xrpl.models.requests import Request  # noqa: E402

from lfg_core import history_events, history_store  # noqa: E402

PAGE_LIMIT = 200
REQUEST_TIMEOUT = 60
# clio rate-limits public endpoints ('slowDown'); pace requests and back off.
THROTTLE_SECONDS = 0.25
RETRYABLE_ERRORS = {"slowDown", "tooBusy"}
RETRY_MAX = 8
RETRY_BASE_DELAY = 5.0
VALID_SOURCES = {"issuer", "brix", "distributor", "nfts"}


def store_raw_tx(conn: Any, tx: dict[str, Any]) -> bool:
    return history_store.insert_tx(
        conn,
        tx_hash=str(tx.get("hash")),
        ledger_index=tx.get("ledger_index"),
        close_time=history_events.tx_unix_time(tx),
        tx_type=str(tx.get("TransactionType", "")),
        account=tx.get("Account"),
        source_tag=tx.get("SourceTag"),
        raw_json=json.dumps(tx, sort_keys=True),
    )


async def backfill_account_tx(conn: Any, request_fn: Any, account: str, source: str) -> int:
    """Page account_tx forward, persisting the marker after every page."""
    stored = history_store.get_cursor(conn, source)
    marker: Any = json.loads(stored) if stored else None
    new = 0
    while True:
        req: dict[str, Any] = {
            "method": "account_tx",
            "account": account,
            "ledger_index_min": -1,
            "ledger_index_max": -1,
            "limit": PAGE_LIMIT,
            "forward": True,
        }
        if marker:
            req["marker"] = marker
        result = await request_fn(req)
        for entry in result.get("transactions", []):
            if entry.get("validated") is False:
                continue
            tx = history_events.normalize_entry(entry)
            if store_raw_tx(conn, tx):
                new += 1
        marker = result.get("marker")
        history_store.set_cursor(conn, source, json.dumps(marker) if marker else None)
        if not marker:
            return new


async def backfill_nft_history(conn: Any, request_fn: Any, nft_id: str) -> int:
    """Full nft_history (clio) for one token; cursor keyed per nft_id.

    The pagination marker is persisted after every page (like
    backfill_account_tx) so an interrupted long token history resumes from
    where it left off instead of restarting from page 1."""
    source = f"nft_history:{nft_id}"
    stored = history_store.get_cursor(conn, source)
    if stored == "done":
        return 0
    marker: Any = json.loads(stored) if stored else None
    new = 0
    while True:
        req: dict[str, Any] = {"method": "nft_history", "nft_id": nft_id, "limit": 100}
        if marker:
            req["marker"] = marker
        result = await request_fn(req)
        for entry in result.get("transactions", []):
            tx = history_events.normalize_entry(entry)
            if store_raw_tx(conn, tx):
                new += 1
        marker = result.get("marker")
        history_store.set_cursor(conn, source, json.dumps(marker) if marker else "done")
        if not marker:
            return new


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import backfill_onchain as bf

    from lfg_core import config, nft_index

    parser = argparse.ArgumentParser(description="Ledger history backfill.")
    parser.add_argument("--network", choices=sorted(bf.NETWORKS), default=config.XRPL_NETWORK)
    parser.add_argument("--distributor", help="airdrop distributor wallet to scrape")
    parser.add_argument("--sources", default="issuer,brix,distributor,nfts")
    parser.add_argument("--derive-only", action="store_true")
    args = parser.parse_args()

    wanted = set(args.sources.split(","))
    unknown = wanted - VALID_SOURCES
    if unknown:
        parser.error(f"unknown --sources value(s): {', '.join(sorted(unknown))}")

    net = bf.NETWORKS[args.network]
    clio = net["clio"]
    issuer = net["issuer"] or config.SWAP_ISSUER_ADDRESS
    conn = history_store.init_history_db(history_store.history_db_path(args.network))

    if args.derive_only:
        from derive_history_events import rederive  # Task 5

        rederive(conn, args.network, distributor=args.distributor)
        return 0

    async with AsyncWebsocketClient(clio) as client:

        async def request_fn(req: dict[str, Any]) -> dict[str, Any]:
            delay = RETRY_BASE_DELAY
            for attempt in range(RETRY_MAX):
                await asyncio.sleep(THROTTLE_SECONDS)
                try:
                    r = await asyncio.wait_for(
                        client.request(Request.from_dict(req)), timeout=REQUEST_TIMEOUT
                    )
                except (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError) as e:
                    # Transient transport trouble gets the same bounded backoff as
                    # slowDown. A torn-down websocket will keep failing and exhaust
                    # the attempts — the run is cursor-resumable, so that is safe.
                    if attempt < RETRY_MAX - 1:
                        logging.warning(f"{req['method']}: {e!r}; backing off {delay:.0f}s")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 120.0)
                        continue
                    raise
                if r.is_successful():
                    return r.result
                error = r.result.get("error") if isinstance(r.result, dict) else None
                if error in RETRYABLE_ERRORS and attempt < RETRY_MAX - 1:
                    logging.warning(f"{req['method']}: {error}; backing off {delay:.0f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120.0)
                    continue
                raise RuntimeError(f"{req['method']} failed: {r.result}")
            raise RuntimeError(f"{req['method']} failed after {RETRY_MAX} attempts")

        if "issuer" in wanted:
            n = await backfill_account_tx(conn, request_fn, issuer, "issuer_tx")
            logging.info(f"issuer_tx: +{n}")
        if "brix" in wanted:
            n = await backfill_account_tx(conn, request_fn, config.SWAP_OFFER_ISSUER, "brix_tx")
            logging.info(f"brix_tx: +{n}")
        if "distributor" in wanted and args.distributor:
            n = await backfill_account_tx(conn, request_fn, args.distributor, "distributor_tx")
            logging.info(f"distributor_tx: +{n}")
        if "nfts" in wanted:
            oconn = nft_index.init_db(nft_index.index_db_path(args.network))
            ids = [r[0] for r in oconn.execute("SELECT nft_id FROM onchain_nfts")]
            total = 0
            for i, nft_id in enumerate(ids, 1):
                total += await backfill_nft_history(conn, request_fn, nft_id)
                if i % 100 == 0:
                    logging.info(f"nft_history: {i}/{len(ids)} tokens, +{total} txs")
            logging.info(f"nft_history: done, +{total}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
