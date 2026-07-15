# lfg_core/supply.py
# Collection-size census + headroom for bulk minting (#215). The authoritative
# live-edition count is the on-chain index (onchain_<net>.db, is_burned=0) —
# the same store the economy conservation audit reads.
import sqlite3

from lfg_core import config, nft_index


def current_supply(network: str) -> int:
    """Number of live (un-burned) editions currently on-chain for `network`."""
    path = nft_index.index_db_path(network)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remaining_headroom(network: str) -> int:
    """How many more mints fit under MAX_COLLECTION_SIZE. Never negative."""
    return max(0, config.MAX_COLLECTION_SIZE - current_supply(network))
