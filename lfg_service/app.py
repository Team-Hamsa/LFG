# lfg_service/app.py
# Discord Activity backend: aiohttp app serving the embedded-app frontend,
# OAuth token exchange, and the mint/swap/register API.
#
# Run with:  python -m lfg_service.app   (from the repo root)
# (`python -m webapp.server` also works via the webapp/server.py launch shim.)

import asyncio
import base64
import datetime
import functools
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import sqlite3
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast
from urllib.parse import quote as urlquote

import aiohttp
from aiohttp import web
from xrpl.core.addresscodec import is_valid_classic_address

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import (
    closet_token,
    config,
    economy_flow,
    economy_store,
    history_store,
    layer_store,
    leaderboard,
    market_flow,
    market_ops,
    market_store,
    mint_flow,
    nft_index,
    swap_flow,
    swap_meta,
    trait_config,
    xrpl_ops,
    xumm_ops,
)
from lfg_service import identity as identity_store
from lfg_service.auth import require_service_token, surface_for_token
from lfg_service.events import Event, InMemoryEventBus
from lfg_service.telegram_auth import validate_init_data
from user_db import create_users_table, get_user, register_user
from webapp import economy_api, mock_economy, mock_market

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

CLIENT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp", "client"
)
DISCORD_API = "https://discord.com/api"
SESSION_TTL = 6 * 3600

# In-memory sessions: session_id -> MintSession / SwapSession / EconomyWebSession
mint_sessions: dict[str, Any] = {}
swap_sessions: dict[str, Any] = {}
economy_sessions: dict[str, Any] = {}
# Shared by List/Cancel/Buy (market_flow.ListSession/CancelSession/BuySession),
# same "one dict, `.kind` routes the status handler" shape as economy_sessions.
market_sessions: dict[str, Any] = {}
SESSION_RETENTION = 3600  # keep terminal sessions briefly for late polls

BUS = InMemoryEventBus()


def enrich_minter_identity(
    platform: str, platform_user_id: str, wallet: str | None
) -> dict[str, Any]:
    """Build the event identity dict that carries the minter's display handle.

    Consumers (the bot surfaces) run as separate processes and cannot read the
    identity DB, so the handle is resolved here — at publish time, where the DB
    lives — and attached to the event. Returns:
        {platform, platform_user_id, display_handle, linked: [{platform,
         platform_user_id, display_handle}, ...]}
    where display_handle is the minter's own handle (None if unknown) and
    linked is every identity sharing this wallet.

    Resilient by design: a missing wallet or a failing lookup must never break
    publishing, so we fall back to the bare identity. The wallet is matched
    verbatim and NEVER altered (XRPL classic addresses are case-sensitive).
    """
    bare: dict[str, Any] = {
        "platform": platform,
        "platform_user_id": platform_user_id,
        "display_handle": None,
        "linked": [],
    }
    if not wallet:
        return bare
    try:
        rows = identity_store.identities_for_wallet(wallet)
    except Exception as e:
        logging.error(f"enrich_minter_identity lookup failed: {e}")
        return bare
    linked = [
        {
            "platform": r.get("platform"),
            "platform_user_id": r.get("platform_user_id"),
            "display_handle": r.get("display_handle"),
        }
        for r in rows
    ]
    display_handle = next(
        (
            link["display_handle"]
            for link in linked
            if link["platform"] == platform and link["platform_user_id"] == platform_user_id
        ),
        None,
    )
    return {
        "platform": platform,
        "platform_user_id": platform_user_id,
        "display_handle": display_handle,
        "linked": linked,
    }


async def publish_event(
    type_: str,
    identity_obj: Any,
    wallet: str | None,
    data: Any,
) -> None:
    await BUS.publish(
        Event(
            type=type_,
            ts=int(time.time()),
            identity=identity_obj,
            wallet=wallet,
            data=data or {},
        )
    )


async def publish_terminal(
    session: Any,
    prefix: str,
    *,
    wallet: str | None,
    user_id: str,
    platform: str,
    image_url: str | None,
    success_states: set[str],
    fail_states: set[str],
) -> None:
    """Publish a `<prefix>.completed`/`<prefix>.failed` firehose event once a
    session reaches a terminal state. Shared by mint, swap, and the economy ops
    so every in-process NFT interaction announces uniformly.

    - Idempotent: guarded by `session._published` so a polled session can't
      double-publish.
    - Only fires when `session.state in (success_states | fail_states)`.
    - Normalizes the artwork onto a uniform top-level `data["image_url"]` so the
      surface consumers read one field regardless of interaction.
    NOTE: standalone burns are deliberately NOT published here — Discord-admin /
    CLI burns run out-of-process, and swap's internal burns are already covered
    by `swap.*`. (X/Twitter consumer deferred to #41.)
    """
    if getattr(session, "_published", False):
        return
    if session.state not in (success_states | fail_states):
        return
    ok = session.state in success_states
    data = session.to_dict()
    if image_url and not data.get("image_url"):
        data["image_url"] = image_url
    await publish_event(
        f"{prefix}.completed" if ok else f"{prefix}.failed",
        enrich_minter_identity(platform, user_id, wallet),
        wallet,
        data,
    )
    # Mark published only after the await succeeds: if the request task is
    # cancelled mid-publish, the session stays unpublished so a later poll
    # retries. The resulting sub-tick double-publish window under concurrent
    # polls is acceptable.
    session._published = True


async def _ws_stream(request: Any, predicate: Any) -> Any:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async with BUS.subscribe(predicate) as stream:
        nxt = asyncio.ensure_future(stream.__anext__())
        disconnect = asyncio.ensure_future(ws.receive())
        try:
            while True:
                done, _ = await asyncio.wait({nxt, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    break  # client closed/sent a frame -> stop (context exit removes subscriber)
                event = nxt.result()
                if ws.closed:
                    break
                await ws.send_str(json.dumps(event.to_dict()))
                nxt = asyncio.ensure_future(stream.__anext__())
        finally:
            nxt.cancel()
            disconnect.cancel()
    return ws


async def handle_events(request: Any) -> Any:
    token = request.query.get("token") or (
        request.headers.get("Authorization", "").removeprefix("Bearer ")
    )
    if not surface_for_token(token):
        return web.json_response({"error": "unauthorized", "code": "bad_service_token"}, status=401)
    types_param = request.query.get("types")
    allowed: set[str] | None = set(types_param.split(",")) if types_param else None
    return await _ws_stream(request, lambda e: allowed is None or e.type in allowed)


async def handle_events_me(request: Any) -> Any:
    payload = verify_session_token(request.query.get("token", ""))
    if not payload:
        return web.json_response({"error": "unauthorized", "code": "bad_session"}, status=401)
    wallet = await _resolve_wallet(_platform(payload), payload["id"])
    if wallet is None:
        return web.json_response({"error": "no wallet", "code": "no_wallet"}, status=403)
    return await _ws_stream(request, lambda e: e.wallet == wallet)


def _prune_sessions(sessions: dict[str, Any], terminal_states: set[str]) -> None:
    cutoff = time.time() - SESSION_RETENTION
    for sid, s in list(sessions.items()):
        if s.state in terminal_states and s.created_at < cutoff:
            del sessions[sid]


def _active_session(
    sessions: dict[str, Any],
    terminal_states: set[str],
    discord_id: str,
    platform: str | None = None,
):
    for s in sessions.values():
        if s.discord_id == discord_id and s.state not in terminal_states:
            if platform is None or getattr(s, "platform", "discord") == platform:
                return s
    return None


def _session_secret() -> bytes:
    if config.WEBAPP_SESSION_SECRET:
        return config.WEBAPP_SESSION_SECRET.encode()
    # Fall back to a derivation of the XUMM secret so single-process setups
    # work without extra config; set WEBAPP_SESSION_SECRET in production.
    return hashlib.sha256(b"lfg-webapp:" + config.XUMM_API_SECRET.encode()).digest()


def make_session_token(user: dict[str, Any]) -> str:
    payload = {
        "id": user["id"],
        "name": user["name"],
        "platform": user.get("platform", "discord"),
        "exp": int(time.time()) + SESSION_TTL,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(_session_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_session_token(token: str):
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(_session_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body))
        if payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


def _platform(user: dict[str, Any]) -> str:
    return user.get("platform", "discord")


async def _resolve_wallet(platform: str, uid: str) -> str | None:
    wallet = await asyncio.to_thread(identity_store.resolve, platform, uid)
    if wallet is None and platform == "discord":
        record = await asyncio.to_thread(get_user, uid)
        wallet = record["address"] if record else None
    return wallet


async def _push_token(user: dict[str, Any]) -> str | None:
    """The stored XUMM push token for the request's user, or None (issue #135).
    Passed into payload builders so a returning, push-enabled user gets the
    sign request delivered to Xaman instead of a QR. None simply falls back to
    the QR/deep link — never blocks the flow."""
    return await asyncio.to_thread(identity_store.user_token_for, _platform(user), user["id"])


def require_auth(handler):
    async def wrapper(request):
        if config.WEBAPP_DEV_MODE:
            request["user"] = {"id": "dev", "name": "dev"}
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"error": "unauthorized"}, status=401)
        user = verify_session_token(auth[7:])
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)
        request["user"] = user
        return await handler(request)

    return wrapper


def require_wallet(handler):
    """require_auth + a registered wallet; puts the address in request["wallet"]."""

    @require_auth
    async def wrapper(request):
        if config.WEBAPP_DEV_MODE:
            request["wallet"] = mock_economy.DEV_OWNER
            return await handler(request)
        wallet = await _resolve_wallet(_platform(request["user"]), request["user"]["id"])
        if not wallet:
            return web.json_response({"error": "no wallet registered"}, status=400)
        request["wallet"] = wallet
        return await handler(request)

    return wrapper


def _market_disabled_response():
    return web.json_response(
        {"error": "the marketplace is not enabled", "code": "market_disabled"}, status=403
    )


_Handler = TypeVar("_Handler", bound=Callable[..., Awaitable[web.StreamResponse]])


def require_market(handler: _Handler) -> _Handler:
    """Gate an in-app marketplace (#44) route on config.MARKET_ENABLED (checked
    before auth so a disabled deploy exposes nothing of the money-touching
    market surface). Defined here so it precedes every /api/market handler."""

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        if not config.MARKET_ENABLED:
            return _market_disabled_response()
        return await handler(request)

    return cast(_Handler, wrapper)


def make_status_handler(sessions: dict[str, Any]):
    @require_auth
    async def handler(request):
        session = sessions.get(request.match_info["session_id"])
        if (
            not session
            or session.discord_id != request["user"]["id"]
            or getattr(session, "platform", "discord") != _platform(request["user"])
        ):
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(session.to_dict())

    return handler


# --- API handlers ---


async def handle_token(request):
    """Exchange the Embedded App SDK authorize() code for an access token,
    look up the Discord identity, and issue a session token."""
    body = await request.json()
    code = body.get("code")
    if not code:
        return web.json_response({"error": "missing code"}, status=400)

    async with aiohttp.ClientSession() as http:
        resp = await http.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_data = await resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logging.error(f"OAuth exchange failed: {token_data}")
            return web.json_response({"error": "oauth exchange failed"}, status=400)

        me = await http.get(
            f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {access_token}"}
        )
        user = await me.json()
        if me.status != 200 or "id" not in user:
            logging.error(f"Discord /users/@me failed ({me.status}): {user}")
            return web.json_response({"error": "discord identity lookup failed"}, status=502)

    session_token = make_session_token({"id": user["id"], "name": user.get("username", "")})
    return web.json_response(
        {
            "access_token": access_token,  # the SDK needs this for authenticate()
            "session_token": session_token,
            "user": {"id": user["id"], "username": user.get("username", "")},
        }
    )


