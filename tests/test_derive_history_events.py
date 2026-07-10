# tests/test_derive_history_events.py
# Cross-network issuer resolution for the rederive pass. Bit us in prod ops
# (2026-07-10): `--network testnet` under the post-cutover mainnet env had no
# static NETWORK_ISSUERS entry, fell back to config's MAINNET issuer, and
# silently cleared + zero-derived the testnet events. The issuer must come
# from the network's own index (every NFTokenID embeds it) — and an
# unresolvable issuer must abort loudly BEFORE the destructive clear.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them.
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import sqlite3  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import derive_history_events as d  # noqa: E402

from lfg_core import config, history_events  # noqa: E402

# A real-shaped NFTokenID whose issuer field (hex chars 8..48) encodes ISSUER.
ISSUER = "rHb8SdDPAre5jmEQASWtZZt6PnBPtUpgTh"
_NFT_ID = "00090000" + history_events.issuer_account_hex(ISSUER) + "0000000000000001"


def _index_conn(with_row: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INT)")
    if with_row:
        conn.execute("INSERT INTO onchain_nfts VALUES (?, 1)", (_NFT_ID,))
    return conn


def test_issuer_from_index_decodes_embedded_account():
    assert d.issuer_from_index(_index_conn()) == ISSUER


def test_issuer_from_index_none_on_empty_index():
    assert d.issuer_from_index(_index_conn(with_row=False)) is None


def test_issuers_for_network_derives_from_index_cross_network(monkeypatch):
    """A network with no static entry that isn't the env-native network must
    resolve its issuer from its own index, never config's (wrong-network)
    accounts."""
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    nft, brix = d.issuers_for_network("testnet", oconn=_index_conn())
    assert nft == ISSUER and brix == ISSUER
    assert nft != config.SWAP_ISSUER_ADDRESS


def test_issuers_for_network_aborts_when_unresolvable(monkeypatch):
    """Better to abort than clear the derived tables and rebuild zero events."""
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    with pytest.raises(SystemExit):
        d.issuers_for_network("testnet", oconn=_index_conn(with_row=False))


def test_issuers_for_network_env_native_still_uses_config(monkeypatch):
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    nft, brix = d.issuers_for_network("testnet", oconn=_index_conn())
    assert (nft, brix) == (config.SWAP_ISSUER_ADDRESS, config.SWAP_OFFER_ISSUER)
