# lfg_core/nft_index.py
# Per-nft_id on-chain NFT index shared by the backfill, the layer-coverage
# auditor, and the live listener. The chain holds multiple NFTokens per edition
# number (duplicates from trait-swaps / reminting); this index keeps EVERY token
# (keyed by nft_id), unlike the edition-keyed app DB. Per-network SQLite files.

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

import aiohttp
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import Request

from lfg_core import swap_meta

# lsfMutable bit on the on-ledger NFToken (Dynamic NFTs amendment).
NFT_FLAG_MUTABLE = 0x0010

FETCH_ATTEMPTS = 3
FETCH_TIMEOUT_SECONDS = 15


def _metadata_urls(uri_hex: str) -> list[str]:
    """Candidate URLs for a token's metadata. ipfs:// URIs yield NOTHING:
    gateway flakiness at collection scale fed the []-clobber cycle that
    eroded mainnet coverage (unreadable-live 1 -> 483), so IPFS is never
    fetched — the Bithomp CSV import is the metadata source for those
    tokens. Only http(s) URIs (BunnyCDN) are fetchable."""
    try:
        uri = bytes.fromhex(uri_hex).decode("ascii")
    except ValueError:
        return []
    if uri.startswith("ipfs://"):
        return []
    return [uri]


async def fetch_metadata_multi(http: aiohttp.ClientSession, uri_hex: str) -> dict[str, Any] | None:
    """Fetch metadata JSON from the token's http(s) URI, retrying over a few
    passes. ipfs:// URIs yield no candidate URLs (see _metadata_urls) and
    return None immediately. Returns the parsed dict or None."""
    urls = _metadata_urls(uri_hex)
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    for _ in range(FETCH_ATTEMPTS):
        for url in urls:
            try:
                async with http.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        return json.loads(await resp.text())  # type: ignore[no-any-return]
            except Exception:
                continue
    return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS onchain_nfts (
    nft_id          TEXT PRIMARY KEY,
    nft_number      INTEGER,
    owner           TEXT,
    is_burned       INTEGER DEFAULT 0,
    mutable         INTEGER,
    uri_hex         TEXT,
    body            TEXT,
    attributes_json TEXT,
    image           TEXT,
    ledger_index    INTEGER,
    last_synced_at  TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_onchain_number ON onchain_nfts(nft_number);
CREATE INDEX IF NOT EXISTS idx_onchain_live   ON onchain_nfts(is_burned);
CREATE TABLE IF NOT EXISTS uri_metadata_cache (
    uri_hex       TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL,
    cached_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass
class OnchainNft:
    nft_id: str
    nft_number: int | None
    owner: str | None
    is_burned: bool
    mutable: bool | None
    uri_hex: str
    body: str
    attributes: list[dict[str, Any]]
    image: str
    ledger_index: int | None


def index_db_path(network: str) -> str:
    """Per-network index DB file; ONCHAIN_DB_PATH overrides."""
    override = os.getenv("ONCHAIN_DB_PATH")
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, f"onchain_{network}.db")


def init_db(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the index DB and ensure the schema exists."""
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# Stay under SQLite's per-statement parameter limit (999 on builds older than
# 3.32) — a whale wallet can hold more distinct URIs than that in one lookup.
_META_CACHE_QUERY_CHUNK = 500


def meta_cache_get_many(
    conn: sqlite3.Connection, uri_hexes: list[str]
) -> dict[str, dict[str, Any]]:
    """Cached raw metadata JSON for the given on-chain URIs (misses omitted),
    keyed by the caller's own strings. The key is the URI itself, which is
    content-addressed for our tokens (IPFS CIDs for legacy mints, unique CDN
    basenames for swap outputs), so entries never go stale — a modify changes
    the URI, not the content.

    uri_hex is case-insensitive: the ledger (account_nfts) reports hex URIs
    UPPERCASE while onchain_nfts rows store lowercase, and the deployed
    mainnet cache joined 0/3535 live tokens on exactly that mismatch.
    Lowercase is canonical in storage (matching the index); lookups fold both
    sides so either caller wins. LOWER() defeats the PK index, but the table
    is one row per distinct URI (≤ collection size) — a scan is negligible."""
    by_lower: dict[str, list[str]] = {}
    for u in uri_hexes:
        by_lower.setdefault(u.lower(), []).append(u)
    lowered = list(by_lower)
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(lowered), _META_CACHE_QUERY_CHUNK):
        chunk = lowered[i : i + _META_CACHE_QUERY_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            "SELECT uri_hex, metadata_json FROM uri_metadata_cache "
            f"WHERE LOWER(uri_hex) IN ({placeholders})",
            chunk,
        )
        for row in cur.fetchall():
            meta = json.loads(row[1])
            for original in by_lower.get(row[0].lower(), []):
                out[original] = meta
    return out


def meta_cache_put_many(conn: sqlite3.Connection, metas: dict[str, dict[str, Any]]) -> None:
    if not metas:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO uri_metadata_cache (uri_hex, metadata_json) VALUES (?, ?)",
        [(uri_hex.lower(), json.dumps(meta)) for uri_hex, meta in metas.items()],
    )
    conn.commit()


def migrate_meta_cache_case(conn: sqlite3.Connection) -> int:
    """Fold pre-normalization cache rows (uppercase uri_hex, written from
    ledger-reported URIs) into the canonical lowercase form. Returns the
    number of rows migrated. Idempotent; safe to run any time."""
    cur = conn.execute(
        "SELECT uri_hex, metadata_json FROM uri_metadata_cache WHERE uri_hex != LOWER(uri_hex)"
    )
    stale = cur.fetchall()
    for uri_hex, metadata_json in stale:
        conn.execute(
            "INSERT OR IGNORE INTO uri_metadata_cache (uri_hex, metadata_json) VALUES (?, ?)",
            (uri_hex.lower(), metadata_json),
        )
        conn.execute("DELETE FROM uri_metadata_cache WHERE uri_hex = ?", (uri_hex,))
    conn.commit()
    return len(stale)


class UriMetadataCache:
    """The meta_cache duck type swap_meta.load_wallet_nfts takes, bound to an
    open index-DB connection."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_many(self, uri_hexes: list[str]) -> dict[str, dict[str, Any]]:
        return meta_cache_get_many(self._conn, uri_hexes)

    def put_many(self, metas: dict[str, dict[str, Any]]) -> None:
        meta_cache_put_many(self._conn, metas)


def _nft_to_row(rec: OnchainNft) -> tuple[Any, ...]:
    return (
        rec.nft_id,
        rec.nft_number,
        rec.owner,
        1 if rec.is_burned else 0,
        None if rec.mutable is None else (1 if rec.mutable else 0),
        rec.uri_hex,
        rec.body,
        json.dumps(rec.attributes),
        rec.image,
        rec.ledger_index,
    )


def _row_to_nft(row: sqlite3.Row) -> OnchainNft:
    return OnchainNft(
        nft_id=row["nft_id"],
        nft_number=row["nft_number"],
        owner=row["owner"],
        is_burned=bool(row["is_burned"]),
        mutable=None if row["mutable"] is None else bool(row["mutable"]),
        uri_hex=row["uri_hex"] or "",
        body=row["body"] or "",
        attributes=json.loads(row["attributes_json"]) if row["attributes_json"] else [],
        image=row["image"] or "",
        ledger_index=row["ledger_index"],
    )


def upsert(conn: sqlite3.Connection, rec: OnchainNft) -> None:
    """Insert a token or update it in place (keyed on nft_id)."""
    conn.execute(
        """
        INSERT INTO onchain_nfts
            (nft_id, nft_number, owner, is_burned, mutable, uri_hex, body,
             attributes_json, image, ledger_index, last_synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(nft_id) DO UPDATE SET
            nft_number=excluded.nft_number,
            owner=excluded.owner,
            -- Burns are irreversible on XRPL: a stale source (e.g. a Bithomp
            -- CSV exported before the burn) must never resurrect a burned
            -- token to live.
            is_burned=MAX(is_burned, excluded.is_burned),
            mutable=excluded.mutable,
            uri_hex=excluded.uri_hex,
            -- Empty attributes mean "metadata fetch failed", never "token has
            -- no traits" — a re-scan must not clobber previously-good metadata
            -- (CSV-imported or fetched) with []. body/image ride along since
            -- they are derived from the same fetch.
            body=CASE WHEN excluded.attributes_json='[]'
                 THEN body ELSE excluded.body END,
            attributes_json=CASE WHEN excluded.attributes_json='[]'
                 THEN attributes_json ELSE excluded.attributes_json END,
            image=CASE WHEN excluded.attributes_json='[]'
                 THEN image ELSE excluded.image END,
            ledger_index=excluded.ledger_index,
            last_synced_at=CURRENT_TIMESTAMP
        """,
        _nft_to_row(rec),
    )
    conn.commit()


def mark_burned(conn: sqlite3.Connection, nft_id: str) -> None:
    """Flip is_burned on a known token. Unknown tokens are ignored — a burn of
    an NFT outside our collection must not add a stub row to the index. The
    single implementation shared by the listener (burn txs) and swap_flow's
    #211 post-burn persist / stale-pointer heal, so a change to burn-flip
    semantics can never miss a writer."""
    conn.execute("UPDATE onchain_nfts SET is_burned=1 WHERE nft_id=?", (nft_id,))
    conn.commit()


def live_nfts(conn: sqlite3.Connection) -> list[OnchainNft]:
    """Every non-burned token in the index."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM onchain_nfts WHERE is_burned=0 ORDER BY nft_number, nft_id")
    return [_row_to_nft(r) for r in cur.fetchall()]


def owner_live_nfts(conn: sqlite3.Connection, owner: str) -> list[OnchainNft]:
    """Non-burned tokens currently owned by `owner`, in edition order."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM onchain_nfts WHERE is_burned=0 AND owner=? ORDER BY nft_number, nft_id",
        (owner,),
    )
    return [_row_to_nft(row) for row in cur.fetchall()]


def nft_by_number(conn: sqlite3.Connection, nft_number: int) -> OnchainNft | None:
    """The single LIVE token at this edition number, or None if none is live
    (unknown edition, or every token at this number is burned — including a
    dress-up Harvest burn, which never touches the LFG app table, so this is
    the only reliable liveness check for a given nft_number).

    Multiple NFTokens can share an edition number (trait-swap/reminting
    duplicates); when more than one is live at once (a data anomaly, see
    collection_anomalies()'s multi_live), the highest ledger_index (the most
    recently synced) wins.

    Side effect: sets `conn.row_factory = sqlite3.Row` on the caller's
    connection (module-wide convention here, not a bug) — don't pass a
    shared/reused connection whose row_factory matters after this call
    returns."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM onchain_nfts WHERE nft_number=? AND is_burned=0 "
        "ORDER BY ledger_index DESC LIMIT 1",
        (nft_number,),
    )
    row = cur.fetchone()
    return _row_to_nft(row) if row else None


def retryable_unreadable(conn: sqlite3.Connection) -> list[OnchainNft]:
    """Non-burned tokens whose metadata never resolved (empty attributes) but
    that still carry a URI — candidates for a re-fetch pass."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM onchain_nfts "
        "WHERE is_burned=0 AND attributes_json='[]' "
        "AND uri_hex IS NOT NULL AND uri_hex!='' ORDER BY nft_id"
    )
    return [_row_to_nft(r) for r in cur.fetchall()]


