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
import math
import os
import sys
import time
from collections.abc import Callable
from typing import Any

import aiohttp
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import Request, StreamParameter, Subscribe

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import backfill_onchain as bf  # noqa: E402

from lfg_core import (  # noqa: E402
    db_path,
    economy_store,
    history_events,
    history_store,
    market_store,
    nft_index,
    nft_listener,
    sponsored_mint,
    swap_meta,
    trait_economy,
    xrpl_ops,
)

RECONNECT_BASE = 2
RECONNECT_MAX = 60


# Watchdog (#345): validated ledgers close every ~4s, so even a silent
# collection sees a whole-network stream message far more often than this.
# Silence past the window means the subscription is wedged (e.g. a keepalive
# ping timeout the endpoint never followed with a close frame) — force a
# reconnect instead of sitting online-but-dead for days.
def _read_positive_timeout(name: str, default: str) -> float:
    """Read a timeout env var, rejecting values that would break the watchdog:
    0/negative fire immediately, inf/nan disable it entirely."""
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite value greater than zero, got {value!r}")
    return value


STREAM_IDLE_TIMEOUT = _read_positive_timeout("LISTENER_STREAM_IDLE_TIMEOUT", "300")
# Side-channel awaits (clio nft_info, IPFS/CDN metadata, the identity request)
# run inline in the stream loop; a single hung call used to freeze the whole
# listener without ever tripping the reconnect loop.
SIDE_CALL_TIMEOUT = _read_positive_timeout("LISTENER_SIDE_CALL_TIMEOUT", "30")
# One bounded retry before a timed-out side call degrades to None — a transient
# clio/IPFS stall usually clears immediately, and a retry is far cheaper than
# leaving the tx's index/economy state to the next backfill.
SIDE_CALL_ATTEMPTS = 2


# Batched eligibility-archive commits (#333): the certified-archive path used
# to pay one synchronous fsync per streamed transaction (~2.2ms measured) on
# the listener's serial event loop. Accumulate in memory and flush at most one
# commit per window. The 900s SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS freshness
# gate has three orders of magnitude of headroom over a 1-2s flush window.
ARCHIVE_FLUSH_MAX_TXS = int(os.environ.get("ARCHIVE_FLUSH_MAX_TXS", "200"))
ARCHIVE_FLUSH_MAX_SECONDS = float(os.environ.get("ARCHIVE_FLUSH_MAX_SECONDS", "1.0"))


