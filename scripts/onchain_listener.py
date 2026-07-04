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
import json as _json
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

import aiohttp
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import StreamParameter, Subscribe

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import backfill_onchain as bf  # noqa: E402

from lfg_core import (  # noqa: E402
    economy_store,
    history_events,
    history_store,
    nft_index,
    nft_listener,
    swap_meta,
    trait_economy,
    xrpl_ops,
)

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
    tx.setdefault("hash", msg.get("hash"))
    tx.setdefault("ledger_index", msg.get("ledger_index"))
    if "close_time_iso" in msg:
        tx.setdefault("close_time_iso", msg["close_time_iso"])
    return tx


def _effective_genesis(conn: Any) -> trait_economy.Genesis:
    """Genesis with the supply_changes ledger folded in — the moving
    conservation target. Read fresh per tx so an edition recorded earlier in the
    stream is recognised, making new-edition growth-logging idempotent."""
    genesis = economy_store.read_genesis(conn)
    return trait_economy.effective_genesis(genesis, economy_store.read_supply_changes(conn))


async def process_stream_tx(
    conn: Any,
    tx: dict[str, Any],
    *,
    fetch_token: nft_listener.FetchTokenFn,
    fetch_meta: nft_listener.FetchMetaFn,
    is_ours: Callable[[dict[str, Any]], bool],
    history_conn: Any = None,
    history_ctx: dict[str, Any] | None = None,
) -> None:
    """Apply one normalized stream tx to BOTH the per-nft_id index and the
    trait-economy tables. The single per-message seam the live loop drives,
    extracted so the listen path is testable without a websocket. Economy apply
    (supply-growth logging + Bucket rebuild) is gated on a frozen genesis — until
    one exists every mint would look like an unknown edition and log spurious
    growth.

    The index and economy applies both resolve the same token/metadata per
    nft_id, so per-tx memo caches feed both helpers from a single clio nft_info
    call and (on mainnet) a single IPFS metadata fetch — the token/meta state is
    fixed for the duration of one tx, so caching is correctness-safe."""
    token_cache: dict[str, dict[str, Any] | None] = {}
    meta_cache: dict[str, dict[str, Any] | None] = {}

    async def cached_token(nft_id: str) -> dict[str, Any] | None:
        if nft_id not in token_cache:
            token_cache[nft_id] = await fetch_token(nft_id)
        return token_cache[nft_id]

    async def cached_meta(uri_hex: str) -> dict[str, Any] | None:
        if uri_hex not in meta_cache:
            meta_cache[uri_hex] = await fetch_meta(uri_hex)
        return meta_cache[uri_hex]

    await nft_listener.apply_tx(conn, tx, cached_token, cached_meta, is_ours)
    # mint/modify/accept/burn reach economy logic; Closet NFTokenAcceptOffer promotes
    # pending_accept → active; TRAIT_TAXON burn deletes the trait_tokens row. Closet/
    # trait mirror maintenance must NOT depend on a frozen genesis (a fresh/reset DB
    # still needs trait mint/accept/burn applied); only the supply-growth path uses
    # genesis, so pass it only when frozen and let apply_economy_tx skip growth when None.
    if nft_listener.classify_tx(tx) in ("mint", "modify", "accept", "burn"):
        genesis = _effective_genesis(conn) if economy_store.genesis_exists(conn) else None
        await nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=cached_token,
            fetch_meta_fn=cached_meta,
            genesis=genesis,
        )
    if history_conn is not None and history_ctx is not None:
        _record_history(history_conn, tx, history_ctx, index_conn=conn)


def _record_history(
    hconn: Any, tx: dict[str, Any], ctx: dict[str, Any], *, index_conn: Any = None
) -> None:
    """Append one stream tx to the history archive iff it produces events.

    The listener subscribes to the WHOLE network tx stream, so derived NFT
    events must be scoped to our collection: every NFTokenID embeds its
    issuer's AccountID, and events whose nft_id embeds a foreign issuer are
    dropped. The raw tx is archived only if any events survive."""
    nft_evs = history_events.derive_nft_events(tx, nft_issuer=ctx["nft_issuer"])
    if nft_evs:
        issuer_hex = ctx.get("issuer_hex")
        if issuer_hex is None:
            issuer_hex = ctx["issuer_hex"] = history_events.issuer_account_hex(ctx["nft_issuer"])
        nft_evs = [
            ev for ev in nft_evs if history_events.nft_id_issuer_matches(ev["nft_id"], issuer_hex)
        ]
    brix_evs = history_events.derive_brix_events(
        tx,
        brix_issuer=ctx["brix_issuer"],
        brix_hex=ctx["brix_hex"],
        distributor=ctx.get("distributor"),
    )
    if not nft_evs and not brix_evs:
        return
    if not tx.get("hash"):
        return
    history_store.insert_tx(
        hconn,
        tx_hash=str(tx["hash"]),
        ledger_index=tx.get("ledger_index"),
        close_time=history_events.tx_unix_time(tx),
        tx_type=str(tx.get("TransactionType", "")),
        account=tx.get("Account"),
        source_tag=tx.get("SourceTag"),
        raw_json=_json.dumps(tx, sort_keys=True),
    )
    for ev in nft_evs:
        nft_id = ev["nft_id"]
        numbers = ctx["numbers"]
        if nft_id not in numbers and index_conn is not None:
            # ctx["numbers"] is a startup snapshot: a token minted while this
            # process is running isn't in it yet. apply_tx (above, same tx)
            # has already upserted the index row before _record_history runs,
            # so a live lookup on index_conn resolves the number instead of
            # leaving it None until the nightly --derive-only rerun.
            row = index_conn.execute(
                "SELECT nft_number FROM onchain_nfts WHERE nft_id=?", (nft_id,)
            ).fetchone()
            if row is not None and row[0] is not None:
                numbers[nft_id] = row[0]
        ev["nft_number"] = numbers.get(nft_id)
        history_store.insert_nft_event(hconn, ev)
    for ev in brix_evs:
        history_store.insert_brix_event(hconn, ev)
    hconn.commit()


async def _listen(network: str, issuer: str, taxon: int, clio: str) -> None:
    from lfg_core import config

    conn = nft_index.init_db(nft_index.index_db_path(network))
    economy_store.init_economy_schema(conn)
    hconn = history_store.init_history_db(history_store.history_db_path(network))
    # Numbers map is read once at startup, not refreshed per-tx: a mint of a
    # brand-new edition within this process's lifetime won't have its number
    # yet, so that nft_event row is stored with nft_number=None. The nightly
    # `--derive-only` rerun (scripts/derive_history_events.py) fills it in
    # from the now-updated index — acceptable staleness, not data loss.
    numbers = dict(conn.execute("SELECT nft_id, nft_number FROM onchain_nfts"))
    history_ctx: dict[str, Any] = {
        "nft_issuer": issuer,
        "issuer_hex": history_events.issuer_account_hex(issuer),
        "brix_issuer": config.SWAP_OFFER_ISSUER,
        "brix_hex": config.SWAP_OFFER_CURRENCY_HEX,
        "distributor": config.BRIX_DISTRIBUTOR_ADDRESS,
        "numbers": numbers,
    }
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
                        await process_stream_tx(
                            conn,
                            tx,
                            fetch_token=fetch_token,
                            fetch_meta=fetch_meta,
                            is_ours=is_ours,
                            history_conn=hconn,
                            history_ctx=history_ctx,
                        )
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
