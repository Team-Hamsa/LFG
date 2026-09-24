# tests/test_client_signed_discovery.py
# The client-signed discovery contract (agent-users spec, "Client-signed
# dispatch" -> "Discovery contract").
#
# A session signed in with a client-signed provider ("walletconnect", i.e.
# Joey Wallet, or "agent", a bot holding its own key — agent users spec §1)
# signs its own transactions: when a flow needs the user's
# signature, xumm_ops._create_xumm_payload creates a `sign_requests` row
# instead of a XUMM payload, and the flow's link field carries
# `lfg-wc://<sign_requests.id>`. A bot finds its sign request by stripping
# `lfg-wc://` from that field, then calls GET /api/sign/{id} and
# POST /api/sign/{id}/result. The scheme, the `wc-` id and WHICH field carries
# it are public: one test per flow below pins them.
#
# Every test drives the real handler with a real signed web-session token
# (`platform: "web"`, `provider: <provider>`), not WEBAPP_DEV_MODE: the auth
# decorators then do what production does, `signing_context.use(provider,
# wallet)`, and every task the handler spawns inherits it. A flow whose link is
# produced by a background task (mint delivery, harvest) is driven through that
# task and read back from its status route, so a context that failed to
# propagate would fail the test rather than hide behind a stub.
#
# PROVIDERS is the list of client-signed providers; every test below is
# parametrized over both, so "agent" inherits every flow's discovery contract.
import asyncio
import dataclasses
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from lfg_core import (
    bulk_mint_flow,
    closet_token,
    db_path,
    economy_store,
    image_archive,
    memos,
    mint_flow,
    nft_index,
    supply,
    trait_economy,
    xrpl_ops,
    xumm_ops,
)
from lfg_core.nft_index import OnchainNft
from lfg_core.signing import store
from lfg_service import app
from scripts import _economy_deps
from webapp import economy_api

# "agent" (the agent sign-in provider) inherits every test below: every
# client-signed flow's payload carries platform=agent end to end for it.
PROVIDERS = ["walletconnect", "agent"]

W = "rnmQUgXYCKpFSF6aaUmPf4LGvhKg3RxxNd"  # the session wallet
LINK_SCHEME = "lfg-wc://"

MINTED_NFT = "0008000012345678900000000000000000000000000000000000000000004242"
DELIVERY_OFFER = "D" * 64
BODY = "Straight Blue"


def _run(coro: Any) -> Any:
    """Run on a private loop (asyncio.run would poison the loop policy for
    later suites), letting every task the handler spawned finish on it."""

    async def _and_drain() -> Any:
        result = await coro
        spawned = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if spawned:
            await asyncio.wait_for(asyncio.gather(*spawned), timeout=10)
        return result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_and_drain())
    finally:
        loop.close()


def _json(response: Any) -> Any:
    return json.loads(response.body)


