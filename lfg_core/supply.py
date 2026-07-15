# lfg_core/supply.py
# Collection-size census + headroom for bulk minting (#215). The authoritative
# live-edition count is the on-chain index (onchain_<net>.db, is_burned=0) —
# the same store the economy conservation audit reads.
import sqlite3

from lfg_core import config, nft_index


def current_supply(network: str) -> int:
    """Number of live (un-burned) editions currently on-chain for `network`.

    NOTE (#215 follow-up): this reads the on-chain index, which is populated by
    the listener process from the tx stream and therefore LAGS real-time mints
    (`record_nft_mint` writes the LFG app table, not `onchain_nfts`). The
    request-time headroom clamp still bounds any single bulk job, but the
    per-unit cap re-check cannot observe another concurrent job's in-flight
    mints — so two jobs near the cap could collectively overshoot
    MAX_COLLECTION_SIZE. Enforcement is advisory until an authoritative,
    synchronous headroom reservation lands (tracked as a follow-up issue)."""
    path = nft_index.index_db_path(network)
    conn = sqlite3.connect(path)
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0").fetchone()
        except sqlite3.OperationalError:
            # Unbuilt index (fresh checkout / pre-backfill deploy): no
            # onchain_nfts table yet. Treat as zero recorded supply -> full
            # headroom; the request-time clamp still bounds any single job.
            return 0
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remaining_headroom(network: str) -> int:
    """How many more mints fit under MAX_COLLECTION_SIZE. Never negative."""
    return max(0, config.MAX_COLLECTION_SIZE - current_supply(network))