def collection_anomalies(records: list[OnchainNft], max_edition: int) -> dict[str, Any]:
    """Collection-integrity report over LIVE token records (pure, no I/O):
    - missing: editions in 1..max_edition with no live token (burned, never re-minted)
    - multi_live: {edition: count} for editions with >1 live token (failed-burn dupes)
    - out_of_range: nft_ids of live tokens whose edition is outside 1..max_edition
    - unparsed: nft_ids of live tokens whose name yielded no edition number"""
    from collections import Counter

    present: Counter[int] = Counter()
    out_of_range: list[str] = []
    unparsed: list[str] = []
    for r in records:
        if r.nft_number is None:
            unparsed.append(r.nft_id)
        elif 1 <= r.nft_number <= max_edition:
            present[r.nft_number] += 1
        else:
            out_of_range.append(r.nft_id)
    missing = [n for n in range(1, max_edition + 1) if n not in present]
    multi_live = {n: c for n, c in sorted(present.items()) if c > 1}
    return {
        "missing": missing,
        "multi_live": multi_live,
        "out_of_range": out_of_range,
        "unparsed": unparsed,
    }


def to_token(rec: OnchainNft) -> dict[str, Any]:
    """Reconstruct the enumerated-token shape from an index row (for re-fetch
    and for the local-first roster).

    Flags come from the NFTokenID itself: its first two bytes ARE the
    on-ledger flags (NFTokenID layout: Flags(2) | TransferFee(2) |
    Issuer(20) | Taxon(4) | Sequence(4)), which matters because most
    Bithomp-imported rows carry mutable=NULL — guessing 0 for those would
    route a genuinely mutable NFT down the swap burn-remint path instead of
    NFTokenModify. The mutable column is only the fallback for a malformed
    ID."""
    try:
        flags = int(rec.nft_id[:4], 16)
    except ValueError:
        flags = NFT_FLAG_MUTABLE if rec.mutable else 0
    return {
        "nft_id": rec.nft_id,
        "owner": rec.owner,
        "is_burned": rec.is_burned,
        "flags": flags,
        "uri_hex": rec.uri_hex,
        "ledger_index": rec.ledger_index,
    }