class _Req:
    """The slice of aiohttp's Request the handlers read, carrying a real
    signed web-session token so require_auth/require_wallet run for real."""

    def __init__(self, provider: str, body: Any = None, **match_info: str) -> None:
        token = app.make_session_token(
            {"id": W, "name": "discovery-bot", "platform": "web", "provider": provider}
        )
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = match_info
        self._body = {} if body is None else body
        self._store: dict[str, Any] = {}

    async def json(self) -> Any:
        return self._body

    def __getitem__(self, key: str) -> Any:
        return self._store[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._store[key] = value


def _expected_platform(provider: str, *, economy_accept: bool = False) -> str:
    """The provenance `platform` memo a flow's builder actually stamps.

    Every builder except the two economy accepts labels through
    `memos.platform_for`: `agent` for an agent session, else the surface's own
    label (`webapp` here, since every _Req is `platform: "web"`). The Closet
    claim and harvest delivery accepts label through `memos.backend_platform`
    instead (Task 6): a HUMAN's economy accept keeps today's `backend` label,
    but an agent session still gets `agent` there too — same split, one flag."""
    if provider == "agent":
        return memos.PLATFORM_AGENT
    return memos.PLATFORM_BACKEND if economy_accept else memos.PLATFORM_WEBAPP


def _discover(
    link: Any, env: Any, provider: str, *, tx_type: str, economy_accept: bool = False
) -> dict[str, Any]:
    """What a bot does with a flow's link field, and the contract it relies on:
    (1) the field is exactly `lfg-wc://<id>` with a `wc-` id; (2) the id names a
    pending sign request owned by, and signing as, the session wallet; (3) no
    XUMM payload was created; (4) its txjson memos carry the platform the
    session's provider (and, for the two economy accepts, `backend_platform`)
    actually stamps end to end. Then the bot's next call, GET /api/sign/{id},
    must serve that same transaction."""
    assert isinstance(link, str) and link.startswith(LINK_SCHEME), link
    request_id = link.removeprefix(LINK_SCHEME)
    assert request_id.startswith("wc-"), link
    row = store.get(request_id)
    assert row is not None, f"{link} names no sign_requests row"
    assert row["wallet"] == W
    assert row["state"] == "pending"
    assert row["txjson"]["Account"] == W
    assert row["txjson"]["TransactionType"] == tx_type
    assert env.xumm_calls == []

    # The provenance memos are the typed (initiator/platform/action[/campaign])
    # entries build_memos_json wrote. decode_memos rejects the whole array on
    # any malformed entry.
    raw_memos = row["txjson"]["Memos"]
    decoded = memos.decode_memos(raw_memos)
    assert decoded is not None, f"undecodable provenance Memos: {raw_memos}"
    assert decoded["platform"] == _expected_platform(provider, economy_accept=economy_accept), (
        f"{tx_type} platform memo: {decoded['platform']!r}"
    )

    fetched = _run(app.handle_sign_request(_Req(provider, request_id=request_id)))
    assert fetched.status == 200
    assert _json(fetched)["id"] == request_id
    assert _json(fetched)["txjson"] == row["txjson"]
    return row


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    # A private sign_requests table.
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "sign.db"))
    store.ensure_table()

    # (3) Any XUMM payload create is recorded and refused. It must be recorded:
    # _create_xumm_payload swallows the exception and returns None, so a raise
    # alone would only show up as a flow failure.
    xumm_calls: list[dict[str, Any]] = []

    async def _no_xumm(payload: dict[str, Any]) -> dict[str, Any]:
        xumm_calls.append(payload)
        raise AssertionError("a client-signed session reached the XUMM API")

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", _no_xumm)

    # Real session tokens; a web identity resolves to its own wallet.
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", True)

    async def _resolve_wallet(platform: str, uid: str) -> str | None:
        return uid if platform == "web" else None

    async def _no_push_token(_user: Any) -> None:
        return None

    monkeypatch.setattr(app, "_resolve_wallet", _resolve_wallet)
    monkeypatch.setattr(app, "_push_token", _no_push_token)

    # Fresh in-memory flow registries.
    for registry in ("mint_sessions", "bulk_sessions", "economy_sessions"):
        monkeypatch.setattr(app, registry, {})
    monkeypatch.setattr(app, "brix_trustline_payloads", {})

    # Per-network stores (headroom, index, economy mirror, bulk job records).
    monkeypatch.setattr(db_path, "app_db_path", lambda network=None: str(tmp_path / "app.db"))
    monkeypatch.setattr(nft_index, "index_db_path", lambda network=None: str(tmp_path / "index.db"))
    monkeypatch.setattr(supply, "current_supply", lambda network: 0)
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path / "bulk_jobs"))

    # The terminal firehose publish (and its identity-DB enrichment).
    async def _no_publish(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(app, "publish_event", _no_publish)
    monkeypatch.setattr(app, "enrich_minter_identity", lambda *_a, **_k: {})
    return SimpleNamespace(xumm_calls=xumm_calls)


@pytest.fixture
def funded_wallet(monkeypatch):
    """The session wallet exists, takes NFT offers, and holds enough LFGO that
    mints pay in LFGO (no XRP buy-and-burn leg)."""

    async def _preflight(_wallet: str) -> xrpl_ops.DestinationPreflight:
        return xrpl_ops.DestinationPreflight(
            exists=True, blocks_nft_offers=False, balance_drops=50_000_000, owner_count=1
        )

    async def _lfgo_line(_wallet: str, _currency: str, _issuer: str) -> Any:
        return xrpl_ops.TrustlineState.PRESENT, Decimal("1000000")

    monkeypatch.setattr(xrpl_ops, "destination_preflight", _preflight)
    monkeypatch.setattr(xrpl_ops, "get_trustline_state", _lfgo_line)
    monkeypatch.setattr(app, "_sponsored_recovery_ready", False)  # paid path only


@pytest.fixture
def mint_pipeline(monkeypatch, tmp_path):
    """The single-mint pipeline between "payment confirmed" and "accept
    payload", stubbed at the network edge (compose, CDN, NFTokenMint). The mint
    folds its delivery offer (XLS-52), so the one real builder left is the
    delivery accept."""

    async def _paid(**_kw: Any) -> bool:
        return True

    async def _number() -> int:
        return 4242

    async def _attributes(_store: Any) -> Any:
        return "male", [
            {"trait_type": "Body", "value": BODY},
            {"trait_type": "Head", "value": "Wizard Hat"},
        ]

    async def _compose(*_a: Any, **_k: Any) -> Any:
        return str(tmp_path / "out.png"), False

    async def _upload_output(*_a: Any, **_k: Any) -> Any:
        return "https://cdn.example/4242/4242_0.png", None

    async def _upload(name: str, _data: bytes, _content_type: str) -> str:
        return f"https://cdn.example/{name}"

    async def _mint(**_kw: Any) -> xrpl_ops.MintNFTResult:
        return xrpl_ops.MintNFTResult(nft_id=MINTED_NFT, tx_hash="A" * 64, offer_id=DELIVERY_OFFER)

    monkeypatch.setattr(xrpl_ops, "wait_for_payment", _paid)
    monkeypatch.setattr(xrpl_ops, "mint_nft", _mint)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _number)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", _upload)
    monkeypatch.setattr(mint_flow, "record_nft_mint", lambda **_kw: True)
    monkeypatch.setattr(mint_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", _attributes)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", _compose)
    monkeypatch.setattr(mint_flow.swap_compose, "upload_output", _upload_output)
    monkeypatch.setattr(
        mint_flow,
        "rarity",
        SimpleNamespace(
            connect=lambda: SimpleNamespace(close=lambda: None),
            start_boost_clock=lambda *_a, **_k: None,
            recalculate_rarity=lambda *_a, **_k: None,
            BODY_SENTINEL="",
            BODY_CATEGORY="",
        ),
    )
    monkeypatch.setattr(image_archive, "promote_still", lambda *_a, **_k: None)
    monkeypatch.setattr(image_archive, "discard_still", lambda *_a, **_k: None)
    monkeypatch.setattr(
        image_archive, "pending_still_path", lambda *_a, **_k: str(tmp_path / "still.png")
    )


