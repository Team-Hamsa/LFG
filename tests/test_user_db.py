# tests/test_user_db.py
# #199: user_db's four DB functions used an unguarded `finally: conn.close()`
# with the connect INSIDE the try — a failed sqlite3.connect raised
# UnboundLocalError from the finally, masking the real error (and turning
# create_users_table's logged-and-swallowed failure into a crash).
#
# Env guard: set before lfg_core imports so frozen config constants are sane
# when this file runs first (see test-env-guard convention).
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3  # noqa: E402

from lfg_core import user_db  # noqa: E402


def test_connect_failure_surfaces_handled_errors_not_unboundlocal(monkeypatch):
    """If sqlite3.connect itself raises, each function's own error handling
    must run — not an UnboundLocalError from the guarded finally."""

    def _boom(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(user_db.sqlite3, "connect", _boom)
    user_db.create_users_table()  # swallows-and-logs; the bug crashed here
    assert user_db.register_user("1", "n", "rW") is False
    assert user_db.get_user("1") is None
    assert user_db.get_all_registered_users() == []
