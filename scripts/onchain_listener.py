#!/usr/bin/env python3
"""Keep the per-nft_id on-chain NFT index fresh.

  python scripts/onchain_listener.py --network testnet snapshot   # one-time backfill
  python scripts/onchain_listener.py --network testnet listen     # live websocket sync

`snapshot` delegates to the backfill. `listen` subscribes to the clio transaction
stream and applies NFTokenMint / AcceptOffer / Burn / Modify to the index,
resolving post-transfer owners via nft_info (the XLS-46 path). Reconnects with
backoff. Run one `listen` process per network (pm2: lfg-index-testnet / -mainnet).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

import aiohttp
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import StreamParameter, Subscribe

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import backfill_onchain as bf  # noqa: E402

from lfg_core import nft_index, nft_listener, swap_meta, xrpl_ops  # noqa: E402

RECONNECT_BASE = 2
RECONNECT_MAX = 60


def _resolve(args: argparse.Namespace) -> tuple[str, str, int, str]:
    from lfg_core import config

    net = bf.NETWORKS[args.network]
    issuer = args.issuer or net["issuer"] or config.SWAP_ISSUER_ADDRESS
    taxon = args.taxon if args.taxon is not None else net["taxon"]
    clio = args.clio or net["clio"]
    return args.network, issuer, taxon, clio


def _normalize_stream_tx(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a clio `transactions` stream message into a tx dict carrying
    TransactionType, NFTokenID and `meta` (handles both tx_json and the older
    `transaction` envelope)."""
    if msg.get("type") != "transaction":
        return None
    tx = dict(msg.get("tx_json") or msg.get("transaction") or {})
    tx["meta"] = msg.get("meta") or msg.get("metaData") or {}
    return tx


async def _listen(network: str, issuer: str, taxon: int, clio: str) -> None:
    conn = nft_index.init_db(nft_index.index_db_path(network))
    backoff = RECONNECT_BASE
    async with aiohttp.ClientSession() as http:

        async def fetch_meta(uri_hex: str) -> dict[str, Any] | None:
            return await swap_meta.fetch_metadata(uri_hex, http)

        async def fetch_token(nft_id: str) -> dict[str, Any] | None:
            return await xrpl_ops.nft_info(nft_id, clio)

        def is_ours(token: dict[str, Any]) -> bool:
            return token.get("issuer") == issuer and int(token.get("taxon") or -1) == taxon

        while True:
            try:
                async with AsyncWebsocketClient(clio) as client:
                    await client.request(Subscribe(streams=[StreamParameter.TRANSACTIONS]))
                    logging.info(f"[{network}] subscribed to tx stream on {clio}")
                    backoff = RECONNECT_BASE
                    async for msg in client:
                        tx = _normalize_stream_tx(dict(msg))
                        if tx is None:
                            continue
                        # Only collection NFTs matter; cheap filter by issuer when present.
                        if tx.get("Issuer") and tx["Issuer"] != issuer:
                            # AcceptOffer/Burn won't carry Issuer; apply_tx + nft_info
                            # still scope correctness, so only skip clear mismatches.
                            if tx.get("TransactionType") == "NFTokenMint":
                                continue
                        await nft_listener.apply_tx(conn, tx, fetch_token, fetch_meta, is_ours)
            except Exception as e:
                logging.warning(f"[{network}] stream error: {e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from lfg_core import config

    parser = argparse.ArgumentParser(description="On-chain NFT index listener.")
    parser.add_argument("--network", choices=sorted(bf.NETWORKS), default=config.XRPL_NETWORK)
    parser.add_argument("--issuer")
    parser.add_argument("--taxon", type=int)
    parser.add_argument("--clio")
    parser.add_argument("mode", choices=["snapshot", "listen"])
    args = parser.parse_args()

    network, issuer, taxon, clio = _resolve(args)

    if args.mode == "snapshot":
        conn = nft_index.init_db(nft_index.index_db_path(network))

        async def enum() -> list[dict[str, Any]]:
            return await nft_index.enumerate_tokens(clio, issuer, taxon)

        async with aiohttp.ClientSession() as http:

            async def fetch(uri_hex: str) -> dict[str, Any] | None:
                return await swap_meta.fetch_metadata(uri_hex, http)

            counts = await bf.run_backfill(conn, enum, fetch)
        print(f"[{network}] snapshot: {counts}")
        return 0

    await _listen(network, issuer, taxon, clio)
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