@require_service_token
async def handle_session(request):
    body = await request.json()
    pid = (body.get("platform_user_id") or "").strip()
    pname = (body.get("platform_username") or "").strip()
    if not pid:
        return web.json_response(
            {"error": "missing platform_user_id", "code": "bad_request"}, status=400
        )
    token = make_session_token({"id": pid, "name": pname, "platform": request["surface"]})
    return web.json_response({"session_token": token, "user": {"id": pid, "username": pname}})


async def handle_telegram_auth(request):
    """Validate a Telegram Mini App `initData` payload and mint a
    platform="telegram" session token (#89).

    Client-callable (unlike /api/session, which needs a service secret): trust
    is established by HMAC-validating Telegram's signed launch payload with the
    bot token. NO wallet creation/lookup — unregistered users just get a valid
    telegram-platform token and the app prompts them to Xaman sign-in inline.

    503 when the service has no bot token configured (feature-off); 401 on a
    bad/stale/forged initData. Never logs `init_data` or the bot token.
    """
    bot_token = config.TELEGRAM_BOT_TOKEN
    if not bot_token:
        return web.json_response(
            {"error": "telegram mini app not configured", "code": "telegram_not_configured"},
            status=503,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    init_data = body.get("init_data") or ""
    if not isinstance(init_data, str):
        init_data = ""
    fields = validate_init_data(init_data, bot_token, config.TELEGRAM_INITDATA_MAX_AGE)
    if not fields:
        return web.json_response({"error": "invalid initData", "code": "bad_initdata"}, status=401)
    user = fields["user"]
    tg_id = str(user["id"])
    handle = user.get("username") or user.get("first_name") or ""
    token = make_session_token({"id": tg_id, "name": handle, "platform": "telegram"})
    return web.json_response({"session_token": token, "user": {"id": tg_id, "username": handle}})


@require_auth
async def handle_me(request):
    user = request["user"]
    wallet = await _resolve_wallet(_platform(user), user["id"])
    # Opportunistic handle refresh (#90): the session token carries the current
    # name, so any authenticated touch keeps display_handle fresh — no crawler.
    # Best-effort: a failure here must never block the /api/me response.
    try:
        await asyncio.to_thread(
            identity_store.touch_handle, _platform(user), user["id"], user["name"]
        )
    except Exception as e:
        logging.warning(f"touch_handle failed for {_platform(user)}:{user['id']}: {e}")
    return web.json_response({"id": user["id"], "username": user["name"], "wallet": wallet})


@require_wallet
async def handle_account(request):
    """The caller's account: their resolved wallet plus every identity linked to
    it. A caller only ever sees their OWN account (keyed by their resolved
    wallet) — there is no public arbitrary wallet -> identities lookup (privacy);
    internal consumers call identity_store.identities_for_wallet in-process."""
    wallet = request["wallet"]
    identities = await asyncio.to_thread(identity_store.identities_for_wallet, wallet)
    return web.json_response({"wallet": wallet, "identities": identities})


_LbKey = tuple[str, str, str, str | None]
_LB_CACHE: dict[_LbKey, tuple[float, dict[str, Any]]] = {}
_LB_CACHE_TTL = 60.0
_LB_CACHE_MAX = 256
_LB_FULL_LIMIT = 500
_LB_PAGE_SIZE = 25
_LB_MIN_START = datetime.date(2013, 1, 1)  # ~XRPL genesis; clamp `start` below this


def _lb_cache_put(key: _LbKey, value: dict[str, Any], now_mono: float) -> None:
    """Insert into the leaderboard cache, dropping expired entries and — if
    still over _LB_CACHE_MAX — evicting the oldest by timestamp (bounds memory
    against arbitrary `start` values fanning out the key space)."""
    for k in [k for k, (ts, _) in _LB_CACHE.items() if now_mono - ts >= _LB_CACHE_TTL]:
        del _LB_CACHE[k]
    _LB_CACHE[key] = (now_mono, value)
    while len(_LB_CACHE) > _LB_CACHE_MAX:
        oldest = min(_LB_CACHE, key=lambda k: _LB_CACHE[k][0])
        del _LB_CACHE[oldest]


def _lb_system_accounts() -> frozenset[str]:
    return frozenset(
        a
        for a in (
            config.SWAP_ISSUER_ADDRESS,
            config.SWAP_OFFER_ISSUER,
            config.BRIX_DISTRIBUTOR_ADDRESS,
            config.BRIX_AMM_ACCOUNT,
        )
        if a
    )


def _lb_display_name(wallet: str) -> str:
    handle = identity_store.handle_for_wallet(wallet)
    if handle:
        return handle
    return wallet[:6] + "…" + wallet[-4:] if len(wallet) > 10 else wallet


async def handle_leaderboard(request):
    """Public leaderboard: GET /api/leaderboard?board=&period=&start=&me=.

    Full (up to rank 500) results are cached for 60s keyed on
    (network, board, period, start); `me` is computed post-cache by scanning
    the cached full row set so it never invalidates the cache."""
    board = request.query.get("board", "")
    period = request.query.get("period", "all")
    start = request.query.get("start") or None
    me = request.query.get("me") or None

    if board not in leaderboard.BOARDS:
        return web.json_response({"error": f"unknown board: {board!r}"}, status=400)

    if start is not None:
        try:
            start_date = datetime.date.fromisoformat(start)
        except ValueError:
            return web.json_response({"error": f"bad start date: {start!r}"}, status=400)
        today_utc = datetime.datetime.now(datetime.timezone.utc).date()
        if start_date < _LB_MIN_START or start_date > today_utc:
            return web.json_response({"error": f"start out of range: {start!r}"}, status=400)

    network = config.XRPL_NETWORK
    cache_key = (network, board, period, start)
    now_mono = time.monotonic()
    cached = _LB_CACHE.get(cache_key)

    if cached is not None and now_mono - cached[0] < _LB_CACHE_TTL:
        full_rows = cached[1]["rows"]
        start_ts = cached[1]["start_ts"]
        end_ts = cached[1]["end_ts"]
    else:
        try:
            start_ts, end_ts = leaderboard.period_bounds(period, start, now=int(time.time()))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        system_accounts = _lb_system_accounts()

        def _compute_sync():
            # Runs on an executor thread so sqlite work never stalls the event
            # loop. Python's sqlite3 here is not serialized (threadsafety=1),
            # so open short-lived connections IN this thread rather than
            # sharing the loop-thread conns across threads.
            sqlite3 = __import__("sqlite3")
            hconn = history_store.init_history_db(history_store.history_db_path(network))
            oconn = nft_index.init_db(nft_index.index_db_path(network))
            oconn.row_factory = sqlite3.Row
            try:
                computed_rows = leaderboard.compute(
                    board,
                    hconn,
                    oconn,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    network=network,
                    system_accounts=system_accounts,
                    limit=_LB_FULL_LIMIT,
                )
                nft_ids = [r["nft_id"] for r in computed_rows if r.get("nft_id")]
                images: dict[str, str | None] = {}
                if nft_ids:
                    placeholders = ",".join("?" * len(nft_ids))
                    cur = oconn.execute(
                        f"SELECT nft_id, image FROM onchain_nfts WHERE nft_id IN ({placeholders})",
                        nft_ids,
                    )
                    images = {r["nft_id"]: r["image"] for r in cur.fetchall()}
                for r in computed_rows:
                    nft_id = r.get("nft_id")
                    r["image"] = images.get(nft_id) if nft_id else None
                return computed_rows
            finally:
                hconn.close()
                oconn.close()

        try:
            full_rows = await asyncio.get_event_loop().run_in_executor(None, _compute_sync)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        _lb_cache_put(
            cache_key,
            {"rows": full_rows, "start_ts": start_ts, "end_ts": end_ts},
            now_mono,
        )

    page_rows = full_rows[:_LB_PAGE_SIZE]

    rows = []
    for i, r in enumerate(page_rows):
        wallet = r.get("wallet")
        rows.append(
            {
                "rank": i + 1,
                "wallet": wallet,
                "display_name": _lb_display_name(wallet) if wallet else None,
                "nft_id": r.get("nft_id"),
                "nft_number": r.get("nft_number"),
                "image": r.get("image"),
                "value": r["value"],
            }
        )

    me_block = None
    if me is not None:
        for i, r in enumerate(full_rows):
            if r.get("wallet") == me:
                me_block = {"rank": i + 1, "value": r["value"]}
                break

    return web.json_response(
        {
            "board": board,
            "period": period,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "rows": rows,
            "me": me_block,
        }
    )


# --- In-app marketplace (#44): browse + mine + history ---


def _use_market_mock() -> bool:
    """Whether the market handlers below should serve webapp.mock_market's
    in-memory fixture instead of touching sqlite/XRPL/XUMM. Defaults to
    config.WEBAPP_DEV_MODE (the real dev-mode mock harness, Task 10) but is
    its OWN indirection — not a direct `config.WEBAPP_DEV_MODE` check —
    because several Task 7-9 tests (tests/test_market_api.py,
    tests/test_market_trait_flow.py) already set WEBAPP_DEV_MODE=True purely
    to get require_wallet's dev-mode wallet-injection convenience (mirrors
    tests/test_swap_cross_body_api.py's identical trick) while exercising
    these handlers' REAL sqlite/XUMM-mocked logic against a seeded onchain_env
    fixture. Those tests monkeypatch this function directly to keep that
    convenience without also getting the mock substitution; ordinary dev-mode
    usage (`WEBAPP_DEV_MODE=1` env var, no monkeypatch) is unaffected."""
    return config.WEBAPP_DEV_MODE


_MarketKey = tuple[str, str]  # (network, kind)
_MARKET_CACHE: dict[_MarketKey, tuple[float, list[dict[str, Any]]]] = {}
_MARKET_CACHE_TTL = 60.0
# Cardinality is bounded by construction (network x kind only — filters never
# key the cache, see the module docstring on Task 7's spec excerpt), so this
# ceiling is generous headroom, not a load-bearing eviction path.
_MARKET_CACHE_MAX = 16
# "Unfiltered" cache population cap: effectively unbounded for the realistic
# live-listing volume (a few hundred to low thousands per market_store.browse's
# own docstring) while still bounding a single query's result set.
_MARKET_ROW_CAP = 50_000

_MARKET_MAX_LIMIT = 100
_MARKET_DEFAULT_LIMIT = 24
_MARKET_MAX_OFFSET = 100_000


def _market_cache_put(key: _MarketKey, value: list[dict[str, Any]], now_mono: float) -> None:
    """Insert into the browse cache, dropping expired entries and — if still
    over _MARKET_CACHE_MAX — evicting the oldest by timestamp. Mirrors
    _lb_cache_put's shape (leaderboard cache) for the same reasons."""
    for k in [k for k, (ts, _) in _MARKET_CACHE.items() if now_mono - ts >= _MARKET_CACHE_TTL]:
        del _MARKET_CACHE[k]
    _MARKET_CACHE[key] = (now_mono, value)
    while len(_MARKET_CACHE) > _MARKET_CACHE_MAX:
        oldest = min(_MARKET_CACHE, key=lambda k: _MARKET_CACHE[k][0])
        del _MARKET_CACHE[oldest]


def _attach_character_images(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Mutate `rows` in place, adding an `image` key sourced from
    onchain_nfts.image (market_store.browse's character join carries
    nft_number/attributes_json but not image — same 2-query pattern
    handle_leaderboard uses for the same column)."""
    nft_ids = [r["nft_id"] for r in rows]
    if not nft_ids:
        return
    placeholders = ",".join("?" * len(nft_ids))
    cur = conn.execute(
        f"SELECT nft_id, image FROM onchain_nfts WHERE nft_id IN ({placeholders})", nft_ids
    )
    images = {r["nft_id"]: r["image"] for r in cur.fetchall()}
    for r in rows:
        r["image"] = images.get(r["nft_id"]) or None


def _compute_market_rows(network: str, kind: str) -> list[dict[str, Any]]:
    """The canonical UNFILTERED live join for one (network, kind) — cached
    for _MARKET_CACHE_TTL. Runs on an executor thread (sqlite3 threadsafety=1,
    so this opens its own short-lived connection rather than sharing a
    loop-thread conn across threads, same as handle_leaderboard's _compute_sync)."""
    conn = nft_index.init_db(nft_index.index_db_path(network))
    conn.row_factory = sqlite3.Row
    try:
        market_store.init_db(conn)
        economy_store.init_economy_schema(conn)
        rows = market_store.browse(conn, kind=kind, limit=_MARKET_ROW_CAP, offset=0)
        if kind == "character":
            _attach_character_images(conn, rows)
        return rows
    finally:
        conn.close()


def _market_network(kind: str) -> str:
    """Which per-network onchain db a marketplace read of `kind` lives in.

    The two config knobs can legitimately differ (deployed topology: the app
    runs XRPL_NETWORK=mainnet while the trait economy stays testnet-gated at
    ECONOMY_NETWORK=testnet). Everything trait-economy-backed — trait listings
    (their listener writes the economy db), the trait_tokens ownership join,
    loose Closet assets, sold-trait history — resolves via ECONOMY_NETWORK,
    exactly like webapp/economy_api.py::open_conn; everything character-backed
    (onchain_nfts, character listings, nft_events) stays on XRPL_NETWORK.
    Do NOT "simplify" this back to a single network: with the split topology
    a trait read against XRPL_NETWORK silently returns empty for every user."""
    return config.ECONOMY_NETWORK if kind == "trait" else config.XRPL_NETWORK


def _trait_image_url(cfg: trait_config.TraitConfig, slot: str, value: str) -> str:
    """A same-origin /api/layer URL for a trait value, picking a representative
    body: the first body allowed by trait_config's affinity engine, or the
    shared/ dir for a universal (unrestricted) value — mirrors
    scripts/_economy_deps.py's _compose_trait ("first body that has it")
    without the network/download cost, since affinity already tells us which
    bodies are legal without touching the layer store."""
    allowed = cfg.allowed_bodies(slot, value)
    body = sorted(allowed)[0] if allowed else layer_store.SHARED_DIR
    return (
        f"/api/layer?body={urlquote(body, safe='')}"
        f"&trait={urlquote(slot, safe='')}&value={urlquote(value, safe='')}"
    )


def _serialize_listing_row(
    r: dict[str, Any], kind: str, cfg: trait_config.TraitConfig
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "nft_id": r["nft_id"],
        "kind": kind,
        "image": r.get("image"),
        "amount_drops": r["amount_drops"],
        "amount_xrp": market_ops.drops_to_xrp_str(str(r["amount_drops"])),
        "seller": r["seller"],
        "offer_index": r["offer_index"],
    }
    if kind == "character":
        out["nft_number"] = r.get("nft_number")
        raw_attrs = r.get("attributes_json")
        out["attributes"] = json.loads(raw_attrs) if raw_attrs else []
    else:
        out["slot"] = r.get("slot")
        out["value"] = r.get("value")
        out["image"] = _trait_image_url(cfg, str(r.get("slot")), str(r.get("value")))
    return out


def _parse_market_int_param(
    request: web.Request, name: str, default: int, *, max_value: int
) -> int | None:
    """Parse a non-negative, bounded int query param. Returns None (the
    caller 400s) for a missing-required/non-integer/out-of-range value; a
    param that is simply absent uses `default`."""
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0 or value > max_value:
        return None
    return value


@require_market
async def handle_market_listings(request: web.Request) -> web.Response:
    """Public: GET /api/market/listings?kind=&trait=&min_xrp=&max_xrp=&sort=&limit=&offset=.

    Cache holds only the canonical unfiltered live join per (network, kind),
    TTL 60s; trait/price filter + sort + pagination run in-process on the
    cached rows post-cache-lookup, so user-controlled filter params never
    key (and can never fan out) the cache."""
    kind = request.query.get("kind", "character")
    if kind not in market_store._VALID_KINDS:
        return web.json_response({"error": f"unknown kind: {kind!r}"}, status=400)

    # Trait listings are only transactable when the trait economy is enabled on
    # this chain (ECONOMY_NETWORK == XRPL_NETWORK). With it off, surfacing trait
    # rows on this public browse would advertise listings no one can actually
    # buy (buy would 403 economy_disabled), so serve an empty page instead of a
    # hard 403 — the character surface is unaffected. See CLAUDE.md's seam note.
    if kind == "trait" and not config.ECONOMY_ENABLED:
        return web.json_response({"rows": [], "total": 0})

    sort = request.query.get("sort", "price_asc")
    if sort not in market_store._VALID_SORTS:
        return web.json_response({"error": f"unknown sort: {sort!r}"}, status=400)

    trait_filters: dict[str, list[str]] = {}
    for raw in request.query.getall("trait", []):
        slot, sep, value = raw.partition(":")
        if not sep or not slot or not value:
            return web.json_response({"error": f"bad trait filter: {raw!r}"}, status=400)
        trait_filters.setdefault(slot, []).append(value)

    min_drops: int | None = None
    max_drops: int | None = None
    min_xrp = request.query.get("min_xrp")
    max_xrp = request.query.get("max_xrp")
    try:
        if min_xrp is not None:
            min_drops = int(market_ops.xrp_to_drops_str(min_xrp))
        if max_xrp is not None:
            max_drops = int(market_ops.xrp_to_drops_str(max_xrp))
    except Exception as e:
        # Broad on purpose (same guard as handle_market_list_start):
        # xrp_to_drops_str raises TypeError/ValueError for its documented
        # cases, but Decimal("Infinity")/("nan") slip past its <= 0 guard and
        # raise OverflowError/decimal.InvalidOperation instead — on this
        # public unauthenticated endpoint those were an uncaught 500.
        return web.json_response({"error": f"bad XRP amount: {e}"}, status=400)

    limit = _parse_market_int_param(
        request, "limit", _MARKET_DEFAULT_LIMIT, max_value=_MARKET_MAX_LIMIT
    )
    if limit is None:
        return web.json_response({"error": "bad limit"}, status=400)
    offset = _parse_market_int_param(request, "offset", 0, max_value=_MARKET_MAX_OFFSET)
    if offset is None:
        return web.json_response({"error": "bad offset"}, status=400)

    if _use_market_mock():
        rows = mock_market.INSTANCE.browse(
            kind=kind,
            trait_filters=trait_filters,
            min_drops=min_drops,
            max_drops=max_drops,
            sort=sort,
        )
        page = rows[offset : offset + limit]
        return web.json_response({"rows": page, "total": len(rows)})

    # Per-kind network resolution (see _market_network): the cache key carries
    # the resolved network, so a testnet-trait entry and a mainnet-character
    # entry coexist; cardinality stays <= networks x kinds by construction.
    network = _market_network(kind)
    cache_key: _MarketKey = (network, kind)
    now_mono = time.monotonic()
    cached = _MARKET_CACHE.get(cache_key)
    if cached is not None and now_mono - cached[0] < _MARKET_CACHE_TTL:
        rows = cached[1]
    else:
        rows = await asyncio.get_event_loop().run_in_executor(
            None, _compute_market_rows, network, kind
        )
        _market_cache_put(cache_key, rows, now_mono)

    filtered = rows
    if min_drops is not None:
        filtered = [r for r in filtered if r["amount_drops"] >= min_drops]
    if max_drops is not None:
        filtered = [r for r in filtered if r["amount_drops"] <= max_drops]
    if trait_filters:
        # market_store._row_attrs is typed against sqlite3.Row (the shape it
        # sees internally, pre-dict-conversion, inside browse()); our cached
        # rows are already plain dicts (browse()'s own return type), which
        # support the same __getitem__ access _row_attrs relies on.
        filtered = [
            r
            for r in filtered
            if market_store._attributes_match(
                market_store._row_attrs(cast(sqlite3.Row, r), kind), trait_filters
            )
        ]

    if sort == "price_asc":
        filtered = sorted(filtered, key=lambda r: (r["amount_drops"], r["offer_index"]))
    elif sort == "price_desc":
        filtered = sorted(filtered, key=lambda r: (-r["amount_drops"], r["offer_index"]))
    else:  # newest
        filtered = sorted(filtered, key=lambda r: (-(r["created_ts"] or 0), r["offer_index"]))

    page = filtered[offset : offset + limit]
    cfg = trait_config.get_config()
    return web.json_response(
        {"rows": [_serialize_listing_row(r, kind, cfg) for r in page], "total": len(filtered)}
    )


def _compute_mine_data(char_network: str, econ_network: str, wallet: str) -> dict[str, Any]:
    """Sync sqlite work for GET /api/market/mine, run on an executor thread
    (same posture as _compute_market_rows). Two per-kind connections (see
    _market_network — the networks can differ in the deployed topology):
    character listings + unlisted live characters come from the XRPL-network
    db; trait listings + unlisted wallet trait tokens + loose Closet assets
    come from the economy-network db. When the knobs match, both connections
    simply open the same file."""
    # --- character-backed groups (XRPL network) ---
    conn = nft_index.init_db(nft_index.index_db_path(char_network))
    conn.row_factory = sqlite3.Row
    try:
        market_store.init_db(conn)
        cur = conn.execute(
            "SELECT * FROM market_listings WHERE seller = ? AND is_live = 1 AND kind = 'character'",
            (wallet,),
        )
        char_listing_rows = [dict(row) for row in cur.fetchall()]
        _attach_character_images(conn, char_listing_rows)
        listed_char_ids = {r["nft_id"] for r in char_listing_rows}

        unlisted_characters = [
            {
                "nft_id": c.nft_id,
                "nft_number": c.nft_number,
                "image": c.image or None,
                "attributes": c.attributes,
            }
            for c in nft_index.owner_live_nfts(conn, wallet)
            if c.nft_id not in listed_char_ids
        ]
    finally:
        conn.close()

    # --- trait-economy-backed groups (economy network) ---
    conn = nft_index.init_db(nft_index.index_db_path(econ_network))
    conn.row_factory = sqlite3.Row
    try:
        market_store.init_db(conn)
        economy_store.init_economy_schema(conn)
        cur = conn.execute(
            "SELECT * FROM market_listings WHERE seller = ? AND is_live = 1 AND kind = 'trait'",
            (wallet,),
        )
        trait_listing_rows = [dict(row) for row in cur.fetchall()]
        listed_trait_ids = {r["nft_id"] for r in trait_listing_rows}

        unlisted_trait_tokens = [
            {"nft_id": nid, "slot": s, "value": v}
            for nid, o, s, v in economy_store.read_trait_tokens(conn)
            if o == wallet and nid not in listed_trait_ids
        ]

        closet_assets = [
            {"slot": s, "value": v, "count": c}
            for (o, s, v, c) in economy_store.read_closet_assets(conn)
            if o == wallet and c > 0
        ]
    finally:
        conn.close()

    return {
        "listings": char_listing_rows + trait_listing_rows,
        "unlisted_characters": unlisted_characters,
        "unlisted_trait_tokens": unlisted_trait_tokens,
        "closet_assets": closet_assets,
    }


@require_market
@require_wallet
async def handle_market_mine(request):  # untyped: matches require_wallet's other handlers
    # (e.g. handle_account, handle_economy) — an annotated signature under an
    # untyped decorator trips mypy's untyped-decorator check.
    """The caller's marketplace surface: their own live listings (both kinds)
    + unlisted live characters + unlisted wallet trait tokens + loose Closet
    traits, grouped so the UI can offer list/cancel/sell-from-closet per item."""
    wallet = request["wallet"]
    if _use_market_mock():
        return web.json_response(mock_market.INSTANCE.mine(wallet))
    data = await asyncio.get_event_loop().run_in_executor(
        None,
        _compute_mine_data,
        _market_network("character"),
        _market_network("trait"),
        wallet,
    )
    cfg = trait_config.get_config()
    listings = [_serialize_listing_row(r, r["kind"], cfg) for r in data["listings"]]
    # With the trait economy off, trait ops are unavailable on this chain, so
    # drop trait content from the caller's surface (character listings + their
    # unlisted characters are unaffected). See CLAUDE.md's marketplace seam note.
    if not config.ECONOMY_ENABLED:
        listings = [row for row in listings if row.get("kind") != "trait"]
        return web.json_response(
            {
                "listings": listings,
                "unlisted_characters": data["unlisted_characters"],
                "unlisted_trait_tokens": [],
                "closet_assets": [],
            }
        )
    return web.json_response(
        {
            "listings": listings,
            "unlisted_characters": data["unlisted_characters"],
            "unlisted_trait_tokens": data["unlisted_trait_tokens"],
            "closet_assets": data["closet_assets"],
        }
    )


_MARKET_HISTORY_EVENTS = ("sale", "offer_create", "offer_cancel")


def _compute_nft_history(network: str, nft_id: str) -> list[dict[str, Any]]:
    conn = history_store.init_history_db(history_store.history_db_path(network))
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(_MARKET_HISTORY_EVENTS))
        cur = conn.execute(
            f"SELECT * FROM nft_events WHERE nft_id = ? AND event IN ({placeholders}) "
            "ORDER BY ledger_index DESC LIMIT 50",
            (nft_id, *_MARKET_HISTORY_EVENTS),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _compute_trait_sales(network: str, slot: str, value: str) -> list[dict[str, Any]]:
    conn = nft_index.init_db(nft_index.index_db_path(network))
    conn.row_factory = sqlite3.Row
    try:
        market_store.init_db(conn)
        cur = conn.execute(
            "SELECT * FROM market_listings WHERE kind = 'trait' AND slot = ? AND value = ? "
            "AND closed_reason = 'sold' ORDER BY created_ts DESC",
            (slot, value),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


@require_market
async def handle_market_history(request: web.Request) -> web.Response:
    """Public: GET /api/market/history?nft_id=… (character sale/offer history)
    or ?slot=&value=… (sold trait listings — per-nft_id history is near-useless
    for traits since each listing is a fresh token). Neither param -> 400."""
    nft_id = request.query.get("nft_id")
    slot = request.query.get("slot")
    value = request.query.get("value")

    if _use_market_mock():
        if nft_id:
            return web.json_response(mock_market.INSTANCE.history(nft_id=nft_id))
        if slot and value:
            return web.json_response(mock_market.INSTANCE.history(slot=slot, value=value))
        return web.json_response({"error": "nft_id or slot+value required"}, status=400)

    if nft_id:
        # nft_events live in history_<XRPL_NETWORK>.db (character-backed).
        events = await asyncio.get_event_loop().run_in_executor(
            None, _compute_nft_history, config.XRPL_NETWORK, nft_id
        )
        return web.json_response({"nft_id": nft_id, "events": events})

    if slot and value:
        # Sold trait listings are trait-economy-backed; with the economy off on
        # this chain there is nothing meaningful to serve (see CLAUDE.md's seam
        # note) — return an empty sales list rather than reading the wrong net.
        if not config.ECONOMY_ENABLED:
            return web.json_response({"slot": slot, "value": value, "sales": []})
        # Sold trait listings are trait-economy-backed -> economy network
        # (see _market_network).
        rows = await asyncio.get_event_loop().run_in_executor(
            None, _compute_trait_sales, _market_network("trait"), slot, value
        )
        sales = [
            {
                "nft_id": r["nft_id"],
                "seller": r["seller"],
                "amount_drops": r["amount_drops"],
                "amount_xrp": market_ops.drops_to_xrp_str(str(r["amount_drops"])),
                "offer_index": r["offer_index"],
            }
            for r in rows
        ]
        return web.json_response({"slot": slot, "value": value, "sales": sales})

    return web.json_response({"error": "nft_id or slot+value required"}, status=400)


# --- In-app marketplace (#44) Task 8: list / cancel / buy sessions ---
# Each POST builds one XUMM sign request (NFTokenCreateOffer / CancelOffer /
# AcceptOffer) via lfg_core.xumm_ops and returns immediately — the state
# machine (including List/Buy's tx-fetch finalize step) lives in
# lfg_core.market_flow and is driven by the GET status handlers below on
# every poll. See market_flow's module docstring for the full design.


def _resolve_ownable(char_network: str, econ_network: str, nft_id: str) -> dict[str, Any] | None:
    """Resolve nft_id's marketplace kind + current owner (+ trait slot/value)
    by checking on-chain membership: onchain_nfts (character net) first, then
    trait_tokens (economy net). None if nft_id is unknown to both (never
    minted, or a burned/deposited character)."""
    conn = nft_index.init_db(nft_index.index_db_path(char_network))
    try:
        row = conn.execute(
            "SELECT owner, is_burned FROM onchain_nfts WHERE nft_id = ?", (nft_id,)
        ).fetchone()
        if row is not None and not row[1]:
            return {"kind": "character", "owner": row[0], "slot": None, "value": None}
    finally:
        conn.close()
    conn = nft_index.init_db(nft_index.index_db_path(econ_network))
    try:
        economy_store.init_economy_schema(conn)
        row = conn.execute(
            "SELECT owner, slot, value FROM trait_tokens WHERE nft_id = ?", (nft_id,)
        ).fetchone()
        if row is not None:
            return {"kind": "trait", "owner": row[0], "slot": row[1], "value": row[2]}
    finally:
        conn.close()
    return None


def _has_live_listing(network: str, nft_id: str) -> bool:
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        market_store.init_db(conn)
        return market_store.live_listing_for_nft(conn, nft_id) is not None
    finally:
        conn.close()


def _find_listing_any_network(offer_index: str) -> tuple[str, dict[str, Any]] | None:
    """A listing row by offer_index, live or not, searched across every
    distinct network a marketplace kind can live in (see _market_network) —
    the caller doesn't yet know which kind offer_index belongs to."""
    for network in {_market_network("character"), _market_network("trait")}:
        conn = nft_index.init_db(nft_index.index_db_path(network))
        try:
            market_store.init_db(conn)
            row = market_store.get_listing(conn, offer_index)
            if row is not None:
                return network, row
        finally:
            conn.close()
    return None


def _closet_active(network: str, wallet: str) -> bool:
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        economy_store.init_economy_schema(conn)
        rec = economy_store.get_closet_record(conn, wallet)
        return rec is not None and rec[2] == closet_token.ACTIVE
    finally:
        conn.close()


def _write_listing_row(network: str, row: dict[str, Any]) -> None:
    # Creation-only write (record_listing_creation, NOT upsert_listing): the
    # finalize poll carries stale creation-time data and can land after the
    # listener already closed the row (sold/settled=0). A full overwrite would
    # resurrect a sold listing and break the settlement sweep predicate — see
    # market_store.record_listing_creation's docstring.
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        market_store.init_db(conn)
        market_store.record_listing_creation(conn, market_store.MarketListing(**row))
    finally:
        conn.close()


def _close_listing_sync(
    network: str, offer_index: str, reason: str, buyer: str | None = None
) -> None:
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        market_store.init_db(conn)
        market_store.close_listing(conn, offer_index, reason, buyer=buyer)
    finally:
        conn.close()


@require_market
@require_wallet
async def handle_market_list_start(request):
    """POST /api/market/list {nft_id, price_xrp}: 409 if the caller doesn't
    own nft_id (checked across onchain_nfts + trait_tokens) or a live listing
    already exists for it; otherwise builds the NFTokenCreateOffer XUMM
    payload and returns a session (mirrors mint/swap's QR/deeplink shape)."""
    user = request["user"]
    wallet = request["wallet"]
    body = await request.json()
    nft_id = body.get("nft_id")
    price_xrp = body.get("price_xrp")
    if not nft_id or not isinstance(price_xrp, str):
        return web.json_response(
            {"error": "nft_id and price_xrp (string) are required"}, status=400
        )
    try:
        amount_drops = int(market_ops.xrp_to_drops_str(price_xrp))
    except Exception as e:
        # Broad on purpose: xrp_to_drops_str raises TypeError/ValueError for
        # the documented cases, but Decimal("Infinity")/("nan") slip past its
        # `<= 0` guard and raise decimal.InvalidOperation/OverflowError
        # instead — this edge (where a user-controlled price_xrp is parsed)
        # must 400 cleanly on all of them, not just the two it advertises.
        return web.json_response({"error": f"bad price_xrp: {e}"}, status=400)

    if _use_market_mock():
        try:
            return web.json_response(mock_market.INSTANCE.start_list(wallet, nft_id, amount_drops))
        except mock_market.MockMarketError as e:
            return web.json_response({"error": str(e)}, status=409)

    loop = asyncio.get_event_loop()
    membership = await loop.run_in_executor(
        None,
        _resolve_ownable,
        _market_network("character"),
        _market_network("trait"),
        nft_id,
    )
    if membership is None or membership["owner"] != wallet:
        return web.json_response({"error": "you do not own that NFT"}, status=409)

    # Trait ON-LEDGER ops assume ECONOMY_NETWORK == XRPL_NETWORK; gate trait
    # listing on ECONOMY_ENABLED (same as the trait wizard/sweep). Characters
    # are unaffected. See CLAUDE.md's marketplace seam note.
    if membership["kind"] == "trait" and not config.ECONOMY_ENABLED:
        return _economy_disabled_response()

    network = _market_network(membership["kind"])
    already_listed = await loop.run_in_executor(None, _has_live_listing, network, nft_id)
    if already_listed:
        return web.json_response({"error": "that NFT is already listed"}, status=409)

    _prune_sessions(market_sessions, market_flow.TERMINAL_STATES)
    active = _active_session(
        market_sessions, market_flow.TERMINAL_STATES, user["id"], _platform(user)
    )
    if active:
        return web.json_response(
            {"error": "a market action is already in progress", "session": active.to_dict()},
            status=409,
        )

    payload = await xumm_ops.create_sell_offer_payload(
        wallet,
        nft_id,
        str(amount_drops),
        return_url=xumm_ops.discord_return_url(body.get("guild_id"), body.get("channel_id")),
        user_token=await _push_token(user),
    )
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)

    session = market_flow.ListSession(
        discord_id=user["id"],
        wallet_address=wallet,
        nft_id=nft_id,
        listing_kind=membership["kind"],
        amount_drops=amount_drops,
        slot=membership["slot"],
        value=membership["value"],
        platform=_platform(user),
    )
    session.qr_url = payload["qr_url"]
    session.xumm_url = payload["xumm_url"]
    session.payload_uuid = payload.get("uuid")
    market_sessions[session.id] = session
    return web.json_response(session.to_dict())


@require_market
@require_wallet
async def handle_market_cancel_start(request):
    """POST /api/market/cancel {offer_index}: 404 if there's no live listing
    at that offer_index, 403 if the caller isn't its seller; otherwise builds
    the NFTokenCancelOffer XUMM payload."""
    user = request["user"]
    wallet = request["wallet"]
    body = await request.json()
    offer_index = body.get("offer_index")
    if not offer_index:
        return web.json_response({"error": "offer_index is required"}, status=400)

    if _use_market_mock():
        try:
            return web.json_response(mock_market.INSTANCE.start_cancel(wallet, offer_index))
        except mock_market.MockMarketError as e:
            status = {"not found": 404, "not your listing": 403}.get(str(e), 400)
            return web.json_response({"error": str(e)}, status=status)

    loop = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, _find_listing_any_network, offer_index)
    if found is None or not found[1]["is_live"]:
        return web.json_response({"error": "not found"}, status=404)
    network, row = found
    if row["seller"] != wallet:
        return web.json_response({"error": "not your listing"}, status=403)

    _prune_sessions(market_sessions, market_flow.TERMINAL_STATES)
    active = _active_session(
        market_sessions, market_flow.TERMINAL_STATES, user["id"], _platform(user)
    )
    if active:
        return web.json_response(
            {"error": "a market action is already in progress", "session": active.to_dict()},
            status=409,
        )

    payload = await xumm_ops.create_cancel_offer_payload(
        wallet,
        offer_index,
        return_url=xumm_ops.discord_return_url(body.get("guild_id"), body.get("channel_id")),
        user_token=await _push_token(user),
    )
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)

    session = market_flow.CancelSession(
        discord_id=user["id"],
        wallet_address=wallet,
        offer_index=offer_index,
        network=network,
        platform=_platform(user),
    )
    session.qr_url = payload["qr_url"]
    session.xumm_url = payload["xumm_url"]
    session.payload_uuid = payload.get("uuid")
    market_sessions[session.id] = session
    return web.json_response(session.to_dict())


@require_market
@require_wallet
async def handle_market_buy_start(request):
    """POST /api/market/buy {offer_index}: 404/410 if the listing is unknown
    or dead; 403 closet_required for a trait listing when the buyer has no
    active Closet; fail-closed on-ledger re-verify (410 listing_unavailable +
    stale on any mismatch/absence/RPC failure, including verify_sell_offer
    itself raising); otherwise the NFTokenAcceptOffer XUMM payload, with the
    price echoed in the response's instruction text."""
    user = request["user"]
    wallet = request["wallet"]
    body = await request.json()
    offer_index = body.get("offer_index")
    if not offer_index:
        return web.json_response({"error": "offer_index is required"}, status=400)

    if _use_market_mock():
        try:
            return web.json_response(mock_market.INSTANCE.start_buy(wallet, offer_index))
        except mock_market.MockMarketError as e:
            status = {"not found": 404, "listing_unavailable": 410, "closet_required": 403}.get(
                str(e), 400
            )
            return web.json_response({"error": str(e)}, status=status)

    loop = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, _find_listing_any_network, offer_index)
    if found is None:
        return web.json_response({"error": "not found"}, status=404)
    network, row = found
    if not row["is_live"]:
        return web.json_response({"error": "listing_unavailable"}, status=410)

    # Buying your own listing is a no-op that would fail on-ledger
    # (tecCANT_ACCEPT_OWN_OFFER) — reject up front instead of spending a sign.
    if row["seller"] == wallet:
        return web.json_response({"error": "cannot buy your own listing"}, status=400)

    # Trait ON-LEDGER ops (verify/accept/settlement-deposit) assume
    # ECONOMY_NETWORK == XRPL_NETWORK; with the economy off (or on a different
    # net) they'd fail-verify against the wrong chain, so gate trait buys on
    # the same flag the trait wizard/sweep use. Characters are unaffected.
    if row["kind"] == "trait" and not config.ECONOMY_ENABLED:
        return _economy_disabled_response()

    if row["kind"] == "trait":
        active = await loop.run_in_executor(None, _closet_active, _market_network("trait"), wallet)
        if not active:
            return web.json_response({"error": "closet_required"}, status=403)

    try:
        verified = await market_ops.verify_sell_offer(
            row["nft_id"], offer_index, row["amount_drops"], strict=True
        )
    except Exception as e:
        # A lookup FAILURE (RPC down / rippled soft-error), NOT a verified
        # absence — do not stale-close a possibly-healthy listing; ask the
        # buyer to retry. No DB write (fix #3).
        logging.warning(f"verify_sell_offer lookup failed for offer {offer_index}: {e}")
        return web.json_response(
            {"error": "could not verify the listing right now, please retry"}, status=503
        )
    if not verified:
        # Lookup succeeded and the offer is genuinely absent/mismatched/foreign
        # — safe to stale-close and report unavailable.
        await loop.run_in_executor(None, _close_listing_sync, network, offer_index, "stale")
        return web.json_response({"error": "listing_unavailable"}, status=410)

    _prune_sessions(market_sessions, market_flow.TERMINAL_STATES)
    active = _active_session(
        market_sessions, market_flow.TERMINAL_STATES, user["id"], _platform(user)
    )
    if active:
        return web.json_response(
            {"error": "a market action is already in progress", "session": active.to_dict()},
            status=409,
        )

    payload = await xumm_ops.create_accept_offer_payload(
        offer_index,
        return_url=xumm_ops.discord_return_url(body.get("guild_id"), body.get("channel_id")),
        user_token=await _push_token(user),
    )
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)

    amount_xrp = market_ops.drops_to_xrp_str(str(row["amount_drops"]))
    session = market_flow.BuySession(
        discord_id=user["id"],
        wallet_address=wallet,
        offer_index=offer_index,
        nft_id=row["nft_id"],
        listing_kind=row["kind"],
        network=network,
        amount_drops=row["amount_drops"],
        platform=_platform(user),
    )
    session.qr_url = payload["qr_url"]
    session.xumm_url = payload["xumm_url"]
    session.payload_uuid = payload.get("uuid")
    session.instruction = f"Confirm purchase for {amount_xrp} XRP"
    market_sessions[session.id] = session
    return web.json_response(session.to_dict())


