# Smoke test: the CLI drivers import cleanly and expose main().

import importlib
import os
import sys

import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))


@pytest.mark.parametrize(
    "mod_name",
    [
        "economy_harvest",
        "economy_assemble",
        "economy_equip",
        "migrate_bucket_to_closet",
        "economy_extract",
        "economy_deposit",
        "reconcile_supply_growth",
    ],
)
def test_cli_driver_exposes_main(mod_name):
    mod = importlib.import_module(mod_name)
    assert hasattr(mod, "main") and callable(mod.main)


def test_build_economy_deps_wires_all_callables():
    import sqlite3

    import _economy_deps as deps

    from lfg_core import economy_store

    conn = sqlite3.connect(":memory:")
    economy_store.init_economy_schema(conn)
    d = deps.build_economy_deps(conn)
    assert d.conn is conn
    for fn in (d.closet_mint_fn, d.char_mint_fn, d.char_burn_fn, d.char_compose_fn):
        assert callable(fn)