# --- a. mint payment: POST /api/mint -> payment_link ------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_mint_payment_link_is_a_client_sign_request(env, funded_wallet, monkeypatch, provider):
    async def _parked(_session: Any) -> None:
        return None  # the payment watch is not under test here

    monkeypatch.setattr(app, "_run_mint_session_and_publish", _parked)

    response = _run(app.handle_mint_start(_Req(provider)))

    assert response.status == 200
    body = _json(response)
    assert body["state"] == mint_flow.AWAITING_PAYMENT
    row = _discover(body["payment_link"], env, provider, tx_type="Payment")
    assert row["txjson"]["Destination"] == app.config.TOKEN_ISSUER_ADDRESS


# --- b. mint delivery accept: GET /api/mint/{id} -> accept_deeplink ---------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_mint_delivery_accept_is_a_client_sign_request(env, funded_wallet, mint_pipeline, provider):
    """Start, let the real background session pay and mint, then read the
    accept link back from the status route the client polls."""

    async def _scenario() -> Any:
        started = await app.handle_mint_start(_Req(provider))
        session = app.mint_sessions[_json(started)["id"]]
        await session.task
        status = await app.handle_mint_status(_Req(provider, session_id=session.id))
        return started, status

    started, status = _run(_scenario())

    assert status.status == 200
    body = _json(status)
    assert body["state"] == mint_flow.OFFER_READY
    row = _discover(body["accept_deeplink"], env, provider, tx_type="NFTokenAcceptOffer")
    assert row["txjson"]["NFTokenSellOffer"] == DELIVERY_OFFER
    # The payment and the delivery are two different sign requests.
    assert body["accept_deeplink"] != _json(started)["payment_link"]