def _make_market_status_handler(prefix: str):
    @require_auth
    async def handler(request):
        if _use_market_mock():
            try:
                return web.json_response(
                    mock_market.INSTANCE.status(request.match_info["session_id"])
                )
            except KeyError:
                return web.json_response({"error": "not found"}, status=404)
        session = market_sessions.get(request.match_info["session_id"])
        if (
            not session
            or session.discord_id != request["user"]["id"]
            or getattr(session, "platform", "discord") != _platform(request["user"])
        ):
            return web.json_response({"error": "not found"}, status=404)
        if getattr(session, "kind", prefix) != prefix:
            return web.json_response({"error": "not found"}, status=404)

        loop = asyncio.get_event_loop()
        if prefix == "list":
            row = await market_flow.advance_list_session(session)
            if row is not None:
                network = _market_network(session.listing_kind)
                await loop.run_in_executor(None, _write_listing_row, network, row)
        elif prefix == "cancel":
            if await market_flow.advance_cancel_session(session):
                await loop.run_in_executor(
                    None, _close_listing_sync, session.network, session.offer_index, "cancelled"
                )
        elif prefix == "buy":
            outcome = await market_flow.advance_buy_session(session)
            if outcome == "sold":
                # Persist the buyer on the sold row so settlement stays
                # recoverable even if run_deposit deletes the trait_tokens
                # ownership row before Closet credit (CodeRabbit #129).
                await loop.run_in_executor(
                    None,
                    _close_listing_sync,
                    session.network,
                    session.offer_index,
                    "sold",
                    session.wallet_address,
                )
                if session.listing_kind == "trait":
                    # Primary settlement trigger (spec §Q7): burn the sold
                    # trait token back into the buyer's Closet right away.
                    # Awaited (not fire-and-forget) — run_deposit's own
                    # fail-closed/journaling guarantees mean there is nothing
                    # to gain from detaching it, and awaiting keeps the
                    # outcome deterministic for both callers and tests. A
                    # failure here leaves settled=0 (already set by
                    # close_listing above) for the settlement sweep to retry.
                    await _settle_trait_sale(
                        session.wallet_address, session.nft_id, session.offer_index, session.network
                    )
            elif outcome == "stale":
                await loop.run_in_executor(
                    None, _close_listing_sync, session.network, session.offer_index, "stale"
                )

        return web.json_response(session.to_dict())

    return require_market(handler)


