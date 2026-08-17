# tests/test_batch_harvest.py
# Batch harvest (#356): N stacked server-side harvests behind one gesture.
# The batch endpoint is an orchestration layer over the EXISTING fire-and-forget
# single-harvest machinery (PR #307): it validates the id list, then reserves +
# starts one harvest session per character via the same _reserve_economy_slot /
# economy_api.start_harvest seam the single POST /api/harvest uses. Per-unit
# failure is isolated -- a mid-batch precondition error must not strand the rest.
import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_service import app as service_app  # noqa: E402
from lfg_service.app import (  # noqa: E402
    BATCH_HARVEST_MAX,
    economy_sessions,
    start_batch_harvest,
    validate_batch_nft_ids,
)
from webapp import economy_api  # noqa: E402


def _run(coro):
    # A fresh loop per call: earlier async suites close/replace the ambient
    # loop, so get_event_loop() is not full-suite-order safe.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _clean_sessions():
    economy_sessions.clear()
    yield
    economy_sessions.clear()


# ---------------------------------------------------------------------------
# validate_batch_nft_ids: wire-shape preconditions -> EconomyError (HTTP 400)
# ---------------------------------------------------------------------------


def test_validate_happy_path_preserves_order():
    assert validate_batch_nft_ids({"nft_ids": ["B", "A", "C"]}) == ["B", "A", "C"]


def test_validate_rejects_missing_or_non_list():
    for body in ({}, {"nft_ids": None}, {"nft_ids": "AAA"}, {"nft_ids": {}}):
        with pytest.raises(economy_api.EconomyError):
            validate_batch_nft_ids(body)


def test_validate_rejects_empty_list():
    with pytest.raises(economy_api.EconomyError):
        validate_batch_nft_ids({"nft_ids": []})


def test_validate_rejects_non_string_and_empty_entries():
    for bad in ([1], [None], [""], ["A", 2]):
        with pytest.raises(economy_api.EconomyError):
            validate_batch_nft_ids({"nft_ids": bad})


def test_validate_rejects_duplicates():
    with pytest.raises(economy_api.EconomyError, match="duplicate"):
        validate_batch_nft_ids({"nft_ids": ["A", "B", "A"]})


def test_validate_enforces_batch_cap():
    ids = [f"NFT{i}" for i in range(BATCH_HARVEST_MAX + 1)]
    with pytest.raises(economy_api.EconomyError):
        validate_batch_nft_ids({"nft_ids": ids})
    # exactly at the cap is fine
    assert len(validate_batch_nft_ids({"nft_ids": ids[:BATCH_HARVEST_MAX]})) == BATCH_HARVEST_MAX


# ---------------------------------------------------------------------------
# start_batch_harvest orchestration
# ---------------------------------------------------------------------------


class _FakeWebSession:
    """Shape-compatible stand-in for economy_api.EconomyWebSession."""

    def __init__(self, user_id, nft_id):
        self.discord_id = user_id
        self.kind = "harvest"
        self.platform = "discord"
        self.id = f"sess-{nft_id}"
        self.state = "running"
        self.inner = SimpleNamespace(owner="rOWNER", character=SimpleNamespace(nft_id=nft_id))

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "state": self.state, "error": None}


def _fake_start_harvest(fail_ids=(), crash_ids=()):
    calls = []

    async def fake(user_id, owner, nft_id, user_token=None):
        calls.append(nft_id)
        if nft_id in fail_ids:
            raise economy_api.EconomyError(f"cannot harvest: nope ({nft_id})")
        if nft_id in crash_ids:
            raise RuntimeError("boom")
        return _FakeWebSession(user_id, nft_id)

    fake.calls = calls
    return fake


def test_batch_all_succeed_registers_stacked_sessions(monkeypatch):
    fake = _fake_start_harvest()
    monkeypatch.setattr(economy_api, "start_harvest", fake)
    results = _run(start_batch_harvest("u1", "rOWNER", "discord", ["A", "B", "C"]))
    assert [r["nft_id"] for r in results] == ["A", "B", "C"]
    assert all(r["error"] is None and r["session_id"] for r in results)
    # every real session is registered under its own id (pollable via
    # GET /api/harvest/{session_id}) and no placeholder leaked
    assert set(economy_sessions) == {"sess-A", "sess-B", "sess-C"}
    assert all(s.kind == "harvest" for s in economy_sessions.values())


