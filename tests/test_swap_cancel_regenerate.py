# tests/test_swap_cancel_regenerate.py
# The Trait Swapper's fee-QR screen shipped with no way out: a stale/expired
# XUMM payload left the user staring at a dead QR with no regenerate and no
# back button (they had to close and reopen the whole Activity). Mirror the
# mint flow's machinery onto SwapSession: regenerate_payment (issue #22
# equivalent) and cancel (issue #141 equivalent), plus the service endpoints.
#
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants at import time; set the same defaults test_smoke.py /
# test_server_identity_wiring.py use so collection order can't strand them.
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
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import memos, swap_flow, xumm_ops  # noqa: E402
from lfg_service import app as server  # noqa: E402

NFT = {"name": "LFG #1", "image": "https://cdn.example/1.png"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _session(state=swap_flow.AWAITING_PAYMENT, discord_id="dev", platform="discord"):
    s = swap_flow.SwapSession(
        discord_id=discord_id,
        wallet_address="rTest",
        nft1=NFT,
        nft2=NFT,
        traits_to_swap=["Hat"],
        platform=platform,
    )
    s.state = state
    return s


async def _read_json(resp):
    return json.loads(resp.body.decode())


# --- SwapSession.cancel (mirror of mint issue #141) ------------------------


def test_cancelled_is_a_terminal_state():
    assert swap_flow.CANCELLED == "cancelled"
    assert swap_flow.CANCELLED in swap_flow.TERMINAL_STATES


def test_cancel_awaiting_payment_succeeds_and_stops_task():
    s = _session()

    async def hang():
        await asyncio.sleep(3600)

    async def run():
        s.task = asyncio.get_event_loop().create_task(hang())
        assert s.cancel() is True
        assert s.state == swap_flow.CANCELLED
        await asyncio.sleep(0)
        assert s.task.cancelled()

    _run(run())


def test_cancel_refused_once_past_payment():
    for state in (swap_flow.COMPOSING, swap_flow.MINTING, swap_flow.OFFERS_READY):
        s = _session(state=state)
        assert s.cancel() is False
        assert s.state == state


def test_mark_published_sets_publish_guard():
    s = _session()
    s.mark_published()
    assert s._published is True


# --- SwapSession.regenerate_payment (mirror of mint issue #22) --------------


def test_regenerate_payment_builds_fresh_payload(monkeypatch):
    s = _session()
    s.fee_amount = "6"
    s.fee_destination = "rBotWallet"
    s.fee_currency = "XRP"
    s.fee_issuer = None
    s.payment_link = "https://xumm.app/sign/OLD"
    captured = {}

    async def fake_payload(destination, **kw):
        captured["destination"] = destination
        captured.update(kw)
        return {"xumm_url": "https://xumm.app/sign/NEW", "uuid": "u2"}

    monkeypatch.setattr(xumm_ops, "create_payment_payload", fake_payload)
    _run(s.regenerate_payment())
    assert s.payment_link == "https://xumm.app/sign/NEW"
    assert captured["destination"] == "rBotWallet"
    assert captured["value"] == "6"
    assert captured["currency"] == "XRP"
    # Provenance invariants at this boundary (the SourceTag itself is stamped
    # below it, inside _create_xumm_payload, covered by the sourcetag tests):
    # the rebuilt payload must carry the same closed-schema action and the
    # session's real originating surface.
    assert captured["action"] == memos.ACTION_TRAIT_SWAP_FEE
    assert captured["platform"] == memos.platform_for_surface("discord")


def test_regenerate_payment_keeps_old_link_on_failure(monkeypatch):
    s = _session()
    s.fee_amount = "6"
    s.fee_destination = "rBotWallet"
    s.fee_currency = "XRP"
    s.fee_issuer = None
    s.payment_link = "https://xumm.app/sign/OLD"

    async def fail(destination, **kw):
        return None

    monkeypatch.setattr(xumm_ops, "create_payment_payload", fail)
    _run(s.regenerate_payment())
    assert s.payment_link == "https://xumm.app/sign/OLD"


# --- service endpoints ------------------------------------------------------


@pytest.fixture
def dev_auth(monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "swap_sessions", {})
    return server.swap_sessions


def _request(method, session_id):
    return make_mocked_request(
        method,
        f"/api/swap/{session_id}/x",
        match_info={"session_id": session_id},
        app=web.Application(),
    )


def test_swap_cancel_awaiting_payment(dev_auth):
    s = _session()
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_cancel(_request("POST", s.id)))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["state"] == "cancelled"
    # A deliberate cancel is not a swap outcome: the terminal firehose
    # publish a late status poll would fire must be suppressed.
    assert s._published is True