handle_market_list_status = _make_market_status_handler("list")
handle_market_cancel_status = _make_market_status_handler("cancel")
handle_market_buy_status = _make_market_status_handler("buy")


# --- Task 9 (spec §Q7): trait-sale settlement (burn sold trait -> buyer's Closet) ---


async def _settle_trait_sale(buyer: str, nft_id: str, offer_index: str, network: str) -> bool:
    """Run a Closet deposit on the buyer's behalf for one sold trait listing:
    exactly `economy_flow.run_deposit` (the shipped Phase-4 flow, unchanged),
    fail-closed on-ledger owner verify -> issuer burn -> Closet credit. On
    success, flips `market_listings.settled` to 1; on any failure (including a
    buyer with no active Closet — economy_flow's own precondition) the row is
    left exactly as `close_listing(sold)` set it (settled=0) and
    `run_deposit` has already journaled the failure to ECONOMY_RECORDS_DIR for
    recovery — this function adds no journal of its own. Returns whether
    settlement completed, so callers (the buy status handler, the sweep) can
    decide what to do next without a second DB read."""
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        economy_store.init_economy_schema(conn)
        market_store.init_db(conn)
        deps = economy_api.build_settlement_deps(conn)
        deposit_session = economy_flow.DepositSession(owner=buyer, nft_id=nft_id)
        await economy_flow.run_deposit(deposit_session, deps)
        if deposit_session.state == economy_flow.DONE:
            market_store.mark_settled(conn, offer_index)
            return True
        return False
    finally:
        conn.close()