def test_batch_partial_failure_does_not_strand_the_rest(monkeypatch):
    fake = _fake_start_harvest(fail_ids={"B"})
    monkeypatch.setattr(economy_api, "start_harvest", fake)
    results = _run(start_batch_harvest("u1", "rOWNER", "discord", ["A", "B", "C"]))
    by_id = {r["nft_id"]: r for r in results}
    assert by_id["A"]["error"] is None and by_id["C"]["error"] is None
    assert by_id["B"]["session_id"] is None
    assert "cannot harvest" in by_id["B"]["error"]
    # C was still attempted after B failed
    assert fake.calls == ["A", "B", "C"]
    # only the two real sessions remain; B's placeholder was released
    assert set(economy_sessions) == {"sess-A", "sess-C"}


def test_batch_unexpected_crash_is_isolated_and_user_safe(monkeypatch):
    fake = _fake_start_harvest(crash_ids={"A"})
    monkeypatch.setattr(economy_api, "start_harvest", fake)
    results = _run(start_batch_harvest("u1", "rOWNER", "discord", ["A", "B"]))
    by_id = {r["nft_id"]: r for r in results}
    assert by_id["A"]["session_id"] is None
    assert "boom" not in by_id["A"]["error"]  # internal detail never leaks
    assert by_id["B"]["error"] is None
    assert set(economy_sessions) == {"sess-B"}


def test_batch_conflicts_with_inflight_same_nft(monkeypatch):
    fake = _fake_start_harvest()
    monkeypatch.setattr(economy_api, "start_harvest", fake)
    # a live single harvest of A (same user/platform) is already stacked
    live = _FakeWebSession("u1", "A")
    economy_sessions[live.id] = live
    results = _run(start_batch_harvest("u1", "rOWNER", "discord", ["A", "B"]))
    by_id = {r["nft_id"]: r for r in results}
    assert by_id["A"]["error"] == "that character is already being harvested"
    assert by_id["B"]["error"] is None
    assert fake.calls == ["B"]  # A was never started twice


def test_batch_blocked_entirely_by_non_harvest_op(monkeypatch):
    fake = _fake_start_harvest()
    monkeypatch.setattr(economy_api, "start_harvest", fake)
    equip = SimpleNamespace(
        discord_id="u1",
        platform="discord",
        kind="equip",
        state="running",
        inner=SimpleNamespace(),
    )
    economy_sessions["eq"] = equip
    results = _run(start_batch_harvest("u1", "rOWNER", "discord", ["A", "B"]))
    assert all(r["error"] == "an economy action is already in progress" for r in results)
    assert fake.calls == []
    assert set(economy_sessions) == {"eq"}


def test_batch_units_conflict_with_each_other_never(monkeypatch):
    # Successive units of one batch are DIFFERENT nft_ids by validation, and a
    # started unit must not block the next one (harvests stack per #307).
    fake = _fake_start_harvest()
    monkeypatch.setattr(economy_api, "start_harvest", fake)
    results = _run(start_batch_harvest("u1", "rOWNER", "discord", ["A", "B", "C", "D"]))
    assert all(r["error"] is None for r in results)
    assert len(economy_sessions) == 4


def test_batch_route_registered():
    app = service_app.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/harvest/batch" in paths
    # the single-harvest routes are untouched
    assert "/api/harvest" in paths
    assert "/api/harvest/{session_id}" in paths


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_batch_handler_dev_mode_per_item_results(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    from webapp import mock_economy

    monkeypatch.setattr(service_app.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(service_app.config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(mock_economy, "INSTANCE", mock_economy.MockEconomy())
    # walk the mock Closet lifecycle to active so harvest preconditions pass
    for _ in range(3):
        if mock_economy.INSTANCE._closet_active(mock_economy.DEV_OWNER):
            break
        mock_economy.INSTANCE.create_closet(mock_economy.DEV_OWNER)
    state = mock_economy.INSTANCE.read_state(mock_economy.DEV_OWNER)
    ids = [c["nft_id"] for c in state["characters"] if not c.get("blank")][:2]
    assert ids, "mock economy should seed at least one dressed character"
    req = make_mocked_request("POST", "/api/harvest/batch")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER
    body = {"nft_ids": ids + ["missing-nft"]}

    async def _json():
        return body

    req.json = _json
    resp = _run(service_app.handle_harvest_batch_start(req))
    assert resp.status == 200
    import json

    payload = json.loads(resp.body)
    by_id = {r["nft_id"]: r for r in payload["results"]}
    for nid in ids:
        assert by_id[nid]["error"] is None
    assert by_id["missing-nft"]["error"]


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_batch_handler_malformed_body_is_400(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(service_app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(service_app.config, "ECONOMY_ENABLED", True)
    req = make_mocked_request("POST", "/api/harvest/batch")
    req["user"] = {"id": "u1", "name": "u"}
    req["wallet"] = "rOWNER"

    async def _json():
        return {"nft_ids": []}

    req.json = _json
    resp = _run(service_app.handle_harvest_batch_start(req))
    assert resp.status == 400
