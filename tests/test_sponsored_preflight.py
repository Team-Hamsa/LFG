# Destination pre-flight before any spend (#388, #408).
#
# Sponsored eligibility is "this wallet has never submitted a SourceTag-carrying
# transaction", which a NEVER-FUNDED wallet passes as emphatically as possible —
# so the campaign preferentially admitted wallets it could not deliver to. Two
# live mainnet claims proved it: r32XeGY… (unfunded, campaign #1) and rMuseum…
# (lsfDisallowIncomingNFTokenOffer AND below reserve, campaign 2026-08-19). Both
# minted an NFT and burned the sponsorship LFGO for nothing.
#
# The burn cannot be reordered out of harm's way: the debt row is committed
# inside record_minted_and_enqueue_burn and drained by a 1s worker, so it lands
# before the offer is even attempted. Only refusing at admission prevents it.
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
import sqlite3  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from lfg_core import bulk_mint_flow, headroom, mint_flow, supply, xrpl_ops  # noqa: E402
from lfg_service import app as server  # noqa: E402
from surfaces._client.errors import ServiceError  # noqa: E402
from surfaces._shared.mint_result import friendly_error  # noqa: E402
from tests.sponsored_helpers import ready_history  # noqa: E402

BASE = 1_000_000  # 1 XRP account reserve (mainnet, 2026-08-23)
INC = 200_000  # 0.2 XRP per owned object


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- the reserve arithmetic, pure -------------------------------------------


@pytest.mark.parametrize(
    "balance,owner_count,expected",
    [
        # OwnerCount 0: receiving an NFT MUST create its first NFTokenPage, so
        # base + inc is required with no ambiguity at all.
        (0, 0, True),
        (999_892, 0, True),  # the exact #408 repro wallet (rMuseum…)
        (BASE + INC - 1, 0, True),  # one drop short
        (BASE + INC, 0, False),  # exactly enough
        (50_000_000, 0, False),
        # OwnerCount > 0: an existing NFTokenPage may have room, in which case
        # the NFT costs NO extra reserve. account_info cannot tell us which, so
        # we ABSTAIN rather than refuse a holder within 0.2 XRP of their floor.
        (0, 1, False),
        (BASE, 5, False),
        # Unknown inputs never refuse.
        (None, 0, False),
        (0, None, False),
    ],
)
def test_reserve_short_for_first_nft(balance, owner_count, expected):
    assert xrpl_ops.reserve_short_for_first_nft(balance, owner_count, BASE, INC) is expected


def test_reserve_short_defaults_to_the_configured_reserves():
    assert xrpl_ops.reserve_short_for_first_nft(999_892, 0) is True
    assert xrpl_ops.reserve_short_for_first_nft(1_200_000, 0) is False


# --- destination_preflight parsing ------------------------------------------


class _Resp:
    def __init__(self, ok, result):
        self._ok = ok
        self.result = result

    def is_successful(self):
        return self._ok


def _stub_client(monkeypatch, response=None, raises=None):
    class _Client:
        def __init__(self, _url):
            pass

        async def request(self, _req):
            if raises is not None:
                raise raises
            return response

    monkeypatch.setattr(xrpl_ops, "AsyncJsonRpcClient", _Client)


def test_preflight_absent_account_is_definitively_unfunded(monkeypatch):
    _stub_client(monkeypatch, _Resp(False, {"error": "actNotFound"}))
    p = _run(xrpl_ops.destination_preflight("rWhoever"))
    assert p.exists is False
    assert p.resolved is True


def test_preflight_lookup_failure_is_unresolved_not_a_refusal(monkeypatch):
    _stub_client(monkeypatch, raises=RuntimeError("connection reset"))
    p = _run(xrpl_ops.destination_preflight("rWhoever"))
    assert p == xrpl_ops.DestinationPreflight(None, None, None, None)
    assert p.resolved is False
    assert p.reserve_short is False


def test_preflight_other_error_is_unresolved(monkeypatch):
    _stub_client(monkeypatch, _Resp(False, {"error": "actMalformed"}))
    assert _run(xrpl_ops.destination_preflight("rBad")).resolved is False


