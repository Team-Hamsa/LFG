# tests/test_server_identity_wiring.py
import asyncio
import os

# Set env vars before any lfg_core.config import so module-level constants
# (e.g. IMG_PROXY_ALLOWED_BASES) are frozen with the correct values even when
# this file is collected before webapp/test_smoke.py.
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import lfg_service.identity as identity  # noqa: E402
from lfg_service import app as server  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_create_app_ensures_and_migrates(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    called = {}
    monkeypatch.setattr(
        identity, "ensure_identities_table", lambda: called.setdefault("ensure", True)
    )
    monkeypatch.setattr(
        identity, "migrate_users_to_identities", lambda: called.setdefault("migrate", 0)
    )
    server.create_app()
    assert called.get("ensure") is True
    assert "migrate" in called


class _SigninReq:
    def __init__(self, uuid):
        self.match_info = {"payload_uuid": uuid}
        self.headers = {}
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


def test_signin_status_mirrors_wallet_into_identities(tmp_path, monkeypatch):
    # The XUMM sign-in success path must dual-write to identities, like
    # handle_register — otherwise /events/me 403s for sign-in users until restart.
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)  # require_auth -> user id "dev"
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "t.db"))
    identity.ensure_identities_table()
    monkeypatch.setattr(server, "register_user", lambda *a, **k: True)
    monkeypatch.setattr(server, "is_valid_classic_address", lambda a: True)
    server.signin_payloads["u-1"] = {"discord_id": "dev", "name": "dev", "created_at": 0}

    async def fake_status(uuid):
        return {"signed": True, "account": "rWALLET", "expired": False}

    monkeypatch.setattr(server.xumm_ops, "get_payload_status", fake_status)

    resp = _run(server.handle_signin_status(_SigninReq("u-1")))
    assert resp.status == 200
    assert identity.resolve("discord", "dev") == "rWALLET"