# Bounded retry for the settlement sweep: a buyer with no active Closet fails
# run_deposit's precondition cleanly EVERY sweep pass (the token just sits in
# their wallet as an ordinary trait token, recoverable via a manual Deposit
# once they do claim a Closet) — without a bound this would retry forever.
_SWEEP_MAX_ATTEMPTS = 5
_SWEEP_PERIOD_SECONDS = 120
# offer_index -> consecutive failed sweep attempts. In-memory only (not
# persisted): a service restart resets every count to 0, so a mid-flight
# restart costs a stuck row a few retries rather than falsely reading as
# "already exhausted". This deployment restarts rarely (pm2, no rolling
# restarts) and a durable counter buys nothing but a crash-loop that could
# exhaust the budget in seconds instead of ~10 minutes.
_sweep_attempts: dict[str, int] = {}


def _write_sweep_giveup_record(offer_index: str, nft_id: str, buyer: str) -> None:
    """Journal (ECONOMY_RECORDS_DIR, same convention as economy_flow's own
    per-op records) that the sweep is no longer retrying this sale. The token
    is NOT lost — it is an ordinary trait token in `buyer`'s wallet; they can
    register/claim a Closet and Deposit it manually. `settled` stays 0 (spec
    §Q7): this is a durable breadcrumb for an admin/support to find, not a
    change to the row's meaning."""
    try:
        os.makedirs(config.ECONOMY_RECORDS_DIR, exist_ok=True)
        path = os.path.join(
            config.ECONOMY_RECORDS_DIR, f"trait-settlement-giveup-{offer_index}.json"
        )
        with open(path, "w") as f:
            json.dump(
                {
                    "offer_index": offer_index,
                    "nft_id": nft_id,
                    "buyer": buyer,
                    "attempts": _SWEEP_MAX_ATTEMPTS,
                    "status": "abandoned",
                },
                f,
                indent=2,
            )
    except Exception:
        logging.error(
            f"failed to write sweep giveup record for {offer_index}: {traceback.format_exc()}"
        )


