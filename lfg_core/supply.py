# lfg_core/supply.py
# Collection-size census + headroom for bulk minting (#215). The authoritative
# live-edition count is the on-chain index (onchain_<net>.db, is_burned=0) —
# the same store the economy conservation audit reads.
import sqlite3

from lfg_core import config, nft_index


def current_supply(network: str) -> int:
    """Number of live (un-burned) editions currently on-chain for `network`.

    NOTE (#226): this reads the on-chain index, which is populated by the
    listener process from the tx stream and therefore LAGS real-time mints
    (`record_nft_mint` writes the LFG app table, not `onchain_nfts`). The
    index stays the supply AUTHORITY; the lag window is covered by
    `lfg_core/headroom.py` — an atomic, durable reservation overlay every
    non-cap-exempt mint (single and bulk) grants against. Never enforce
    MAX_COLLECTION_SIZE from this count alone; go through headroom.try_reserve."""
    path = nft_index.index_db_path(network)
    conn = sqlite3.connect(path)
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0").fetchone()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                # Unbuilt index (fresh checkout / pre-backfill deploy): no
                # onchain_nfts table yet. Treat as zero recorded supply -> full
                # headroom; the request-time clamp still bounds any single job.
                return 0
            # Any OTHER OperationalError — most importantly "database is
            # locked" (the index DB is written by the separate listener
            # process and by long-running backfill scripts) — must NOT read
            # as supply 0: inside headroom.try_reserve that would inflate
            # availability to the whole collection and over-grant past the
            # cap. Propagate instead; try_reserve's blanket except then
            # grants 0 (fail CLOSED for new headroom, per its contract).
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remaining_headroom(network: str) -> int:
    """Display-only estimate of remaining mints under MAX_COLLECTION_SIZE
    (never negative). NEVER use this for cap enforcement — it is a raw
    ``MAX - lagging indexed supply`` count that cannot see in-flight
    reservations, and it propagates index read errors; enforcement must go
    through ``headroom.try_reserve`` (#226). Kept (no production callers
    today) for a future "X remaining" UI read and the #220 burn-to-mint
    stub."""
    return max(0, config.MAX_COLLECTION_SIZE - current_supply(network))