def test_swap_cancel_terminal_is_noop(dev_auth):
    s = _session(state=swap_flow.FAILED)
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_cancel(_request("POST", s.id)))
    assert resp.status == 200
    assert _run(_read_json(resp))["state"] == "failed"


def test_swap_cancel_past_payment_409(dev_auth):
    s = _session(state=swap_flow.COMPOSING)
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_cancel(_request("POST", s.id)))
    assert resp.status == 409


def test_swap_cancel_foreign_user_404(dev_auth):
    s = _session(discord_id="someone-else")
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_cancel(_request("POST", s.id)))
    assert resp.status == 404


def test_swap_regenerate_awaiting_payment(dev_auth, monkeypatch):
    s = _session()
    s.fee_amount = "6"
    s.fee_destination = "rBotWallet"
    s.fee_currency = "XRP"
    s.fee_issuer = None

    async def fake_payload(destination, **kw):
        return {"xumm_url": "https://xumm.app/sign/NEW", "uuid": "u2"}

    monkeypatch.setattr(xumm_ops, "create_payment_payload", fake_payload)
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_regenerate(_request("POST", s.id)))
    assert resp.status == 200
    assert _run(_read_json(resp))["payment_link"] == "https://xumm.app/sign/NEW"


def test_swap_regenerate_past_payment_409(dev_auth):
    s = _session(state=swap_flow.MINTING)
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_regenerate(_request("POST", s.id)))
    assert resp.status == 409


def test_swap_start_stores_task_handle():
    """cancel() can only stop the payment wait if the background task handle
    is kept on the session — assert handle_swap_start assigns it (source
    guard: the create_task result must land on session.task)."""
    import inspect

    # handle_swap_start is decorated, so getsource(wrapper) would show the
    # auth wrapper — slice the module source instead.
    src = inspect.getsource(server).split("async def handle_swap_start", 1)[1]
    src = src.split("\nasync def ", 1)[0]
    assert "session.task = asyncio.get_event_loop().create_task(" in src


def test_collect_fee_leaves_awaiting_payment_synchronously():
    """Race guard (mint learned this in #141): once wait_for_payment reports
    the fee paid, the session must leave AWAITING_PAYMENT in the same
    synchronous step — buy_and_burn awaits AFTER the paid check, and a cancel
    landing in that window would kill a PAID pipeline."""
    import inspect

    src = inspect.getsource(swap_flow._collect_modify_fee)
    paid_idx = src.index("xrpl_ops.wait_for_payment")
    burn_idx = src.index("xrpl_ops.buy_and_burn")
    state_idx = src.index("session.state = COMPOSING", paid_idx)
    assert paid_idx < state_idx < burn_idx


def test_run_swap_session_discards_stills_on_cancel():
    """Compose runs BEFORE the fee screen, so a cancel mid-payment must clean
    up the pending archive stills like any other unfinished swap.
    CancelledError is a BaseException — the generic `except Exception`
    handler can't do it."""
    import inspect

    src = inspect.getsource(swap_flow.run_swap_session)
    assert "except asyncio.CancelledError" in src
    cancel_block = src.split("except asyncio.CancelledError", 1)[1].split("except ", 1)[0]
    assert "image_archive.discard_still(" in cancel_block
    assert cancel_block.rstrip().endswith("raise")


def test_modify_results_include_nft_number():
    """The in-place (NFTokenModify) results.append site must also carry
    nft_number via swap_meta.extract_nft_number, same as the two
    _create_offer_and_accept sites covered behaviorally in
    tests/test_swap_offer_recovery.py — so the swap-result share button
    (#41 T9) never needs to regex item['nft']['name'] client-side. Full
    run_swap_session integration coverage of this block would require
    mocking the entire XRPL burn/modify pipeline (no existing test in the
    repo does that — see test_swap_face_roll.py's early-fail-before-results
    pattern); a source guard, matching this file's existing style for
    hard-to-integration-test invariants (test_run_swap_session_discards_
    stills_on_cancel above), is the pragmatic check here."""
    import inspect

    src = inspect.getsource(swap_flow.run_swap_session)
    # There are two "for item in modify_items:" loops (the modify-execution
    # loop, then this results-append loop) — anchor on the comment
    # immediately preceding the second one so the split isolates it.
    after_modifies = src.split("# The modifies are final now that the burns are through.", 1)[1]
    modify_block = after_modifies.split("if burn_items:", 1)[0]
    assert 'swap_meta.extract_nft_number(item["nft"]["name"])' in modify_block