# --- c. bulk-mint payment: POST /api/mint/bulk -> payment_link --------------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_bulk_mint_payment_link_is_a_client_sign_request(env, funded_wallet, monkeypatch, provider):
    async def _parked(_job: Any) -> None:
        return None  # fulfillment is not under test here

    monkeypatch.setattr(bulk_mint_flow, "run_bulk_mint_job", _parked)

    response = _run(app.handle_bulk_mint_start(_Req(provider, {"quantity": 2})))

    assert response.status == 200
    body = _json(response)
    assert body["state"] == bulk_mint_flow.AWAITING_PAYMENT
    row = _discover(body["payment_link"], env, provider, tx_type="Payment")
    lfgo = row["txjson"]["Amount"]
    assert Decimal(lfgo["value"]) == 2 * Decimal(app.config.MINT_PRICE_LFGO)


# --- d. bulk unit accept: POST /api/mint/bulk/{id}/units/{i}/accept -> link --


@pytest.mark.parametrize("provider", PROVIDERS)
def test_bulk_unit_accept_link_is_a_client_sign_request(env, provider):
    # A job whose fulfillment (run_bulk_mint_job) has offered unit 0. The
    # accept payload itself is built by the handler, on click.
    job = bulk_mint_flow.BulkMintJob(
        discord_id=W, wallet_address=W, requested_qty=2, platform="web"
    )
    job.units = [bulk_mint_flow.Unit(index=0), bulk_mint_flow.Unit(index=1)]
    job.units[0].state = bulk_mint_flow.OFFERED
    job.units[0].offer_id = DELIVERY_OFFER
    job.state = bulk_mint_flow.FULFILLING
    app.bulk_sessions[job.id] = job

    response = _run(app.handle_bulk_mint_unit_accept(_Req(provider, session_id=job.id, index="0")))

    assert response.status == 200
    row = _discover(_json(response)["link"], env, provider, tx_type="NFTokenAcceptOffer")
    assert row["txjson"]["NFTokenSellOffer"] == DELIVERY_OFFER


# --- e. pending-offer accept: POST /api/pending-offers/accept -> link -------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_pending_offer_accept_link_is_a_client_sign_request(env, monkeypatch, provider):
    gift = {
        "offer_index": DELIVERY_OFFER,
        "nft_id": MINTED_NFT,
        "amount": "0",
        "destination": W,
        "flags": xrpl_ops.LSF_SELL_NFTOKEN,
        "owner": xrpl_ops.bot_wallet_address(),
        "expiration": None,
    }

    async def _offers(_account: str) -> list[dict[str, Any]]:
        return [gift]

    monkeypatch.setattr(xrpl_ops, "get_account_nft_offers", _offers)

    response = _run(
        app.handle_pending_offer_accept(_Req(provider, {"offer_index": DELIVERY_OFFER}))
    )

    assert response.status == 200
    row = _discover(_json(response)["link"], env, provider, tx_type="NFTokenAcceptOffer")
    assert row["txjson"]["NFTokenSellOffer"] == DELIVERY_OFFER


