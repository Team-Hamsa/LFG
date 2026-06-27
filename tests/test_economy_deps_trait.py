# Tests for trait callable wiring in _economy_deps.build_economy_deps and _compose_trait.

import asyncio
import os
import sys

# Minimal env stubs (match test_economy_scripts_import.py pattern)
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


def _run(coro):
    # Use new_event_loop (not asyncio.run) so the policy's current loop is not
    # poisoned for later tests that rely on asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_build_economy_deps_has_trait_callables():
    """All six trait callable fields must be non-None after build_economy_deps."""
    import sqlite3

    import _economy_deps as deps

    from lfg_core import economy_store

    conn = sqlite3.connect(":memory:")
    economy_store.init_economy_schema(conn)
    d = deps.build_economy_deps(conn)

    for attr in (
        "trait_compose_fn",
        "trait_upload_fn",
        "trait_mint_fn",
        "trait_burn_fn",
        "trait_info_fn",
        "trait_meta_fn",
    ):
        assert getattr(d, attr) is not None, f"{attr} should not be None"
        assert callable(getattr(d, attr)), f"{attr} should be callable"


def test_compose_trait_scans_bodies(tmp_path, monkeypatch):
    """_compose_trait finds the layer in the SECOND body (first has no match)."""
    import _economy_deps as deps

    from lfg_core import layer_store

    # Create a fake layer file in second body
    slot = "Eyes"
    value = "Laser"
    ext = "png"
    body_a = "body_a"
    body_b = "body_b"

    layer_file = tmp_path / body_b / slot / f"{value}.{ext}"
    layer_file.parent.mkdir(parents=True)
    layer_file.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header bytes

    class FakeStore:
        async def list_bodies(self):
            return [body_a, body_b]

        async def resolve(self, body, trait_type, val):
            if body == body_b and trait_type == slot and val == value:
                return str(layer_file)
            return None

    monkeypatch.setattr(layer_store, "get_layer_store", lambda: FakeStore())

    fake_url = "https://cdn.example.com/traits/abc.png"

    async def fake_upload(path_on_cdn, data, content_type):
        return fake_url

    monkeypatch.setattr(deps, "_upload", fake_upload)

    result = _run(deps._compose_trait(slot, value))
    assert result == fake_url