async def settle_pending_trait_sales() -> None:
    """Backstop for `_settle_trait_sale` (spec §Q7): scans
    `market_listings(kind='trait', closed_reason='sold', settled=0)` on the
    trait-economy network and retries settlement for each. Heals service
    restarts mid-settlement and third-party ledger fills (a direct
    NFTokenAcceptOffer from outside this app — the listener still marks the
    row sold/unsettled with no buyer of record in `market_listings` itself,
    so the buyer is resolved from `trait_tokens.owner`, which the listener
    keeps current from the AcceptOffer it observed)."""
    network = _market_network("trait")
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        economy_store.init_economy_schema(conn)
        market_store.init_db(conn)
        rows = market_store.unsettled_trait_sales(conn)
        owners = {nid: owner for nid, owner, _slot, _value in economy_store.read_trait_tokens(conn)}
    finally:
        conn.close()

    for row in rows:
        offer_index = row["offer_index"]
        if _sweep_attempts.get(offer_index, 0) >= _SWEEP_MAX_ATTEMPTS:
            continue  # already given up + journaled on a previous pass
        # Prefer the buyer persisted on the sold row (durable across
        # run_deposit's trait_tokens delete); fall back to the current token
        # owner for legacy rows written before buyer was persisted, or for
        # third-party fills the listener recorded with no buyer on the row.
        buyer = row.get("buyer") or owners.get(row["nft_id"])
        if buyer is None:
            # Neither a persisted buyer nor a current owner — the listener
            # hasn't (yet) recorded an owner for this token: transient lag,
            # not a precondition failure. Try again next sweep without
            # counting an attempt.
            continue
        try:
            settled = await _settle_trait_sale(buyer, row["nft_id"], offer_index, network)
        except Exception:
            logging.error(f"settlement sweep crashed for {offer_index}: {traceback.format_exc()}")
            settled = False
        if settled:
            _sweep_attempts.pop(offer_index, None)
            continue
        _sweep_attempts[offer_index] = _sweep_attempts.get(offer_index, 0) + 1
        if _sweep_attempts[offer_index] >= _SWEEP_MAX_ATTEMPTS:
            logging.warning(
                f"settlement sweep giving up on {offer_index} (nft {row['nft_id']}, buyer "
                f"{buyer}) after {_SWEEP_MAX_ATTEMPTS} attempts"
            )
            _write_sweep_giveup_record(offer_index, row["nft_id"], buyer)


async def _settlement_sweep_loop() -> None:
    while True:
        try:
            await settle_pending_trait_sales()
        except Exception:
            logging.error(f"settlement sweep loop crashed: {traceback.format_exc()}")
        await asyncio.sleep(_SWEEP_PERIOD_SECONDS)


async def _start_settlement_sweep(app: web.Application) -> None:
    """aiohttp on_startup hook: schedule the settlement sweep as a background
    task for the lifetime of the app. Gated on ECONOMY_ENABLED — with the
    trait economy off there are no trait listings to settle."""
    if not config.ECONOMY_ENABLED:
        return
    app["settlement_sweep_task"] = asyncio.get_event_loop().create_task(_settlement_sweep_loop())


async def _stop_settlement_sweep(app: web.Application) -> None:
    task = app.get("settlement_sweep_task")
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _economy_disabled_response():
    return web.json_response(
        {"error": "the trait economy is not enabled", "code": "economy_disabled"}, status=403
    )


def require_economy(handler):
    """Gate an economy/Closet route on config.ECONOMY_ENABLED (checked before
    auth so a disabled deploy exposes nothing of the economy surface)."""

    @functools.wraps(handler)
    async def wrapper(request):
        if not config.ECONOMY_ENABLED:
            return _economy_disabled_response()
        return await handler(request)

    return wrapper


# --- Task 9 (spec §Q7): trait sell wizard — Extract then List, one action ---


@require_market
@require_economy
@require_wallet
async def handle_market_trait_list_start(request):
    """POST /api/market/trait/list {slot, value, price_xrp}: the composite
    "sell a trait out of my Closet" wizard — the existing Phase-4 Extract flow
    (Xaman signature 1) followed by the plain Q4 List flow on the
    freshly-owned token (Xaman signature 2), driven together as one polled
    TraitSellSession (see market_flow.advance_trait_sell_session).

    price_xrp is validated FIRST (same guard as handle_market_list_start) so a
    bad price never starts an extract. Extract's own preconditions (active
    Closet, the (slot, value) trait actually loose in it) surface as
    economy_api.EconomyError -> 400 with no session started; a failure inside
    the running extract surfaces later as the session's own error with no
    listing ever created — an extracted-but-never-listed token is a perfectly
    ordinary wallet trait token, recoverable under /api/market/mine."""
    user = request["user"]
    wallet = request["wallet"]
    body = await request.json()
    slot = body.get("slot")
    value = body.get("value")
    price_xrp = body.get("price_xrp")
    if not slot or not value or not isinstance(price_xrp, str):
        return web.json_response(
            {"error": "slot, value, and price_xrp (string) are required"}, status=400
        )
    try:
        amount_drops = int(market_ops.xrp_to_drops_str(price_xrp))
    except Exception as e:
        # Broad on purpose: see handle_market_list_start's identical guard.
        return web.json_response({"error": f"bad price_xrp: {e}"}, status=400)

    if _use_market_mock():
        try:
            return web.json_response(
                mock_market.INSTANCE.start_trait_list(wallet, slot, value, amount_drops)
            )
        except mock_market.MockMarketError as e:
            return web.json_response({"error": str(e)}, status=400)

    _prune_sessions(market_sessions, market_flow.TERMINAL_STATES)
    active = _active_session(
        market_sessions, market_flow.TERMINAL_STATES, user["id"], _platform(user)
    )
    if active:
        return web.json_response(
            {"error": "a market action is already in progress", "session": active.to_dict()},
            status=409,
        )

    try:
        extract_ws = await economy_api.start_extract(
            user["id"], wallet, {"slot": slot, "value": value}
        )
    except economy_api.EconomyError as e:
        return web.json_response({"error": str(e)}, status=400)
    except (KeyError, ValueError) as e:
        return web.json_response({"error": f"missing or invalid field: {e}"}, status=400)
    except Exception as e:
        logging.error(f"trait sell wizard failed to start extract: {e}")
        return web.json_response({"error": "could not start the action"}, status=502)

    session = market_flow.TraitSellSession(
        discord_id=user["id"],
        wallet_address=wallet,
        slot=slot,
        value=value,
        amount_drops=amount_drops,
        extract_session=extract_ws.inner,
        platform=_platform(user),
    )
    market_sessions[session.id] = session
    return web.json_response(session.to_dict())


@require_market
@require_auth
async def handle_market_trait_list_status(request):
    """GET /api/market/trait/list/{session_id}: advance + report the
    TraitSellSession — mirrors _make_market_status_handler's "list" branch
    exactly (same finalize-row write) once the wizard reaches its own List
    step, since advance_trait_sell_session delegates that step to
    advance_list_session directly."""
    if _use_market_mock():
        try:
            return web.json_response(mock_market.INSTANCE.status(request.match_info["session_id"]))
        except KeyError:
            return web.json_response({"error": "not found"}, status=404)
    session = market_sessions.get(request.match_info["session_id"])
    if (
        not session
        or session.discord_id != request["user"]["id"]
        or getattr(session, "platform", "discord") != _platform(request["user"])
        or getattr(session, "kind", "trait_list") != "trait_list"
    ):
        return web.json_response({"error": "not found"}, status=404)

    row = await market_flow.advance_trait_sell_session(session)
    if row is not None:
        network = _market_network("trait")
        await asyncio.get_event_loop().run_in_executor(None, _write_listing_row, network, row)
    return web.json_response(session.to_dict())


@require_economy
@require_wallet
async def handle_closet(request):
    """Ensure the caller has a Closet NFToken, minting on first use. In dev mode
    returns a stub active record (Task 7 will expand the mock)."""
    if config.WEBAPP_DEV_MODE:
        return web.json_response(mock_economy.INSTANCE.create_closet(request["wallet"]))
    user = request["user"]
    try:
        result = await economy_api.start_closet(user["id"], request["wallet"])
    except Exception as e:
        logging.error(f"start_closet failed for {user['id']}: {e}")
        return web.json_response({"error": "could not create or retrieve Closet"}, status=502)
    return web.json_response(result)


@require_auth
async def handle_register(request):
    user = request["user"]
    platform = _platform(user)
    body = await request.json()
    wallet = (body.get("wallet") or "").strip()
    if not is_valid_classic_address(wallet):
        return web.json_response({"error": "invalid XRPL address"}, status=400)
    # The legacy Users table is keyed by discord_id with no platform column, so
    # only the discord platform may write it — a colliding numeric id from
    # another platform would silently overwrite a discord user's wallet (and be
    # mismigrated into identities as a discord row on the next startup). Non-
    # discord platforms live in identities only; _resolve_wallet gates its legacy
    # fallback on discord, so it never consults this table for them.
    if platform == "discord":
        if not await asyncio.to_thread(register_user, user["id"], user["name"], wallet):
            return web.json_response(
                {"error": "registration failed", "code": "register_failed"}, status=500
            )
    linked = await asyncio.to_thread(
        identity_store.link, platform, user["id"], user["name"], wallet
    )
    if not linked:
        logging.error(
            "identity.link failed for %s:%s — /events/me may 403 until restart-migrate",
            platform,
            user["id"],
        )
    # Best-effort Closet issuance post-registration: kick off ensure_closet so the
    # user's Closet NFToken is minted immediately on registration. This never blocks
    # or fails the registration response — any error is logged and ignored.
    closet_result: dict[str, Any] | None = None
    if config.ECONOMY_ENABLED and not config.WEBAPP_DEV_MODE:
        try:
            closet_result = await economy_api.start_closet(user["id"], wallet)
        except Exception as e:
            logging.warning(f"post-register ensure_closet failed for {wallet}: {e}")
    resp: dict[str, Any] = {"ok": True, "wallet": wallet}
    if closet_result is not None:
        resp["closet_accept"] = closet_result.get("accept")
    return web.json_response(resp)