class ArchiveBatch:
    """In-memory accumulator for the eligibility archive's per-tx writes.

    `add()` buffers tagged-transaction evidence plus the highest
    `(ledger_index, close_time)` cursor seen and the wall-clock time the last
    contributing transaction was OBSERVED. `flush()` writes everything in ONE
    sqlite transaction — evidence rows and the `record_validated_ledger`
    cursor/heartbeat advance commit atomically, so the heartbeat can never
    persist past evidence that failed to. A failed flush rolls back AND
    retains the batch for retry: freshness then decays until the
    sponsored-mint gate closes (fail-closed by construction, closing the old
    per-tx fail-open where dropped evidence never blocked the heartbeat).

    Crash durability is deliberately not attempted here: a listener restart
    already invalidates archive continuity (`_verify_archive_connection`), so
    losing an unflushed batch to a hard kill can never yield a
    usable-but-incomplete archive.

    Uncertified path (`ctx["genesis_hash"]` empty): tagged rows still persist
    on flush; no cursor/heartbeat is ever written — same as before."""

    def __init__(
        self,
        hconn: Any,
        ctx: dict[str, Any],
        *,
        max_txs: int | None = None,
        max_seconds: float | None = None,
    ) -> None:
        self._hconn = hconn
        self._ctx = ctx
        self._max_txs = ARCHIVE_FLUSH_MAX_TXS if max_txs is None else max_txs
        self._max_seconds = ARCHIVE_FLUSH_MAX_SECONDS if max_seconds is None else max_seconds
        self._tagged: list[dict[str, Any]] = []
        self._cursor: tuple[int, int] | None = None  # (ledger_index, close_time)
        self._observed_at: int | None = None
        self._count = 0
        self._first_add_monotonic: float | None = None

    @property
    def pending(self) -> bool:
        return bool(self._tagged) or self._cursor is not None

    def add(self, tx: dict[str, Any]) -> None:
        """Buffer one normalized stream tx (same filters as the old per-tx path)."""
        from lfg_core import config

        if tx.get("validated") is not True or not tx.get("hash"):
            return
        if tx.get("SourceTag") == config.SOURCE_TAG:
            self._tagged.append(dict(tx))
        ledger_index = tx.get("ledger_index")
        close_time = history_events.tx_unix_time(tx)
        if (
            isinstance(ledger_index, int)
            and not isinstance(ledger_index, bool)
            and ledger_index >= 0
            and isinstance(close_time, int)
            and close_time >= 0
        ):
            if self._cursor is None or ledger_index > self._cursor[0]:
                self._cursor = (ledger_index, close_time)
            # Honest heartbeat: stamp with the time this tx was OBSERVED, not
            # a later flush time — the archive never claims currency it lacks.
            self._observed_at = int(time.time())
        self._count += 1
        if self._first_add_monotonic is None:
            self._first_add_monotonic = time.monotonic()

    def due(self) -> bool:
        if not self.pending:
            return False
        if self._count >= self._max_txs:
            return True
        return (
            self._first_add_monotonic is not None
            and time.monotonic() - self._first_add_monotonic >= self._max_seconds
        )

    def flush(self) -> None:
        """Persist the batch in one transaction; retain state on failure."""
        from lfg_core import config

        if not self.pending:
            self._reset()
            return
        network = str(self._ctx.get("network") or "")
        genesis_hash = str(self._ctx.get("genesis_hash") or "").strip()
        inserted: list[dict[str, Any]] = []
        try:
            for tx in self._tagged:
                if history_store.insert_tx(
                    self._hconn,
                    tx_hash=str(tx["hash"]),
                    ledger_index=tx.get("ledger_index"),
                    close_time=history_events.tx_unix_time(tx),
                    tx_type=str(tx.get("TransactionType", "")),
                    account=tx.get("Account"),
                    source_tag=tx.get("SourceTag"),
                    raw_json=_json.dumps(tx, sort_keys=True),
                ):
                    inserted.append(tx)
            if self._cursor is not None and genesis_hash:
                history_store.record_validated_ledger(
                    self._hconn,
                    network=network,
                    genesis_hash=genesis_hash,
                    ledger_index=self._cursor[0],
                    close_time=self._cursor[1],
                    source_tag=int(self._ctx.get("source_tag", config.SOURCE_TAG)),
                    observed_at=self._observed_at,
                    commit=False,
                )
            self._hconn.commit()
        except Exception:
            # Atomic failure: evidence AND heartbeat roll back together, and
            # the batch is kept so the next flush retries it (insert_tx is
            # INSERT OR IGNORE, so retry is idempotent). Until a flush lands,
            # the heartbeat stays frozen and the freshness gate closes.
            self._hconn.rollback()
            raise
        self._reset()
        for tx in inserted:
            try:
                sponsored_mint.observe_sponsored_acceptance(
                    tx, tx.get("meta") or {}, network=network
                )
            except Exception:
                # Raw eligibility evidence is more fundamental than its
                # derived claim state; startup replay repairs a lost race.
                logging.exception("sponsored acceptance observation deferred for %s", tx["hash"])

    def flush_logged(self) -> None:
        """Flush, downgrading failure to a log line (state stays retained)."""
        try:
            self.flush()
        except Exception:
            logging.exception(
                "[%s] eligibility archive flush failed; batch retained for retry",
                self._ctx.get("network"),
            )

    def _reset(self) -> None:
        self._tagged.clear()
        self._cursor = None
        self._observed_at = None
        self._count = 0
        self._first_add_monotonic = None


async def _archive_idle_flush_loop(batch: ArchiveBatch, interval: float | None = None) -> None:
    """Flush a quiet batch so a lull in stream traffic can neither strand a
    partial batch nor let the heartbeat go stale between messages."""
    period = ARCHIVE_FLUSH_MAX_SECONDS if interval is None else interval
    while True:
        await asyncio.sleep(period)
        if batch.due():
            batch.flush_logged()


def _flush_and_mark_disconnected(
    batch: ArchiveBatch | None,
    hconn: Any,
    *,
    network: str,
    at: int | None = None,
) -> None:
    """Disconnect path: land pending evidence FIRST, then invalidate continuity
    — the recorded gap bound then reflects the last archived ledger, and no
    observed transaction is silently dropped on the way out."""
    if batch is not None:
        batch.flush_logged()
    current = history_store.get_archive_state(hconn, network)
    _mark_stream_disconnected(
        hconn,
        network=network,
        after_ledger=current.validated_ledger_index if current else None,
        at=at,
    )


