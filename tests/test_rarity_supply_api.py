"""GET /api/rarity/supply (agent users spec §3): shape, the 60 s per-network
cache, and parity with the nft_rarity board."""

import asyncio
import json
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from lfg_core import history_store, leaderboard
from lfg_core.nft_index import init_db as init_onchain_db
from lfg_service import app as server

TOKENS = [  # nft_id, nft_number, is_burned, attributes
    (
        "N1",
        1,
        0,
        [{"trait_type": "Background", "value": "Blue"}, {"trait_type": "Hat", "value": "Common"}],
    ),
    (
        "N2",
        2,
        0,
        [{"trait_type": "Background", "value": "Blue"}, {"trait_type": "Hat", "value": "Unique"}],
    ),
    (
        "N3",
        3,
        0,
        [
            {"trait_type": "Background", "value": "Red"},
            {"trait_type": "Hat", "value": "Common"},
            {"trait_type": "Accessory", "value": ""},
        ],
    ),
    (
        "N4",
        4,
        1,  # burned
        [{"trait_type": "Background", "value": "Blue"}, {"trait_type": "Hat", "value": "Unique"}],
    ),
    ("N5", 5, 0, []),  # live, no pairs
]


@pytest.fixture
def supply_env(tmp_path, monkeypatch):
    path = str(tmp_path / "onchain_testnet.db")
    oconn = init_onchain_db(path)
    oconn.executemany(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, attributes_json)"
        " VALUES (?,?,?,?,?)",
        [(i, n, "rOwner", b, json.dumps(a)) for i, n, b, a in TOKENS],
    )
    oconn.commit()
    oconn.close()
    monkeypatch.setenv("ONCHAIN_DB_PATH", path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    server._SUPPLY_CACHE.clear()
    yield path
    server._SUPPLY_CACHE.clear()


def _get():
    req = make_mocked_request("GET", "/api/rarity/supply", app=web.Application())
    resp = asyncio.get_event_loop().run_until_complete(server.handle_rarity_supply(req))
    return resp.status, json.loads(resp.body)


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_supply_counts_live_tokens_per_trait_value(supply_env):
    status, body = _get()
    assert status == 200
    assert body["network"] == "testnet"
    assert isinstance(body["as_of"], int)
    assert body["n_live"] == 4  # N1, N2, N3 and N5 (no pairs); N4 is burned
    assert body["counts"] == {
        "Accessory": {"": 1},
        "Background": {"Blue": 2, "Red": 1},
        "Hat": {"Common": 2, "Unique": 1},
    }


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_supply_is_cached_60s_per_network(supply_env, monkeypatch):
    calls = []
    real = leaderboard.live_trait_table

    def counting(oconn):
        calls.append(1)
        return real(oconn)

    monkeypatch.setattr(server.leaderboard, "live_trait_table", counting)
    first = _get()[1]
    assert _get()[1] == first and len(calls) == 1  # within the TTL: cached
    # ONCHAIN_DB_PATH overrides the index path for every network, so mainnet reads
    # the same fixture; only the cache key differs
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "mainnet")
    assert _get()[1]["network"] == "mainnet" and len(calls) == 2  # keyed per network
    ts, payload = server._SUPPLY_CACHE["testnet"]
    server._SUPPLY_CACHE["testnet"] = (ts - server._SUPPLY_CACHE_TTL - 1, payload)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    _get()
    assert len(calls) == 3  # expired: recomputed


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_supply_counts_reproduce_the_nft_rarity_board(supply_env):
    _, body = _get()
    oconn = init_onchain_db(supply_env)
    oconn.row_factory = sqlite3.Row
    board = leaderboard.compute(
        "nft_rarity",
        history_store.init_history_db(":memory:"),
        oconn,
        start_ts=0,
        end_ts=99,
        network="testnet",
        system_accounts=frozenset(),
    )
    n_live = body["n_live"] or 1
    attrs = {i: a for i, _, _, a in TOKENS}
    for row in board:
        pairs = [(t["trait_type"], str(t["value"])) for t in attrs[row["nft_id"]]]
        expected = round(sum(n_live / body["counts"][tt][v] for tt, v in pairs), 2)
        assert row["value"] == expected
    assert {r["nft_id"] for r in board} == {"N1", "N2", "N3"}