async def _request_return_url(request):
    """Optional XUMM return_url from the client's guild/channel context;
    bad/missing IDs simply mean no return button in Xaman (issue #14)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return xumm_ops.discord_return_url(body.get("guild_id"), body.get("channel_id"))


@require_wallet
async def handle_mint_start(request):
    user = request["user"]
    _prune_sessions(mint_sessions, mint_flow.TERMINAL_STATES)

    # Resolve every suspending value BEFORE the one-active-session check so no
    # await sits between the check and the insert below (the guard is only
    # race-free while that window stays await-free).
    return_url = await _request_return_url(request)
    push_user_token = await _push_token(user)

    # One active session per user (no awaits between this check and the
    # insert below, so it cannot race)
    active = _active_session(mint_sessions, mint_flow.TERMINAL_STATES, user["id"], _platform(user))
    if active:
        return web.json_response(
            {"error": "mint already in progress", "session": active.to_dict()}, status=409
        )

    session = mint_flow.MintSession(
        discord_id=user["id"],
        wallet_address=request["wallet"],
        return_url=return_url,
        platform=_platform(user),
        push_user_token=push_user_token,
    )
    mint_sessions[session.id] = session
    # Detect the payment path (LFGO holder vs XRP newcomer) and create the
    # XUMM sign request before the first QR is rendered (after the insert
    # above, so the one-active-session guard stays race-free). Bounded so a
    # stalled XRPL/XUMM API can't hang /api/mint — on timeout the session
    # falls back to the XRP path with the static detect link.
    try:
        await asyncio.wait_for(session.prepare_payment(), timeout=8)
    except asyncio.TimeoutError:
        logging.warning("prepare_payment timed out; falling back to XRP path")
    except Exception as e:
        logging.warning(f"prepare_payment failed; falling back to XRP path: {e}")
    session.ensure_payment_fallback()
    asyncio.get_event_loop().create_task(mint_flow.run_mint_session(session))
    return web.json_response(session.to_dict())


@require_wallet
async def handle_nfts(request):
    """List the user's swappable collection NFTs (normalized metadata)."""
    try:
        nfts = await swap_meta.load_wallet_nfts(request["wallet"], xrpl_ops.get_account_nfts)
    except Exception as e:
        logging.error(f"NFT listing failed: {e}")
        return web.json_response({"error": "failed to load wallet NFTs"}, status=502)
    # Quote the swap fee for the cost line (BRIX holders pay BRIX; everyone
    # else the AMM XRP equivalent). Advisory only — the swap session
    # re-detects the path server-side when the fee is actually charged.
    swap_fee = None
    try:
        pay_with, amount = await swap_flow.detect_swap_payment(
            request["wallet"], swap_flow.swap_fee_total(2)
        )
        swap_fee = {"pay_with": pay_with, "amount": amount, "per_nft": swap_flow.swap_fee_total(1)}
    except Exception as e:
        logging.warning(f"Swap fee quote failed: {e}")
    # Serialize the cross-body swap matrix so the client can mirror
    # swap_allowed() and only offer traits legal for the selected pair
    # (#30 Task 15) — swap_allowed() itself remains the server-side gate.
    cfg = trait_config.get_config()
    matrix = {
        "universal_layers": sorted(cfg.universal_layers),
        "pairs": [
            {
                "bodies": sorted(p.bodies),
                "layers": sorted(p.layers) if p.layers is not None else None,
                "layers_except": (sorted(p.layers_except) if p.layers_except is not None else None),
            }
            for p in cfg.swap_pairs
        ],
    }
    return web.json_response(
        {
            "nfts": nfts,
            "swappable_traits": swap_meta.SWAPPABLE_TRAITS,
            "swap_fee": swap_fee,
            "swap_matrix": matrix,
        }
    )


@require_wallet
async def handle_swap_start(request):
    user = request["user"]
    body = await request.json()
    nft1_id = body.get("nft1_id")
    nft2_id = body.get("nft2_id")
    traits_to_swap = body.get("traits", [])
    if not nft1_id or not nft2_id or nft1_id == nft2_id:
        return web.json_response({"error": "select two different NFTs"}, status=400)
    if not traits_to_swap or any(t not in swap_meta.SWAPPABLE_TRAITS for t in traits_to_swap):
        return web.json_response({"error": "invalid trait selection"}, status=400)

    _prune_sessions(swap_sessions, swap_flow.TERMINAL_STATES)
    if _active_session(swap_sessions, swap_flow.TERMINAL_STATES, user["id"], _platform(user)):
        return web.json_response({"error": "swap already in progress"}, status=409)

    # Re-verify ownership and metadata server-side (never trust client data)
    try:
        nfts = await swap_meta.load_wallet_nfts(request["wallet"], xrpl_ops.get_account_nfts)
    except Exception as e:
        logging.error(f"NFT verification failed: {e}")
        return web.json_response({"error": "failed to verify wallet NFTs"}, status=502)
    by_id = {n["nft_id"]: n for n in nfts}
    nft1, nft2 = by_id.get(nft1_id), by_id.get(nft2_id)
    if not nft1 or not nft2:
        return web.json_response({"error": "NFT not found in your wallet"}, status=400)
    cfg = trait_config.get_config()
    blocked = [t for t in traits_to_swap if not cfg.swap_allowed(nft1["gender"], nft2["gender"], t)]
    if blocked:
        return web.json_response(
            {
                "error": (
                    f"trait(s) {', '.join(blocked)} cannot swap between "
                    f"{nft1['gender']} and {nft2['gender']} bodies"
                )
            },
            status=400,
        )

    # Resolve the push token before the re-check so no await sits between the
    # guard and the insert below (would reopen the race the re-check closes).
    push_user_token = await _push_token(user)
    # The load_wallet_nfts call above awaited, so re-check before inserting
    if _active_session(swap_sessions, swap_flow.TERMINAL_STATES, user["id"], _platform(user)):
        return web.json_response({"error": "swap already in progress"}, status=409)
    session = swap_flow.SwapSession(
        discord_id=user["id"],
        wallet_address=request["wallet"],
        nft1=nft1,
        nft2=nft2,
        traits_to_swap=traits_to_swap,
        return_url=xumm_ops.discord_return_url(body.get("guild_id"), body.get("channel_id")),
        platform=_platform(user),
        push_user_token=push_user_token,
    )
    swap_sessions[session.id] = session
    asyncio.get_event_loop().create_task(swap_flow.run_swap_session(session))
    return web.json_response(session.to_dict())


