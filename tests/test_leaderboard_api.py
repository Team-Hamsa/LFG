# Tests for GET /api/leaderboard: public endpoint, 60s cache, board/period
# validation, and `me` rank lookup. Run: .venv/bin/python -m pytest
# tests/test_leaderboard_api.py -v

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Provide dummy env so lfg_core.config import doesn't fail in CI/dev shells
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import leaderboard  # noqa: E402
from lfg_core.history_store import init_history_db, insert_nft_event  # noqa: E402
from lfg_core.nft_index import init_db as init_onchain_db  # noqa: E402
from lfg_service import app as server  # noqa: E402

WALLET_A = "rAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLET_B = "rBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
WALLET_ME = "rMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEM"


def _seed_dbs(tmp_path):
    history_path = str(tmp_path / "history_testnet.db")
    onchain_path = str(tmp_path / "onchain_testnet.db")

    hconn = init_history_db(history_path)
    now = int(time.time())
    # A few "mint" events within the last day so a "week"/"all" board has rows.
    events = [
        ("txA1", "0001", 1, "mint", None, WALLET_A, now - 3600),
        ("txA2", "0002", 2, "mint", None, WALLET_A, now - 3600),
        ("txB1", "0003", 3, "mint", None, WALLET_B, now - 3600),
        ("txM1", "0004", 4, "mint", None, WALLET_ME, now - 3600),
    ]
    for tx_hash, nft_id, nft_number, event, from_addr, to_addr, ts in events:
        insert_nft_event(
            hconn,
            {
                "tx_hash": tx_hash,
                "nft_id": nft_id,
                "nft_number": nft_number,
                "event": event,
                "from_addr": from_addr,
                "to_addr": to_addr,
                "price_drops": None,
                "price_token": None,
                "ledger_index": 1,
                "ts": ts,
            },
        )
    hconn.commit()
    hconn.close()

    oconn = init_onchain_db(onchain_path)
    oconn.commit()
    oconn.close()

    return history_path, onchain_path


@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    history_path, onchain_path = _seed_dbs(tmp_path)
    monkeypatch.setenv("HISTORY_DB_PATH", history_path)
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain_path)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    server._LB_CACHE.clear()
    yield history_path, onchain_path
    server._LB_CACHE.clear()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mocked_request(method, path):
    return make_mocked_request(method, path, app=web.Application())


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_leaderboard_200_ranked_rows(seeded_env):
    req = _mocked_request(
        "GET", "/api/leaderboard?board=users_swaps&period=all"
    )
    resp = _run(server.handle_leaderboard(req))
    assert resp.status == 200


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_leaderboard_200_users_nfts_ranked(seeded_env):
    req = _mocked_request(
        "GET", "/api/leaderboard?board=users_nfts&period=week"
    )
    resp = _run(server.handle_leaderboard(req))
    assert resp.status == 200
    body = asyncio.get_event_loop().run_until_complete(
        _read_json(resp)
    )
    assert body["board"] == "users_nfts"
    assert body["period"] == "week"
    assert len(body["rows"]) >= 1
    ranks = [r["rank"] for r in body["rows"]]
    assert ranks == list(range(1, len(ranks) + 1))


async def _read_json(resp):
    return resp._body and __import__("json").loads(resp._body)


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_leaderboard_bad_board_400(seeded_env):
    req = _mocked_request(
        "GET", "/api/leaderboard?board=nope&period=all")
    resp = _run(server.handle_leaderboard(req))
    assert resp.status == 400


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_leaderboard_bad_period_400(seeded_env):
    req = _mocked_request(
        "GET", "/api/leaderboard?board=users_nfts&period=fortnight"
    )
    resp = _run(server.handle_leaderboard(req))
    assert resp.status == 400


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_leaderboard_me_block(seeded_env):
    req = _mocked_request(
        "GET",
        f"/api/leaderboard?board=users_nfts&period=week&me={WALLET_ME}",
    )
    resp = _run(server.handle_leaderboard(req))
    assert resp.status == 200
    body = __import__("json").loads(resp._body)
    assert body["me"] is not None
    assert body["me"]["rank"] >= 1
    assert body["me"]["value"] >= 1


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_leaderboard_cache_hit(seeded_env, monkeypatch):
    req1 = _mocked_request(
        "GET", "/api/leaderboard?board=users_nfts&period=week")
    resp1 = _run(server.handle_leaderboard(req1))
    assert resp1.status == 200

    def _boom(*args, **kwargs):
        raise AssertionError("compute() should not be called on a cache hit")

    monkeypatch.setattr(server.leaderboard, "compute", _boom)

    req2 = _mocked_request(
        "GET", "/api/leaderboard?board=users_nfts&period=week")
    resp2 = _run(server.handle_leaderboard(req2))
    assert resp2.status == 200