# --- f. POST /api/closet -> accept (while the Closet offer is pending) ------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_closet_accept_is_a_client_sign_request(env, monkeypatch, provider):
    """Both ways a pending Closet reaches the client: the first call mints and
    offers it; a repeat call re-shows the accept for the stored offer. The real
    start_closet and its real economy deps run; only the ledger/CDN edge is
    stubbed."""
    closet_nft = "C" * 64
    closet_offer = "E" * 64

    async def _upload_closet(_meta: dict[str, Any]) -> str:
        return "https://cdn.example/closets/new.json"

    async def _mint(*_a: Any, **_k: Any) -> str:
        return closet_nft

    async def _offer(_nft_id: str, _owner: str, **_k: Any) -> str:
        return closet_offer

    async def _nft_info(_nft_id: str) -> dict[str, Any]:
        return {"owner": app.config.SWAP_ISSUER_ADDRESS}  # offer not accepted yet

    async def _exists(_nft_id: str) -> bool:
        return True

    monkeypatch.setattr(_economy_deps, "_upload_closet", _upload_closet)
    monkeypatch.setattr(xrpl_ops, "mint_nft", _mint)
    monkeypatch.setattr(xrpl_ops, "create_nft_offer", _offer)
    monkeypatch.setattr(xrpl_ops, "nft_info", _nft_info)
    monkeypatch.setattr(xrpl_ops, "nft_exists", _exists)

    first = _run(app.handle_closet(_Req(provider)))
    repeat = _run(app.handle_closet(_Req(provider)))

    for response in (first, repeat):
        assert response.status == 200
        body = _json(response)
        assert body["status"] == closet_token.PENDING_ACCEPT
        row = _discover(
            body["accept"], env, provider, tx_type="NFTokenAcceptOffer", economy_accept=True
        )
        assert row["txjson"]["NFTokenSellOffer"] == closet_offer


# --- g. harvest delivery accept: GET /api/harvest/{id} -> accept ------------


