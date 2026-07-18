# tests/test_x_admin_toggle.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_seasons.py.)
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

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from lfg_core import config  # noqa: E402
from lfg_service import app as server  # noqa: E402
from surfaces.x_bot import state  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]

    async def json(self):
        return {}


@pytest.fixture(autouse=True)
def _isolated_x_state(tmp_path, monkeypatch):
    # config.X_STATE_DB_PATH is read lazily (attribute access at call time
    # inside each handler, not bound at import), so patching the attribute
    # here isolates every test in this file from the real x_state.db.
    monkeypatch.setattr(config, "X_STATE_DB_PATH", str(tmp_path / "x_state.db"))


@pytest.fixture(autouse=True)
def _service_tokens(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")


_HANDLER_NAMES = ["handle_x_pause", "handle_x_resume", "handle_x_status"]


@pytest.mark.parametrize("handler_name", _HANDLER_NAMES)
def test_requires_service_token(handler_name):
    handler = getattr(server, handler_name)
    resp = _run(handler(_FakeRequest({})))
    assert resp.status == 401


@pytest.mark.parametrize("handler_name", _HANDLER_NAMES)
def test_non_discord_surface_is_forbidden(handler_name):
    handler = getattr(server, handler_name)
    resp = _run(handler(_FakeRequest({"Authorization": "Bearer tok-t"})))
    assert resp.status == 403
    body = json.loads(resp.body)
    assert body["code"] == "wrong_surface"


def test_pause_sets_paused_true_and_returns_200():
    resp = _run(server.handle_x_pause(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert resp.status == 200
    assert json.loads(resp.body) == {"paused": True}
    assert state.posting_paused(config.X_STATE_DB_PATH) is True


def test_pause_is_idempotent():
    _run(server.handle_x_pause(_FakeRequest({"Authorization": "Bearer tok-d"})))
    resp = _run(server.handle_x_pause(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert resp.status == 200
    assert json.loads(resp.body) == {"paused": True}
    assert state.posting_paused(config.X_STATE_DB_PATH) is True


def test_resume_sets_paused_false_and_returns_200():
    state.set_posting_paused(config.X_STATE_DB_PATH, True)
    resp = _run(server.handle_x_resume(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert resp.status == 200
    assert json.loads(resp.body) == {"paused": False}
    assert state.posting_paused(config.X_STATE_DB_PATH) is False


def test_resume_is_idempotent():
    resp = _run(server.handle_x_resume(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert resp.status == 200
    assert json.loads(resp.body) == {"paused": False}
    assert state.posting_paused(config.X_STATE_DB_PATH) is False


def test_status_shape_defaults():
    resp = _run(server.handle_x_status(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {
        "paused": False,
        "month_posts": 0,
        "budget": config.X_MONTHLY_POST_BUDGET,
        "enabled": config.X_ENABLED,
    }


def test_status_reflects_paused_and_month_posts():
    state.set_posting_paused(config.X_STATE_DB_PATH, True)
    state.record(config.X_STATE_DB_PATH, "mint:1", "posted", tweet_id="1")
    resp = _run(server.handle_x_status(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["paused"] is True
    assert body["month_posts"] == 1


def test_status_works_when_x_disabled(monkeypatch):
    # Global constraint: nothing here may hard-require X creds — the admin
    # must be able to inspect/toggle state while the feature is dark.
    monkeypatch.setattr(config, "X_ENABLED", False)
    resp = _run(server.handle_x_status(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert resp.status == 200
    assert json.loads(resp.body)["enabled"] is False


def test_pause_gates_poster_pipeline():
    # The poster pipeline's own pause gate (surfaces/x_bot/bot.py reading
    # state.posting_paused(deps.db_path)) is unit-tested end-to-end in
    # tests/test_x_poster.py
    # (test_handle_event_paused_records_skipped_paused_no_api_call) — not
    # duplicated here. This test only confirms the service endpoint writes
    # the exact flag the poster reads, via the shared state.py helpers.
    _run(server.handle_x_pause(_FakeRequest({"Authorization": "Bearer tok-d"})))
    assert state.posting_paused(config.X_STATE_DB_PATH) is True


def test_x_admin_routes_registered():
    app = server.create_app()
    method_paths = {(r.method, getattr(r.resource, "canonical", "")) for r in app.router.routes()}
    assert ("POST", "/api/admin/x/pause") in method_paths
    assert ("POST", "/api/admin/x/resume") in method_paths
    assert ("GET", "/api/admin/x/status") in method_paths
