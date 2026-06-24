# tests/test_server_identity_wiring.py
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
from webapp import server  # noqa: E402


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