@require_auth
async def handle_mint_status(request):
    session = mint_sessions.get(request.match_info["session_id"])
    if (
        not session
        or session.discord_id != request["user"]["id"]
        or getattr(session, "platform", "discord") != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    # Refresh the QR-scanned flags so the client can swap the QR for a
    # spinner the moment Xaman opens the payload (issue #22).
    await mint_flow.update_scan_state(session)
    if session.state in mint_flow.TERMINAL_STATES and not getattr(session, "_published", False):
        session._published = True
        ok = session.state not in (mint_flow.FAILED, mint_flow.PAYMENT_TIMEOUT)
        await publish_event(
            "mint.completed" if ok else "mint.failed",
            enrich_minter_identity(session.platform, session.discord_id, session.wallet_address),
            session.wallet_address,
            session.to_dict(),
        )
    return web.json_response(session.to_dict())


@require_auth
async def handle_mint_regenerate(request):
    """Issue a fresh payment QR for a session whose payload expired before
    the user could scan it (issue #22)."""
    session = mint_sessions.get(request.match_info["session_id"])
    if (
        not session
        or session.discord_id != request["user"]["id"]
        or getattr(session, "platform", "discord") != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    if session.state != mint_flow.AWAITING_PAYMENT:
        return web.json_response({"error": "session is past payment"}, status=409)
    try:
        await asyncio.wait_for(session.regenerate_payment(), timeout=8)
    except Exception as e:
        logging.warning(f"regenerate_payment failed: {e}")
    session.ensure_payment_fallback()
    return web.json_response(session.to_dict())


def _first_result_image(session: Any) -> str | None:
    for r in getattr(session, "results", None) or []:
        img = r.get("image_url")
        if img:
            return str(img)
    return None


@require_auth
async def handle_swap_status(request):
    session = swap_sessions.get(request.match_info["session_id"])
    if (
        not session
        or session.discord_id != request["user"]["id"]
        or getattr(session, "platform", "discord") != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    await publish_terminal(
        session,
        "swap",
        wallet=session.wallet_address,
        user_id=session.discord_id,
        platform=session.platform,
        image_url=_first_result_image(session),
        success_states={swap_flow.OFFERS_READY, swap_flow.DONE},
        fail_states={swap_flow.FAILED, swap_flow.PAYMENT_TIMEOUT},
    )
    return web.json_response(session.to_dict())


# --- Xaman Sign In registration (issue #24) ---

# payload uuid -> {platform, user_id, name, created_at}; pruned by age
signin_payloads: dict[str, Any] = {}
SIGNIN_TTL = 900


def _prune_signin_payloads():
    cutoff = time.time() - SIGNIN_TTL
    for uuid, rec in list(signin_payloads.items()):
        if rec["created_at"] < cutoff:
            del signin_payloads[uuid]


@require_auth
async def handle_signin_start(request):
    """Create a XUMM SignIn payload; the user scans it in Xaman and their
    wallet address is captured on approval — no manual address entry."""
    user = request["user"]
    _prune_signin_payloads()
    # Optional link-intent (#90): proving the same wallet on a 2nd surface IS
    # the link. The only difference from a plain sign-in is the signed response
    # carries the account view; the request/response shape here is unchanged.
    try:
        body = await request.json()
    except Exception:
        body = {}
    link_intent = bool(body.get("link"))
    payload = await xumm_ops.create_signin_payload(return_url=await _request_return_url(request))
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    signin_payloads[payload["uuid"]] = {
        "platform": _platform(user),
        "user_id": user["id"],
        "name": user["name"],
        "link": link_intent,
        "created_at": time.time(),
    }
    return web.json_response({"uuid": payload["uuid"], "signin_link": payload["xumm_url"]})


@require_auth
async def handle_signin_status(request):
    uuid = request.match_info["payload_uuid"]
    rec = signin_payloads.get(uuid)
    # Ownership keyed by (platform, user_id) — cross-surface isolation: a
    # colliding numeric id on another platform cannot read/complete this payload.
    if (
        not rec
        or rec["user_id"] != request["user"]["id"]
        or rec["platform"] != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    s = await xumm_ops.get_payload_status(uuid)
    if not s:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    if s["signed"] and s["account"] and is_valid_classic_address(s["account"]):
        platform = rec["platform"]
        # Legacy Users table is keyed by discord_id with no platform column —
        # only discord writes it; other platforms live in identities only.
        if platform == "discord":
            if not await asyncio.to_thread(
                register_user, rec["user_id"], rec["name"], s["account"]
            ):
                return web.json_response({"error": "registration failed"}, status=500)
        linked = await asyncio.to_thread(
            identity_store.link, platform, rec["user_id"], rec["name"], s["account"]
        )
        if not linked:
            logging.error(
                "identity.link failed for %s:%s — /events/me may 403 until restart-migrate",
                platform,
                rec["user_id"],
            )
        # #135: capture the XUMM push token issued on this sign-in so future
        # sign requests can be push-delivered. Independent of link() success —
        # a transient link failure over an already-present identity row must
        # not drop the token; set_user_token is best-effort and no-ops on a
        # missing row anyway.
        if s.get("user_token"):
            await asyncio.to_thread(
                identity_store.set_user_token, platform, rec["user_id"], s["user_token"]
            )
        del signin_payloads[uuid]
        resp = {"state": "signed", "wallet": s["account"]}
        # Link-intent (#90): attach the full account view so the surface can
        # confirm "linked — also on Discord as @alice". Plain sign-in stays
        # byte-identical (no account key).
        if rec.get("link"):
            identities = await asyncio.to_thread(identity_store.identities_for_wallet, s["account"])
            resp["account"] = {"wallet": s["account"], "identities": identities}
        return web.json_response(resp)
    if s["expired"]:
        del signin_payloads[uuid]
        return web.json_response({"state": "expired"})
    return web.json_response({"state": "opened" if s["opened"] else "pending"})


async def handle_config(request):
    """Public config the frontend needs before auth (client_id, dev flag)."""
    return web.json_response(
        {
            "client_id": config.DISCORD_CLIENT_ID,
            "dev_mode": config.WEBAPP_DEV_MODE,
            "economy_enabled": config.ECONOMY_ENABLED,
            "market_enabled": config.MARKET_ENABLED,
        }
    )


async def handle_qr(request):
    """Server-rendered QR PNG (same-origin, satisfies the Activity CSP)."""
    data = request.query.get("d", "")
    if not data or len(data) > 2048:
        return web.json_response({"error": "bad data"}, status=400)
    png = await asyncio.to_thread(xumm_ops.generate_qr_png, data)
    return web.Response(body=png, content_type="image/png")


async def _fetch_cdn(url):
    """Fetch an image from the public CDN. Returns (body, content_type)."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f"CDN returned {resp.status}")
            return await resp.read(), resp.content_type


async def handle_img(request):
    """Same-origin proxy for CDN images: the Activity's CSP blocks cross-origin
    <img> loads, so the client routes CDN image URLs through here (allowed
    bases: BUNNY_CDN_PUBLIC_BASE and the BUNNY_PULL_ZONE custom domain)."""
    url = request.query.get("u", "")
    allowed = tuple(base + "/" for base in config.IMG_PROXY_ALLOWED_BASES)
    if len(url) > 2048 or not url.startswith(allowed):
        return web.json_response({"error": "bad image url"}, status=400)
    try:
        body, ctype = await _fetch_cdn(url)
    except Exception as e:
        logging.error(f"Image proxy fetch failed for {url}: {e}")
        return web.json_response({"error": "image fetch failed"}, status=502)
    # Mint/swap outputs get unique CDN basenames, so they are safe to cache.
    return web.Response(
        body=body, content_type=ctype, headers={"Cache-Control": "public, max-age=86400"}
    )


async def handle_layer(request):
    """Same-origin layer file for client-side compositing (CSP-safe).
    Resolves (body, trait, value) through the configured layer_store, which
    serves from local disk or the CDN download cache."""
    body = request.query.get("body", "")
    trait = request.query.get("trait", "")
    value = request.query.get("value", "")
    if (
        not body
        or not trait
        or not value
        or any(len(x) > 128 or "/" in x or ".." in x for x in (body, trait, value))
    ):
        return web.json_response({"error": "bad layer params"}, status=400)
    store = layer_store.get_layer_store()
    path = await store.resolve(body, trait, value)
    if not path or not os.path.exists(path):
        return web.json_response({"error": "layer not found"}, status=404)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return web.FileResponse(
        path, headers={"Content-Type": ctype, "Cache-Control": "public, max-age=86400"}
    )


async def handle_economy(request):
    if not config.ECONOMY_ENABLED:
        return _economy_disabled_response()
    if config.WEBAPP_DEV_MODE:
        return web.json_response(mock_economy.INSTANCE.read_state(request["wallet"]))
    conn = economy_api.open_conn()
    try:
        return web.json_response(economy_api.read_economy_state(conn, request["wallet"]))
    finally:
        conn.close()


def _economy_post(kind, start_coro, mock_call):
    async def handler(request):
        if not config.ECONOMY_ENABLED:
            return _economy_disabled_response()
        user = request["user"]
        body = await request.json()
        if config.WEBAPP_DEV_MODE:
            try:
                return web.json_response(mock_call(request["wallet"], body))
            except Exception as e:
                return web.json_response({"error": str(e)}, status=400)
        _prune_sessions(economy_sessions, economy_api.TERMINAL_STATES)
        if _active_session(
            economy_sessions, economy_api.TERMINAL_STATES, user["id"], _platform(user)
        ):
            return web.json_response(
                {"error": "an economy action is already in progress"}, status=409
            )
        try:
            ws = await start_coro(user["id"], request["wallet"], body)
        except economy_api.EconomyError as e:
            return web.json_response({"error": str(e)}, status=400)
        except (KeyError, ValueError) as e:
            return web.json_response({"error": f"missing or invalid field: {e}"}, status=400)
        except Exception as e:
            logging.error(f"{kind} failed to start: {e}")
            return web.json_response({"error": "could not start the action"}, status=502)
        ws.platform = _platform(user)
        economy_sessions[ws.id] = ws
        return web.json_response(ws.to_dict())

    return handler


handle_equip_start = _economy_post(
    "equip",
    lambda uid, w, b: economy_api.start_equip(uid, w, b["nft_id"], b["slot"], b["value"]),
    lambda w, b: mock_economy.INSTANCE.equip(w, b["nft_id"], b["slot"], b["value"]),
)
handle_harvest_start = _economy_post(
    "harvest",
    lambda uid, w, b: economy_api.start_harvest(uid, w, b["nft_id"]),
    lambda w, b: mock_economy.INSTANCE.harvest(w, b["nft_id"]),
)
handle_assemble_start = _economy_post(
    "assemble",
    lambda uid, w, b: economy_api.start_assemble(uid, w, int(b["edition"]), b["chosen"]),
    lambda w, b: mock_economy.INSTANCE.assemble(w, int(b["edition"]), b["chosen"]),
)
handle_extract_start = _economy_post(
    "extract",
    lambda uid, w, b: economy_api.start_extract(uid, w, b),
    lambda w, b: mock_economy.INSTANCE.extract(w, b),
)
handle_deposit_start = _economy_post(
    "deposit",
    lambda uid, w, b: economy_api.start_deposit(uid, w, b),
    lambda w, b: mock_economy.INSTANCE.deposit(w, b),
)


def _make_economy_status_handler(prefix: str):
    @require_auth
    async def handler(request):
        session = economy_sessions.get(request.match_info["session_id"])
        if (
            not session
            or session.discord_id != request["user"]["id"]
            or getattr(session, "platform", "discord") != _platform(request["user"])
        ):
            return web.json_response({"error": "not found"}, status=404)
        # The three economy ops share one `economy_sessions` dict. Guard against
        # polling e.g. assemble/{harvest_id}/status, which would otherwise
        # publish `assemble.completed` for a harvest session and burn its
        # `_published` slot. `EconomyWebSession.kind` is the authoritative op.
        if getattr(session, "kind", prefix) != prefix:
            return web.json_response({"error": "not found"}, status=404)
        # The wallet is nested as inner.owner; enrichment never raises on a
        # missing wallet, so identity falls back to the bare id if absent.
        wallet = getattr(getattr(session, "inner", None), "owner", None)
        # Only assemble yields a new artwork; equip/harvest carry no image.
        image_url = session.to_dict().get("image_url") if prefix == "assemble" else None
        await publish_terminal(
            session,
            prefix,
            wallet=wallet,
            user_id=session.discord_id,
            platform=getattr(session, "platform", "discord"),
            image_url=image_url,
            success_states={economy_flow.DONE},
            fail_states={economy_flow.FAILED},
        )
        return web.json_response(session.to_dict())

    return handler


handle_equip_status = _make_economy_status_handler("equip")
handle_harvest_status = _make_economy_status_handler("harvest")
handle_assemble_status = _make_economy_status_handler("assemble")
handle_extract_status = _make_economy_status_handler("extract")
handle_deposit_status = _make_economy_status_handler("deposit")


def _client_dir_mtime() -> float:
    latest = 0.0
    for root, _dirs, files in os.walk(CLIENT_DIR):
        for f in files:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                continue
    return latest


async def handle_dev_reload(request):
    if not config.WEBAPP_DEV_MODE:
        return web.json_response({"error": "not found"}, status=404)
    resp = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"}
    )
    await resp.prepare(request)
    last = _client_dir_mtime()
    try:
        while True:
            await asyncio.sleep(0.5)
            now = _client_dir_mtime()
            if now > last:
                last = now
                await resp.write(b"data: reload\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def handle_index(request):
    return web.FileResponse(os.path.join(CLIENT_DIR, "index.html"))


@web.middleware
async def no_cache_mw(request, handler):
    # The Activity is served behind Discord's caching proxy; without this an
    # updated frontend (index.html / app.js / vendored SDK) keeps serving stale
    # from Discord's edge or the browser, even after relaunching the Activity.
    resp = await handler(request)
    if "Cache-Control" not in resp.headers:  # handlers may opt out (image proxy)
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


def create_app() -> web.Application:
    app = web.Application(middlewares=[no_cache_mw])
    identity_store.ensure_identities_table()
    identity_store.migrate_users_to_identities()
    app.router.add_get("/api/config", handle_config)
    app.router.add_post("/api/token", handle_token)
    app.router.add_post("/api/session", handle_session)
    app.router.add_post("/api/telegram/auth", handle_telegram_auth)
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/account", handle_account)
    app.router.add_post("/api/register", handle_register)
    app.router.add_post("/api/mint", handle_mint_start)
    app.router.add_get("/api/mint/{session_id}", handle_mint_status)
    app.router.add_post("/api/mint/{session_id}/regenerate", handle_mint_regenerate)
    app.router.add_post("/api/signin", handle_signin_start)
    app.router.add_get("/api/signin/{payload_uuid}", handle_signin_status)
    app.router.add_get("/api/nfts", handle_nfts)
    app.router.add_get("/api/leaderboard", handle_leaderboard)
    app.router.add_get("/api/market/listings", handle_market_listings)
    app.router.add_get("/api/market/mine", handle_market_mine)
    app.router.add_get("/api/market/history", handle_market_history)
    app.router.add_post("/api/market/list", handle_market_list_start)
    app.router.add_get("/api/market/list/{session_id}", handle_market_list_status)
    app.router.add_post("/api/market/cancel", handle_market_cancel_start)
    app.router.add_get("/api/market/cancel/{session_id}", handle_market_cancel_status)
    app.router.add_post("/api/market/buy", handle_market_buy_start)
    app.router.add_get("/api/market/buy/{session_id}", handle_market_buy_status)
    app.router.add_post("/api/market/trait/list", handle_market_trait_list_start)
    app.router.add_get("/api/market/trait/list/{session_id}", handle_market_trait_list_status)
    app.router.add_post("/api/swap", handle_swap_start)
    app.router.add_get("/api/swap/{session_id}", handle_swap_status)
    app.router.add_get("/api/qr.png", handle_qr)
    app.router.add_get("/api/img", handle_img)
    app.router.add_get("/api/layer", handle_layer)
    app.router.add_post("/api/closet", handle_closet)
    app.router.add_get("/api/economy", require_wallet(handle_economy))
    app.router.add_post("/api/equip", require_wallet(handle_equip_start))
    app.router.add_get("/api/equip/{session_id}", handle_equip_status)
    app.router.add_post("/api/harvest", require_wallet(handle_harvest_start))
    app.router.add_get("/api/harvest/{session_id}", handle_harvest_status)
    app.router.add_post("/api/assemble", require_wallet(handle_assemble_start))
    app.router.add_get("/api/assemble/{session_id}", handle_assemble_status)
    app.router.add_post("/api/extract", require_wallet(handle_extract_start))
    app.router.add_get("/api/extract/{session_id}", handle_extract_status)
    app.router.add_post("/api/deposit", require_wallet(handle_deposit_start))
    app.router.add_get("/api/deposit/{session_id}", handle_deposit_status)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/events/me", handle_events_me)
    app.router.add_get("/__dev/reload", handle_dev_reload)
    app.router.add_get("/", handle_index)
    app.router.add_static("/", CLIENT_DIR)
    app.on_startup.append(_start_settlement_sweep)
    app.on_cleanup.append(_stop_settlement_sweep)
    return app


def main():
    if not config.DISCORD_CLIENT_ID or not config.DISCORD_CLIENT_SECRET:
        raise ValueError(
            "DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET must be set "
            "for the Activity webapp (see docs/ACTIVITY_SETUP.md)"
        )
    create_users_table()
    logging.info(f"Starting LFG Activity webapp on port {config.WEBAPP_PORT}")
    web.run_app(create_app(), port=config.WEBAPP_PORT)


if __name__ == "__main__":
    main()