class StreamStalled(Exception):
    """No stream traffic within STREAM_IDLE_TIMEOUT — treat as a disconnect."""


async def _iter_with_watchdog(source: Any, idle_timeout: float | None = None) -> Any:
    """Yield messages from an async iterable, raising StreamStalled if the next
    message doesn't arrive within the idle window. StreamStalled is a plain
    Exception on purpose: it rides the existing reconnect/backoff path."""
    timeout = STREAM_IDLE_TIMEOUT if idle_timeout is None else idle_timeout
    it = source.__aiter__()
    while True:
        try:
            msg = await asyncio.wait_for(it.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise StreamStalled(
                f"watchdog: no stream message for {timeout:.0f}s "
                "(ledgers close every ~4s; subscription presumed wedged)"
            ) from None
        yield msg


def _bounded(
    fn: Callable[[str], Any], *, label: str, network: str, timeout: float | None = None
) -> Callable[[str], Any]:
    """Wrap a single-argument async fetch with a hard per-attempt timeout and
    SIDE_CALL_ATTEMPTS bounded tries. Both a wait_for timeout AND a None result
    are retryable: nft_info and fetch_metadata swallow their own internal
    timeouts/errors and return None (fetch_metadata's ~20s aiohttp timeout is
    shorter than the default 30s side-call bound, so it surfaces here as a
    "successful" None, never as asyncio.TimeoutError). After the final attempt
    it degrades to None — the pre-existing 'unavailable' shape apply_tx handles
    ("could not resolve token", skip) — instead of hanging the stream loop
    forever; non-timeout exceptions propagate unchanged."""

    async def wrapped(arg: str) -> Any:
        t = SIDE_CALL_TIMEOUT if timeout is None else timeout
        for attempt in range(1, SIDE_CALL_ATTEMPTS + 1):
            try:
                result = await asyncio.wait_for(fn(arg), timeout=t)
            except asyncio.TimeoutError:
                logging.warning(
                    f"[{network}] {label}({arg}) timed out after {t:.0f}s "
                    f"(attempt {attempt}/{SIDE_CALL_ATTEMPTS})"
                )
                continue
            if result is not None:
                return result
            logging.warning(
                f"[{network}] {label}({arg}) unresolved (attempt {attempt}/{SIDE_CALL_ATTEMPTS})"
            )
        logging.warning(f"[{network}] {label}({arg}) exhausted retries; treating as unavailable")
        return None

    return wrapped


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
    tx["validated"] = msg.get("validated") is True
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


def _archive_eligibility_tx(hconn: Any, tx: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """Durably archive validated SourceTag evidence before business processing."""

    from lfg_core import config

    if tx.get("validated") is not True or not tx.get("hash"):
        return False
    inserted = False
    if tx.get("SourceTag") == config.SOURCE_TAG:
        inserted = history_store.insert_tx(
            hconn,
            tx_hash=str(tx["hash"]),
            ledger_index=tx.get("ledger_index"),
            close_time=history_events.tx_unix_time(tx),
            tx_type=str(tx.get("TransactionType", "")),
            account=tx.get("Account"),
            source_tag=tx.get("SourceTag"),
            raw_json=_json.dumps(tx, sort_keys=True),
        )

    ledger_index = tx.get("ledger_index")
    close_time = history_events.tx_unix_time(tx)
    genesis_hash = str(ctx.get("genesis_hash") or "").strip()
    if (
        isinstance(ledger_index, int)
        and not isinstance(ledger_index, bool)
        and ledger_index >= 0
        and isinstance(close_time, int)
        and close_time >= 0
        and genesis_hash
    ):
        history_store.record_validated_ledger(
            hconn,
            network=str(ctx.get("network") or ""),
            genesis_hash=genesis_hash,
            ledger_index=ledger_index,
            close_time=close_time,
            source_tag=int(ctx.get("source_tag", config.SOURCE_TAG)),
        )
    else:
        hconn.commit()

    if inserted:
        try:
            sponsored_mint.observe_sponsored_acceptance(
                tx, tx.get("meta") or {}, network=str(ctx.get("network") or "")
            )
        except Exception:
            # Raw eligibility evidence is more fundamental than its derived
            # claim state. Startup replay repairs an observation that loses a
            # race or temporarily cannot acquire the application database.
            logging.exception("sponsored acceptance observation deferred for %s", tx["hash"])
    return inserted


def _mark_stream_disconnected(
    hconn: Any,
    *,
    network: str,
    after_ledger: int | None,
    at: int | None = None,
) -> None:
    history_store.invalidate_archive_continuity(
        hconn,
        network=network,
        reason="transaction stream disconnected",
        gap_after=after_ledger,
        invalidated_at=at,
    )


def _verify_archive_connection(
    hconn: Any,
    ctx: dict[str, Any],
    snapshot: history_store.EndpointSnapshot,
) -> None:
    """Bind one subscribed stream to its real chain before accepting evidence."""

    network = str(ctx.get("network") or "")
    expected_genesis = str(ctx.get("genesis_hash") or "").strip()
    source_tag = ctx.get("source_tag")
    if not expected_genesis:
        # No certified archive identity (see _listen): there is nothing to bind
        # this stream to and nothing to invalidate. Returning here keeps the
        # listener's index/market/history duties running exactly as they did
        # before the sponsored-mint archive existed.
        return
    state = history_store.get_archive_state(hconn, network)
    if snapshot.genesis_hash != expected_genesis or (
        state is not None and state.genesis_hash != snapshot.genesis_hash
    ):
        history_store.invalidate_archive_continuity(
            hconn,
            network=network,
            reason="endpoint ledger-1 identity mismatch",
            gap_after=state.validated_ledger_index if state is not None else None,
            gap_before=snapshot.validated_ledger_index,
        )
        raise RuntimeError(f"[{network}] endpoint ledger-1 identity mismatch")
    if state is not None and state.source_tag not in {None, source_tag}:
        history_store.invalidate_archive_continuity(
            hconn,
            network=network,
            reason="configured SourceTag differs from certified archive snapshot",
            gap_after=state.validated_ledger_index,
            gap_before=snapshot.validated_ledger_index,
        )
        raise RuntimeError(f"[{network}] configured SourceTag conflicts with archive provenance")
    if state is None or not state.baseline_complete:
        return
    if state.validated_ledger_index is not None:
        # A persisted live cursor proves a previous listener session existed,
        # but transaction streams have no replay token. A process restart can
        # miss another tx in the same ledger, so equality is not sufficient.
        history_store.invalidate_archive_continuity(
            hconn,
            network=network,
            reason="listener process restart lacks exact stream catch-up",
            gap_after=state.validated_ledger_index,
            gap_before=snapshot.validated_ledger_index,
        )
        return
    if state.baseline_ledger_max != snapshot.validated_ledger_index:
        history_store.invalidate_archive_continuity(
            hconn,
            network=network,
            reason="listener start is ahead of certified baseline",
            gap_after=state.baseline_ledger_max,
            gap_before=snapshot.validated_ledger_index,
        )


async def _dispatch_stream_tx(
    conn: Any,
    tx: dict[str, Any],
    *,
    collection_issuer: str,
    fetch_token: nft_listener.FetchTokenFn,
    fetch_meta: nft_listener.FetchMetaFn,
    is_ours: Callable[[dict[str, Any]], bool],
    history_conn: Any,
    history_ctx: dict[str, Any],
    archive_batch: ArchiveBatch | None = None,
) -> None:
    """Archive eligibility first, then apply collection-only optimizations."""

    if (
        tx.get("TransactionType") == "NFTokenMint"
        and tx.get("Issuer")
        and tx["Issuer"] != collection_issuer
    ):
        if archive_batch is not None:
            archive_batch.add(tx)
        else:
            _archive_eligibility_tx(history_conn, tx, history_ctx)
        return
    await process_stream_tx(
        conn,
        tx,
        fetch_token=fetch_token,
        fetch_meta=fetch_meta,
        is_ours=is_ours,
        history_conn=history_conn,
        history_ctx=history_ctx,
        archive_batch=archive_batch,
    )


async def process_stream_tx(
    conn: Any,
    tx: dict[str, Any],
    *,
    fetch_token: nft_listener.FetchTokenFn,
    fetch_meta: nft_listener.FetchMetaFn,
    is_ours: Callable[[dict[str, Any]], bool],
    history_conn: Any = None,
    history_ctx: dict[str, Any] | None = None,
    archive_batch: ArchiveBatch | None = None,
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
    if history_conn is not None and history_ctx is not None:
        # Archiving runs FIRST so a raising apply_tx cannot lose the eligibility
        # evidence — but it must not be able to take the reverse hostage. This
        # listener's primary duties (the per-nft_id index, market listings,
        # derived history) predate the sponsored-mint archive and are what the
        # marketplace and Activity read; a sqlite hiccup here used to propagate
        # and skip all of them for that transaction. Sponsored eligibility
        # degrades safely on a miss (the wallet looks untagged only if the row
        # was never archived, and archive_is_usable independently gates on
        # freshness), so log and carry on rather than dropping index work.
        try:
            if archive_batch is not None:
                # Batched path (#333): buffer only — no per-tx sqlite work.
                archive_batch.add(tx)
            else:
                _archive_eligibility_tx(history_conn, tx, history_ctx)
        except Exception:
            logging.exception(
                "[%s] eligibility archiving failed for %s; continuing with index/market apply",
                history_ctx.get("network"),
                tx.get("hash"),
            )

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
    # offer_create/offer_cancel/accept maintain market_listings; runs after apply_tx
    # so an accept's post-transfer owner (used to delist other stale rows for the
    # same nft_id) reads the just-updated onchain_nfts/trait_tokens row, not stale data.
    await nft_listener.apply_market_tx(conn, tx)
    if history_conn is not None and history_ctx is not None:
        # already_archived: the eligibility write happened above, BEFORE the
        # business applies, so a raising apply_tx cannot lose the evidence.
        # Without this flag every transaction on the whole-network stream paid
        # for the archive twice — two record_validated_ledger round-trips and
        # two synchronous commits per tx, on the listener's serial event loop.
        _record_history(history_conn, tx, history_ctx, index_conn=conn, already_archived=True)


def _record_history(
    hconn: Any,
    tx: dict[str, Any],
    ctx: dict[str, Any],
    *,
    index_conn: Any = None,
    already_archived: bool = False,
) -> None:
    """Append a stream transaction and any derived business events.

    Validated SourceTag rows are archived before these event filters by
    `_archive_eligibility_tx`; calling this helper directly preserves that rule.
    The listener subscribes to the WHOLE network tx stream, so derived NFT
    events must be scoped to our collection: every NFTokenID embeds its
    issuer's AccountID, and events whose nft_id embeds a foreign issuer are
    dropped. The raw tx is archived only if any events survive."""
    if not already_archived:
        _archive_eligibility_tx(hconn, tx, ctx)

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
    market_store.init_db(conn)
    # Self-heal editions the chain metadata couldn't describe (NULL nft_number)
    # from the authoritative app LFG table on every restart — the deployer
    # restarts this listener on each deploy, so a fresh gap never lingers past
    # one release. Runs before the numbers snapshot below so it picks up the
    # healed values too.
    healed = nft_index.reconcile_numbers_from_app_db(conn, db_path.app_db_path(network))
    if healed:
        logging.info(f"[{network}] reconciled {healed} nft_number(s) from app DB")
    hconn = history_store.init_history_db(history_store.history_db_path(network))
    # Numbers map is read once at startup, not refreshed per-tx: a mint of a
    state = history_store.get_archive_state(hconn, network)
    configured_genesis = config.SPONSORED_MINT_ARCHIVE_GENESIS_HASHES.get(network, "")
    if state is not None and configured_genesis and state.genesis_hash != configured_genesis:
        raise RuntimeError(
            f"[{network}] configured genesis hash conflicts with history archive provenance"
        )
    genesis_hash = configured_genesis or (state.genesis_hash if state is not None else "")
    if not genesis_hash:
        # No certified archive identity yet — the normal state on every stack
        # until an operator runs the audited baseline (see
        # docs/ops/sponsored-free-mint.md). This listener's PRIMARY jobs (the
        # per-nft_id index, market listings, and derived history) predate the
        # sponsored-mint archive and must keep running, so degrade instead of
        # raising: raising here killed the process before it ever subscribed,
        # which pm2 turns into a crash loop that takes the NFT index and
        # marketplace down on any stack whose history DB has no archive_state
        # row. Sponsored eligibility stays fail-closed regardless —
        # sponsored_mint.archive_is_usable rejects an archive with no certified
        # baseline, so no wallet can be admitted off an unproven archive.
        logging.warning(
            "[%s] no certified archive genesis identity; SourceTag eligibility archiving is "
            "DISABLED (sponsored mint cannot admit). Index, market and history sync continue. "
            "Run scripts/backfill_history.py --complete-audited-baseline to enable it.",
            network,
        )

    # brand-new edition within this process's lifetime won't have its number
    # yet, so that nft_event row is stored with nft_number=None. The nightly
    # `--derive-only` rerun (scripts/derive_history_events.py) fills it in
    # from the now-updated index — acceptable staleness, not data loss.
    numbers = dict(conn.execute("SELECT nft_id, nft_number FROM onchain_nfts"))
    history_ctx: dict[str, Any] = {
        "network": network,
        "nft_issuer": issuer,
        "issuer_hex": history_events.issuer_account_hex(issuer),
        "genesis_hash": genesis_hash,
        "source_tag": config.SOURCE_TAG,
        "brix_issuer": config.SWAP_OFFER_ISSUER,
        "brix_hex": config.SWAP_OFFER_CURRENCY_HEX,
        "distributor": config.BRIX_DISTRIBUTOR_ADDRESS,
        "numbers": numbers,
    }
    archive_batch = ArchiveBatch(hconn, history_ctx)
    backoff = RECONNECT_BASE
    async with aiohttp.ClientSession() as http:

        async def _fetch_meta(uri_hex: str) -> dict[str, Any] | None:
            return await swap_meta.fetch_metadata(uri_hex, http)

        async def _fetch_token(nft_id: str) -> dict[str, Any] | None:
            return await xrpl_ops.nft_info(nft_id, clio)

        # Bounded (#345): these are awaited inline in the stream loop; a hung
        # side call must not freeze the subscription without tripping reconnect.
        fetch_meta = _bounded(_fetch_meta, label="fetch_meta", network=network)
        fetch_token = _bounded(_fetch_token, label="nft_info", network=network)

        def is_ours(token: dict[str, Any]) -> bool:
            return token.get("issuer") == issuer and int(token.get("taxon") or -1) == taxon

        while True:
            stream_open = False
            idle_flusher: asyncio.Task[None] | None = None
            try:
                async with AsyncWebsocketClient(clio) as client:
                    await asyncio.wait_for(
                        client.request(Subscribe(streams=[StreamParameter.TRANSACTIONS])),
                        timeout=SIDE_CALL_TIMEOUT,
                    )
                    stream_open = True

                    async def endpoint_request(req: dict[str, Any]) -> dict[str, Any]:
                        response = await asyncio.wait_for(
                            client.request(Request.from_dict(req)), timeout=SIDE_CALL_TIMEOUT
                        )
                        if not response.is_successful() or not isinstance(response.result, dict):
                            raise RuntimeError(
                                f"[{network}] {req['method']} identity request failed: "
                                f"{response.result}"
                            )
                        return response.result

                    snapshot = await history_store.fetch_endpoint_snapshot(endpoint_request)
                    _verify_archive_connection(hconn, history_ctx, snapshot)
                    logging.info(f"[{network}] subscribed to verified tx stream on {clio}")
                    backoff = RECONNECT_BASE
                    # Idle backstop (#333): a quiet stream must not strand a
                    # partial batch and let the heartbeat go stale.
                    idle_flusher = asyncio.create_task(_archive_idle_flush_loop(archive_batch))
                    async for msg in _iter_with_watchdog(client):
                        tx = _normalize_stream_tx(dict(msg))
                        if tx is None:
                            continue
                        await _dispatch_stream_tx(
                            conn,
                            tx,
                            collection_issuer=issuer,
                            fetch_token=fetch_token,
                            fetch_meta=fetch_meta,
                            is_ours=is_ours,
                            history_conn=hconn,
                            history_ctx=history_ctx,
                            archive_batch=archive_batch,
                        )
                        if archive_batch.due():
                            # A failed flush is retained and retried; it must
                            # not tear down the stream (that would force a
                            # full re-certification for a transient sqlite
                            # error) — the frozen heartbeat already fails the
                            # sponsored-mint gate closed.
                            archive_batch.flush_logged()
            except Exception as e:
                if stream_open:
                    _flush_and_mark_disconnected(archive_batch, hconn, network=network)
                    stream_open = False
                logging.warning(f"[{network}] stream error: {e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
            else:
                # The `async for` ended without raising — the endpoint closed
                # the subscription cleanly. That is still a disconnect, and it
                # took the SAME backoff path as an error until now: the sleep
                # lived only in `except`, so a server that closes immediately
                # (maintenance, connection cap) spun this loop reconnecting as
                # fast as clio would accept, with no ceiling. Mark the gap and
                # back off exactly like the error path.
                if stream_open:
                    _flush_and_mark_disconnected(archive_batch, hconn, network=network)
                    stream_open = False
                logging.warning(f"[{network}] stream closed cleanly; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
            finally:
                if idle_flusher is not None:
                    idle_flusher.cancel()
                if stream_open:
                    _flush_and_mark_disconnected(archive_batch, hconn, network=network)


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