def test_preflight_reads_decoded_account_flags(monkeypatch):
    _stub_client(
        monkeypatch,
        _Resp(
            True,
            {
                "account_flags": {"disallowIncomingNFTokenOffer": True},
                "account_data": {"Balance": "5000000", "OwnerCount": 3},
            },
        ),
    )
    p = _run(xrpl_ops.destination_preflight("rBlocked"))
    assert (p.exists, p.blocks_nft_offers, p.balance_drops, p.owner_count) == (
        True,
        True,
        5000000,
        3,
    )


def test_preflight_falls_back_to_the_raw_flag_bit(monkeypatch):
    """Older rippled builds omit the decoded account_flags object."""
    _stub_client(
        monkeypatch,
        _Resp(True, {"account_data": {"Flags": 0x04000000, "Balance": "9", "OwnerCount": 0}}),
    )
    p = _run(xrpl_ops.destination_preflight("rOld"))
    assert p.blocks_nft_offers is True
    assert p.reserve_short is True


def test_preflight_unparseable_fields_stay_none(monkeypatch):
    _stub_client(monkeypatch, _Resp(True, {"account_data": {"Balance": "not-a-number"}}))
    p = _run(xrpl_ops.destination_preflight("rWeird"))
    assert p.exists is True
    assert p.balance_drops is None
    assert p.owner_count is None
    assert p.blocks_nft_offers is None
    assert p.reserve_short is False  # unknown never refuses


# --- the handler: nothing is spent on an undeliverable wallet ---------------


class _PostRequest:
    headers: dict[str, str] = {}

    def __init__(self, body=None):
        self._body = body or {}
        self._store = {}

    async def json(self):
        return self._body

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value


@pytest.fixture(autouse=True)
def _service_env(tmp_path, monkeypatch):
    app_db = tmp_path / "app.db"
    history_db = tmp_path / "history-mainnet.db"
    ready_history(str(history_db), network="mainnet")

    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "mainnet")
    monkeypatch.setattr(server, "mint_sessions", {})
    monkeypatch.setattr(server, "_sponsored_recovery_ready", True, raising=False)
    monkeypatch.setattr(server.db_path, "app_db_path", lambda network=None: str(app_db))
    monkeypatch.setattr(server.history_store, "history_db_path", lambda network: str(history_db))
    monkeypatch.setattr(supply, "current_supply", lambda network: 0)
    monkeypatch.setattr(
        headroom.nft_index, "index_db_path", lambda network: str(tmp_path / "idx.db")
    )
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(bulk_mint_flow.config, "DB_PATH", str(tmp_path / "payments.db"))

    async def _no_push(_user):
        return None

    monkeypatch.setattr(server, "_push_token", _no_push)
    return SimpleNamespace(app_db=str(app_db))


def _stub_preflight(monkeypatch, **kwargs):
    fields = {
        "exists": True,
        "blocks_nft_offers": False,
        "balance_drops": 50_000_000,
        "owner_count": 1,
    }
    fields.update(kwargs)

    async def _p(_wallet):
        return xrpl_ops.DestinationPreflight(**fields)

    monkeypatch.setattr(server.xrpl_ops, "destination_preflight", _p)


def _forbid_spending(monkeypatch):
    """Every path that costs the project or the user money must stay untouched."""

    def _reserve(*_a, **_k):
        raise AssertionError("sponsored admission must not run for a refused wallet")

    async def _prepare(_self):
        raise AssertionError("paid preparation must not run for a refused wallet")

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", _reserve)
    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", _prepare)