def _seed_legacy_character(nft_id: str, edition: int) -> None:
    """A live, dressed, NON-mutable character owned by W, plus an active
    Closet: the legacy harvest (burn, remint as a mutable blank, offer it back)
    is the only harvest that has a delivery accept."""
    attrs = [{"trait_type": "Body", "value": BODY}]
    attrs += [{"trait_type": s, "value": "None"} for s in trait_economy.NON_BODY_SLOTS]
    conn = economy_api.open_conn()
    try:
        nft_index.upsert(
            conn,
            OnchainNft(
                nft_id=nft_id,
                nft_number=edition,
                owner=W,
                is_burned=False,
                mutable=False,
                uri_hex="AABB",
                body="male",
                attributes=attrs,
                image="",
                ledger_index=1,
            ),
        )
        economy_store.freeze_genesis(
            conn,
            trait_economy.Genesis(
                trait_counts={(s, "None"): 1 for s in trait_economy.NON_BODY_SLOTS},
                edition_bodies={edition: (BODY, "male")},
            ),
            {},
        )
        economy_store.set_closet_token(
            conn, W, "C" * 64, "00", status=closet_token.ACTIVE, offer_id=None
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_harvest_delivery_accept_is_a_client_sign_request(env, monkeypatch, tmp_path, provider):
    """POST /api/harvest starts the real flow as a background task; the client
    (here, the bot) polls GET /api/harvest/{id} until it is terminal. The real
    economy deps are built, then every ledger/CDN op is swapped for a fake,
    except the delivery accept (char_accept_fn), which stays real."""
    legacy_nft = "00080000" + "B" * 56
    reminted_nft = "00080000" + "F" * 56
    _seed_legacy_character(legacy_nft, edition=7)

    async def _burnable(_owner: str, _nft_id: str) -> bool:
        return True

    async def _url(*_a: Any, **_k: Any) -> str:
        return "https://cdn.example/economy.json"

    async def _hash(*_a: Any, **_k: Any) -> str:
        return "B" * 64

    async def _reminted(*_a: Any, **_k: Any) -> str:
        return reminted_nft

    async def _offered(*_a: Any, **_k: Any) -> str:
        return DELIVERY_OFFER

    async def _closet_owner(_nft_id: str) -> str:
        return W

    async def _closet_exists(_nft_id: str) -> bool:
        return True

    real_build = _economy_deps.build_economy_deps

    def _build(conn: Any, user_token: str | None = None, owner: str | None = None) -> Any:
        deps = real_build(conn, user_token=user_token, owner=owner)
        return dataclasses.replace(
            deps,
            closet_upload_fn=_url,
            closet_modify_fn=_hash,
            closet_owner_fn=_closet_owner,
            closet_exists_fn=_closet_exists,
            char_burn_fn=_hash,
            char_mint_fn=_reminted,
            char_offer_fn=_offered,
            blank_meta_fn=_url,
            app_conn_factory=None,
            history_db_path=None,
            records_dir=str(tmp_path / "economy_records"),
        )

    monkeypatch.setattr(_economy_deps, "fetch_burnable", _burnable)
    monkeypatch.setattr(_economy_deps, "build_economy_deps", _build)
    monkeypatch.setattr(image_archive, "drop_archived", lambda *_a, **_k: None)

    # Registered as require_wallet(handle_harvest_start); wrap it the same way.
    start_handler = app.require_wallet(app.handle_harvest_start)

    async def _scenario() -> Any:
        started = await start_handler(_Req(provider, {"nft_id": legacy_nft}))
        assert started.status == 200, _json(started)
        session_id = _json(started)["id"]
        for _ in range(500):
            status = await app.handle_harvest_status(_Req(provider, session_id=session_id))
            if _json(status)["state"] in economy_api.TERMINAL_STATES:
                return status
            await asyncio.sleep(0.01)
        raise AssertionError("the harvest never reached a terminal state")

    status = _run(_scenario())

    assert status.status == 200
    body = _json(status)
    assert body["state"] == "done", body
    assert body["new_nft_id"] == reminted_nft
    row = _discover(
        body["accept"], env, provider, tx_type="NFTokenAcceptOffer", economy_accept=True
    )
    assert row["txjson"]["NFTokenSellOffer"] == DELIVERY_OFFER


# --- h. BRIX trustline: POST /api/brix/trustline -> uuid + xumm_url ---------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_brix_trustline_uuid_and_link_are_a_client_sign_request(env, monkeypatch, provider):
    monkeypatch.setattr(app.config, "BRIX_CURRENCY_HEX", "4252495800000000000000000000000000000000")
    monkeypatch.setattr(app.config, "BRIX_ISSUER", "rrrrrrrrrrrrrrrrrrrrBZbvji")
    monkeypatch.setattr(app.config, "BRIX_TRUSTLINE_LIMIT", "1000000000")

    async def _no_line(_wallet: str, _currency: str, _issuer: str) -> Any:
        return xrpl_ops.TrustlineState.ABSENT, None

    monkeypatch.setattr(xrpl_ops, "get_trustline_state", _no_line)

    response = _run(app.handle_brix_trustline(_Req(provider)))

    assert response.status == 200
    body = _json(response)
    assert body["state"] == "pending"
    # Here the id is served directly as `uuid` too; both must agree.
    assert body["xumm_url"] == LINK_SCHEME + body["uuid"]
    row = _discover(body["xumm_url"], env, provider, tx_type="TrustSet")
    assert row["txjson"]["LimitAmount"]["issuer"] == app.config.BRIX_ISSUER
