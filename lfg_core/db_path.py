# App DB path resolution, dependency-free on purpose: init_db.py (the
# standalone schema initializer) must be runnable with only DB_PATH /
# XRPL_NETWORK set, without loading lfg_core.config's full runtime settings
# (XUMM/Bunny secrets etc.). Keep this module free of lfg_core imports.
import os

from dotenv import load_dotenv

load_dotenv()


def app_db_path(network: str | None = None) -> str:
    """Per-network app DB file (LFG/Users/burned_nfts); DB_PATH overrides.

    Mainnet keeps the legacy bare filename (the long-lived production data);
    any other network gets its own suffixed file, matching the
    onchain_<net>.db / history_<net>.db convention. A shared file once let
    testnet mints push the mainnet edition counter from 3536 to 3572.
    """
    override = os.getenv("DB_PATH")
    if override:
        return override
    if network is None:
        network = os.getenv("XRPL_NETWORK", "mainnet")
    net = network.strip().lower()
    return "lfg_nfts.db" if net == "mainnet" else f"lfg_nfts_{net}.db"
