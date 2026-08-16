# Tests for scripts/snapshot_balances.py
import asyncio
import os
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
os.environ.setdefault("BUNNY_PULL_ZONE", "")
os.environ.setdefault("ECONOMY_ENABLED", "1")

import importlib

from lfg_core import history_store

sb = importlib.import_module("scripts.snapshot_balances")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_collect_and_write(tmp_path):
    async def request_fn(req):
        if req["method"] == "account_lines" and req["account"] == "rBrix":
            return {"lines": [{"account": "rA", "balance": "-10"}]}
        if req["method"] == "account_lines" and req["account"] == "rAmm":
            return {"lines": [{"account": "rA", "balance": "-2.5"}]}
        raise AssertionError(req)

    bal = _run(sb.collect_balances(request_fn, "rBrix", "rAmm"))
    assert bal == {"rA": {"brix": 10.0, "lp": 2.5}}

    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    assert sb.write_snapshot(conn, bal, "2026-07-04") == 1
    assert sb.write_snapshot(conn, bal, "2026-07-04") == 1
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0] == 1
