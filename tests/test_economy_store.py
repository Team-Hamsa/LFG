# Tests for lfg_core/economy_store.py (genesis + live-state persistence).
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import economy_store, trait_economy  # noqa: E402


def _conn():
    conn = sqlite3.connect(":memory:")
    economy_store.init_economy_schema(conn)
    return conn


def test_genesis_round_trips():
    conn = _conn()
    assert economy_store.genesis_exists(conn) is False
    g = trait_economy.Genesis(
        trait_counts={("Background", "Sky"): 2, ("Head", "None"): 1},
        edition_bodies={1: ("Straight", "male"), 2: ("Curved", "female")},
    )
    economy_store.freeze_genesis(conn, g, {"network": "testnet", "max_edition": "3535"})
    assert economy_store.genesis_exists(conn) is True
    got = economy_store.read_genesis(conn)
    assert got.trait_counts == g.trait_counts
    assert got.edition_bodies == g.edition_bodies
    assert economy_store.read_meta(conn, "max_edition") == "3535"
    assert economy_store.read_meta(conn, "absent") is None


def test_clear_genesis_empties_baseline():
    conn = _conn()
    g = trait_economy.Genesis(trait_counts={("Head", "None"): 1}, edition_bodies={1: ("S", "male")})
    economy_store.freeze_genesis(conn, g, {})
    economy_store.clear_genesis(conn)
    assert economy_store.genesis_exists(conn) is False


def test_genesis_exists_keys_off_complete_flag_not_table_rows():
    # Rows in a genesis table without the completion flag (e.g. an interrupted
    # freeze) must NOT read as a complete genesis.
    conn = _conn()
    conn.execute(
        "INSERT INTO trait_genesis (slot, value, genesis_count) VALUES (?, ?, ?)",
        ("Background", "Sky", 1),
    )
    conn.commit()
    assert economy_store.genesis_exists(conn) is False
    # A full freeze writes the flag and flips existence to True.
    g = trait_economy.Genesis(trait_counts={("Head", "None"): 1}, edition_bodies={1: ("S", "male")})
    economy_store.freeze_genesis(conn, g, {"max_edition": "3535"})
    assert economy_store.genesis_exists(conn) is True


def test_freeze_genesis_replaces_atomically():
    # A second freeze fully replaces the first (no leftover rows from the old set).
    conn = _conn()
    first = trait_economy.Genesis(
        trait_counts={("Background", "Sky"): 5},
        edition_bodies={1: ("S", "male"), 2: ("C", "female")},
    )
    economy_store.freeze_genesis(conn, first, {"max_edition": "2"})
    second = trait_economy.Genesis(
        trait_counts={("Head", "Crown"): 1},
        edition_bodies={1: ("S", "male")},
    )
    economy_store.freeze_genesis(conn, second, {"max_edition": "1"})
    got = economy_store.read_genesis(conn)
    assert got.trait_counts == {("Head", "Crown"): 1}
    assert got.edition_bodies == {1: ("S", "male")}
    assert economy_store.read_meta(conn, "max_edition") == "1"


def test_live_state_readers_empty_then_populated():
    conn = _conn()
    assert economy_store.read_bucket_assets(conn) == []
    assert economy_store.read_bucket_bodies(conn) == []
    assert economy_store.read_trait_tokens(conn) == []

    conn.execute(
        "INSERT INTO bucket_assets (owner, slot, value, count) VALUES (?, ?, ?, ?)",
        ("rA", "Background", "Sky", 3),
    )
    conn.execute("INSERT INTO bucket_bodies (owner, edition) VALUES (?, ?)", ("rA", 7))
    conn.execute(
        "INSERT INTO trait_tokens (nft_id, owner, slot, value) VALUES (?, ?, ?, ?)",
        ("tok1", "rB", "Head", "Crown"),
    )
    conn.commit()

    assert economy_store.read_bucket_assets(conn) == [("rA", "Background", "Sky", 3)]
    assert economy_store.read_bucket_bodies(conn) == [("rA", 7)]
    assert economy_store.read_trait_tokens(conn) == [("tok1", "rB", "Head", "Crown")]