def _tables_empty(app_db):
    conn = sqlite3.connect(app_db)
    try:
        counts = {}
        for table in ("free_mint_claims", "free_mint_burns"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = 0  # table never created — even better
        return counts
    finally:
        conn.close()


@pytest.mark.parametrize(
    "kwargs,code",
    [
        (
            {
                "exists": False,
                "blocks_nft_offers": None,
                "balance_drops": None,
                "owner_count": None,
            },
            "wallet_unfunded",
        ),
        ({"blocks_nft_offers": True}, "wallet_blocks_nft_offers"),
        ({"balance_drops": 999_892, "owner_count": 0}, "wallet_reserve_short"),
    ],
)
def test_undeliverable_wallet_is_refused_before_anything_is_spent(
    _service_env, monkeypatch, kwargs, code
):
    _stub_preflight(monkeypatch, **kwargs)
    _forbid_spending(monkeypatch)

    response = _run(server.handle_mint_start(_PostRequest()))
    body = json.loads(response.body)

    assert response.status == 409
    assert body["code"] == code
    # The message must name the fix, not just the fault.
    assert len(body["error"]) > 40
    assert _tables_empty(_service_env.app_db) == {"free_mint_claims": 0, "free_mint_burns": 0}
    session = next(iter(server.mint_sessions.values()))
    assert session.state == mint_flow.FAILED
    assert session.error == code
    # The collection slot goes back — a refusal is not a consumed mint.
    assert headroom.reserved_for(_service_env.app_db, f"mint:{session.id}") == 0


def test_unresolved_preflight_declines_sponsorship_but_still_allows_paying(
    _service_env, monkeypatch, caplog
):
    """#408 wants fail-closed; #388 wants no refusal on an RPC blip. Both hold
    if the unresolved case declines the FREE mint (nothing is spent) and falls
    through to the paid path, which is what every other non-admission result
    already does."""
    _stub_preflight(
        monkeypatch, exists=None, blocks_nft_offers=None, balance_drops=None, owner_count=None
    )

    def _reserve(*_a, **_k):
        raise AssertionError("sponsorship must not be granted on an unresolved pre-flight")

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", _reserve)

    async def _prepare(self):
        self.pay_with = "XRP"
        self.pay_amount = "10"
        self.payment_link = "https://xumm.app/sign/paid"
        self.payment_uuid = "paid"

    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", _prepare)
    monkeypatch.setattr(server, "_run_mint_session_and_publish", _parked)

    with caplog.at_level("WARNING"):
        response = _run(server.handle_mint_start(_PostRequest()))
    body = json.loads(response.body)

    assert response.status == 200
    assert body["sponsored"] is False
    assert body["pay_with"] == "XRP"
    assert any("pre-flight unresolved" in r.message for r in caplog.records)
    assert _tables_empty(_service_env.app_db) == {"free_mint_claims": 0, "free_mint_burns": 0}


def test_a_timeout_is_not_a_refusal(_service_env, monkeypatch):
    """A slow account_info must degrade to the paid path, never to a 409 — the
    check exists to prevent loss, not to become a new way to fail a mint."""

    async def _hang(_wallet):
        await asyncio.sleep(30)

    monkeypatch.setattr(server.xrpl_ops, "destination_preflight", _hang)
    monkeypatch.setattr(server, "_PREFLIGHT_TIMEOUT_SECONDS", 0.01)

    async def _prepare(self):
        self.pay_with = "XRP"
        self.pay_amount = "10"
        self.payment_uuid = "paid"
        self.payment_link = "https://xumm.app/sign/paid"

    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", _prepare)
    monkeypatch.setattr(
        server.sponsored_mint, "reserve_if_eligible", lambda *a, **k: _not_admitted()
    )
    monkeypatch.setattr(server, "_run_mint_session_and_publish", _parked)

    response = _run(server.handle_mint_start(_PostRequest()))
    assert response.status == 200


async def _parked(_session):
    """Stand in for the mint pipeline so no task outlives the test's loop."""
    return None


def _not_admitted():
    from lfg_core import sponsored_mint

    return sponsored_mint.ReservationResult(False, "eligibility_unavailable", None)


def test_a_healthy_wallet_is_still_admitted(_service_env, monkeypatch):
    """The guard must not become a new reason sponsorship never happens."""
    from lfg_core import sponsored_mint

    _stub_preflight(monkeypatch)
    launched = []

    monkeypatch.setattr(
        server.sponsored_mint,
        "reserve_if_eligible",
        lambda *a, **k: sponsored_mint.ReservationResult(
            True, "reserved", SimpleNamespace(id="claim-test", status="reserved")
        ),
    )

    async def _wrapper(session):
        launched.append(session.id)

    monkeypatch.setattr(server, "_run_mint_session_and_publish", _wrapper)

    async def scenario():
        response = await server.handle_mint_start(_PostRequest())
        session = next(iter(server.mint_sessions.values()))
        await session.task
        return response, session

    response, session = _run(scenario())
    body = json.loads(response.body)
    assert response.status == 200
    assert body["sponsored"] is True
    assert body["pay_with"] == "SPONSORED"
    assert launched == [session.id]


# --- the refusal actually reaches the user on every surface -----------------


@pytest.mark.parametrize(
    "code", ["wallet_unfunded", "wallet_blocks_nft_offers", "wallet_reserve_short"]
)
def test_surfaces_render_the_refusal_not_the_generic_409(code):
    """Discord and Telegram both go through friendly_error, whose 409 branch
    said "you already have a mint in progress" for EVERY 409 — wrong and
    unactionable for these. The message must survive to the user verbatim."""
    actionable = "Fund your wallet with at least 1.2 XRP, then try again."
    err = ServiceError(actionable, code=code, status=409)
    assert friendly_error(err) == actionable


def test_friendly_error_still_handles_a_real_in_progress_409():
    err = ServiceError("mint already in progress", code=None, status=409)
    assert "already have a mint in progress" in friendly_error(err)


def test_cancel_during_the_preflight_await_leaks_nothing(_service_env, monkeypatch):
    """The pre-flight adds a NEW await inside handle_mint_start's race-sensitive
    admission region, after the headroom grant. handle_mint_start's own comments
    call that window load-bearing (#226/#262), so pin it: a client disconnect
    mid-lookup must release the collection slot and drop the session, not strand
    a reservation that silently shrinks the mintable supply."""
    started = asyncio.Event()

    async def _hang(_wallet):
        started.set()
        await asyncio.Event().wait()  # never resolves

    monkeypatch.setattr(server.xrpl_ops, "destination_preflight", _hang)

    def _reserve(*_a, **_k):
        raise AssertionError("sponsored admission must not run after cancellation")

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", _reserve)

    async def scenario():
        task = asyncio.create_task(server.handle_mint_start(_PostRequest()))
        await started.wait()
        session = next(iter(server.mint_sessions.values()))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return session

    session = _run(scenario())

    assert session.state == mint_flow.CANCELLED
    assert headroom.reserved_for(_service_env.app_db, f"mint:{session.id}") == 0
    assert session.id not in server.mint_sessions
    assert _tables_empty(_service_env.app_db) == {"free_mint_claims": 0, "free_mint_burns": 0}


def test_a_cancel_landing_during_the_preflight_wins_over_a_refusal(_service_env, monkeypatch):
    """Greptile, on this PR: the pre-flight await is a NEW window in which a
    concurrent POST /api/mint/{id}/cancel can flip the session to CANCELLED.
    The handler must report that terminal state, not a wallet refusal for a
    mint nobody is waiting on — and must still return the collection slot."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(_wallet):
        started.set()
        await release.wait()
        # An unfunded wallet: without the re-check this returns 409 wallet_unfunded.
        return xrpl_ops.DestinationPreflight(False, None, None, None)

    monkeypatch.setattr(server.xrpl_ops, "destination_preflight", _slow)

    def _reserve(*_a, **_k):
        raise AssertionError("sponsored admission must not run for a cancelled session")

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", _reserve)

    async def scenario():
        task = asyncio.create_task(server.handle_mint_start(_PostRequest()))
        await started.wait()
        session = next(iter(server.mint_sessions.values()))
        session.state = mint_flow.CANCELLED  # what the cancel endpoint does
        release.set()
        return await task, session

    response, session = _run(scenario())
    body = json.loads(response.body)

    assert response.status == 200
    assert body["state"] == mint_flow.CANCELLED
    assert "code" not in body  # not a wallet refusal
    assert session.state == mint_flow.CANCELLED
    assert headroom.reserved_for(_service_env.app_db, f"mint:{session.id}") == 0