async def enumerate_tokens(
    clio: str, issuer: str, taxon: int, limit: int = 400
) -> list[dict[str, Any]]:
    """Page nfts_by_issuer and return EVERY token (live + burned) as
    {nft_id, owner, is_burned, flags, uri_hex, ledger_index}. The edition number
    is derived later from metadata, not available here."""
    out: list[dict[str, Any]] = []
    marker: Any = None
    async with AsyncWebsocketClient(clio) as client:
        while True:
            req: dict[str, Any] = {
                "method": "nfts_by_issuer",
                "issuer": issuer,
                "nft_taxon": taxon,
                "limit": limit,
            }
            if marker:
                req["marker"] = marker
            r = await client.request(Request.from_dict(req))
            # Fail closed on an error response: a mid-pagination failure must not
            # be read as end-of-list (empty nfts + no marker), which would return
            # a silently truncated set and let a consumer's stale-delete drop live
            # rows (#190). Abort the whole enumeration instead.
            if not r.is_successful():
                raise RuntimeError(f"nfts_by_issuer failed (taxon {taxon}): {r.result}")
            for x in r.result.get("nfts", []):
                out.append(
                    {
                        "nft_id": x["nft_id"],
                        "owner": x.get("owner"),
                        "is_burned": bool(x.get("is_burned")),
                        "flags": int(x.get("flags") or 0),
                        "uri_hex": x.get("uri", ""),
                        "ledger_index": x.get("ledger_index"),
                    }
                )
            marker = r.result.get("marker")
            if not marker:
                break
    return out


def token_record(token: dict[str, Any], metadata: dict[str, Any] | None) -> OnchainNft:
    """Build an OnchainNft from an enumerated token + its metadata (or None).
    Normalizes attributes the same way the swap path does."""
    flags = int(token.get("flags") or 0)
    if isinstance(metadata, dict):
        raw = metadata.get("attributes")
        attributes = swap_meta.normalize_attributes(raw if isinstance(raw, list) else [])
        body = swap_meta.detect_body(attributes)
        number = swap_meta.extract_nft_number(str(metadata.get("name", "")))
        image = swap_meta.resolve_ipfs(str(metadata.get("image", "")))
    else:
        attributes = []
        body = ""
        number = None
        image = ""
    return OnchainNft(
        nft_id=token["nft_id"],
        nft_number=number,
        owner=token.get("owner"),
        is_burned=bool(token.get("is_burned")),
        mutable=bool(flags & NFT_FLAG_MUTABLE),
        uri_hex=token.get("uri_hex", "") or "",
        body=body,
        attributes=attributes,
        image=image,
        ledger_index=token.get("ledger_index"),
    )