def test_swap_regenerate_failure_returns_502(dev_auth, monkeypatch):
    """A swallowed regenerate failure used to answer 200 with the OLD link —
    the client showed no error and the button appeared dead (CodeRabbit #216).
    XUMM down (payload build returns None) must surface as 502."""
    s = _session()
    s.fee_amount = "6"
    s.fee_destination = "rBotWallet"
    s.fee_currency = "XRP"
    s.fee_issuer = None
    s.payment_link = "https://xumm.app/sign/OLD"

    async def fail(destination, **kw):
        return None

    monkeypatch.setattr(xumm_ops, "create_payment_payload", fail)
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_regenerate(_request("POST", s.id)))
    assert resp.status == 502


def test_swap_regenerate_exception_returns_502(dev_auth, monkeypatch):
    s = _session()
    s.fee_amount = "6"
    s.fee_destination = "rBotWallet"
    s.fee_currency = "XRP"
    s.fee_issuer = None

    async def boom(destination, **kw):
        raise RuntimeError("xumm down")

    monkeypatch.setattr(xumm_ops, "create_payment_payload", boom)
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_regenerate(_request("POST", s.id)))
    assert resp.status == 502


def test_regenerate_payment_reports_success():
    """regenerate_payment must tell its caller whether a fresh payload was
    actually built, so the service can answer 502 instead of a silent 200."""
    import inspect

    sig = inspect.signature(swap_flow.SwapSession.regenerate_payment)
    assert sig.return_annotation in ("bool", bool)


def test_collect_modify_fee_fails_fast_when_payload_never_created(monkeypatch):
    """#262 (mirror of the mint prod incident): when the initial fee payload
    build fails (429 backoff / outage), _collect_modify_fee used to fall back
    to the static detect link — which Xaman cannot parse as a sign request —
    and still enter the full payment wait, a dead fee screen. It must fail
    the session immediately instead: no wait, nothing on-chain yet."""
    s = _session(state=swap_flow.COMPOSING)

    async def no_payload(destination, **kw):
        return None

    async def must_not_wait(**kw):
        raise AssertionError("wait_for_payment must not be entered without a sign request")

    async def must_not_burn(*a, **kw):
        raise AssertionError("no fee was collected — buy_and_burn must not run")

    monkeypatch.setattr(xumm_ops, "create_payment_payload", no_payload)
    monkeypatch.setattr(swap_flow.xrpl_ops, "wait_for_payment", must_not_wait)
    monkeypatch.setattr(swap_flow.xrpl_ops, "buy_and_burn", must_not_burn)

    assert _run(swap_flow._collect_modify_fee(s, 2)) is False
    assert s.state == swap_flow.FAILED
    assert s.error
    # The fee gate precedes every burn/mint/modify in run_swap_session, so
    # nothing on-chain has happened for this session at this point.


def test_run_swap_session_preserves_fee_gate_failure():
    """#262 source guard (file's established style for hard-to-integration-
    test invariants): the _collect_modify_fee caller must not overwrite the
    fee gate's FAILED + error with the generic PAYMENT_TIMEOUT message."""
    import inspect

    src = inspect.getsource(swap_flow.run_swap_session)
    block = src.split("_collect_modify_fee", 1)[1].split("fee_paid", 1)[0]
    assert "if session.state != FAILED" in block


def test_swap_regenerate_timeout_returns_504(dev_auth, monkeypatch):
    s = _session()
    s.fee_amount = "6"
    s.fee_destination = "rBotWallet"
    s.fee_currency = "XRP"
    s.fee_issuer = None

    async def hang(destination, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(xumm_ops, "create_payment_payload", hang)
    monkeypatch.setattr(server, "SWAP_REGEN_TIMEOUT", 0.01)
    dev_auth[s.id] = s
    resp = _run(server.handle_swap_regenerate(_request("POST", s.id)))
    assert resp.status == 504
