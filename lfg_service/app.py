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
import re
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from xrpl.core.addresscodec import is_valid_classic_address

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import (
    brix_payment,
    bulk_mint_flow,
    closet_token,
    config,
    db_path,
    economy_flow,
    economy_store,
    history_store,
    image_archive,
    layer_store,
    leaderboard,
    market_flow,
    market_ops,
    market_store,
    memos,
    mint_flow,
    nft_index,
    rarity,
    shop,
    shop_flow,
    shop_store,
    swap_flow,
    swap_meta,
    trait_config,
    xrpl_ops,
    xumm_ops,
)
from lfg_core.user_db import create_users_table, get_user, register_user
from lfg_service import identity as identity_store
from lfg_service.auth import require_service_token, surface_for_token
from lfg_service.events import Event, InMemoryEventBus
from lfg_service.telegram_auth import validate_init_data
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
# Trait Shop (#217) buy sessions: shop_flow.ShopBuySession keyed by .id, same
# "one dict, poll via GET status" shape as market_sessions. ShopBuySession has
# no created_at (unlike the other session dataclasses), so pruning tracks
# creation time in a parallel dict rather than reusing _prune_sessions.
shop_sessions: dict[str, Any] = {}
_shop_session_created: dict[str, float] = {}
# Bulk mint (#215) jobs: bulk_mint_flow.BulkMintJob keyed by .id, same
# "one dict, poll via GET status" shape as mint_sessions, but FULFILLING is
# deliberately non-terminal (see bulk_mint_flow.TERMINAL_STATES) so a live job
# stays visible to /api/mint/bulk/active and the startup resume sweep.
bulk_sessions: dict[str, Any] = {}
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
    video_url: str | None = None,
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
    if video_url and not data.get("video_url"):
        data["video_url"] = video_url
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


async def _persist_issued_user_token(user: dict[str, Any], session: Any) -> None:
    """#212: persist a push token a flow captured off a signed payload (see
    the flows' `_capture_issued_token`). Sign-in used to be the only capture
    point — refreshing here keeps tokens current as XUMM rotates them and
    self-heals after an app-key swap invalidates every stored token.
    Best-effort; cleared on the session so each capture writes once."""
    token = getattr(session, "issued_user_token", None)
    if not token:
        return
    session.issued_user_token = None
    await asyncio.to_thread(identity_store.set_user_token, _platform(user), user["id"], token)


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
# Guards all _MARKET_CACHE access: reads/puts happen on the event-loop thread
# while _invalidate_market_cache pops from executor threads — an unlocked pop
# during _market_cache_put's iteration could raise RuntimeError.
_MARKET_CACHE_LOCK = threading.Lock()
# Per-key invalidation generation: a cache fill captures the key's generation
# before computing and only inserts if it is unchanged after — an in-flight
# fill that started before an invalidation must not repopulate the key with
# pre-invalidation rows.
_MARKET_CACHE_GEN: dict[_MarketKey, int] = {}
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


def _market_cache_put(
    key: _MarketKey, value: list[dict[str, Any]], now_mono: float, gen: int
) -> None:
    """Insert into the browse cache, dropping expired entries and — if still
    over _MARKET_CACHE_MAX — evicting the oldest by timestamp. Mirrors
    _lb_cache_put's shape (leaderboard cache) for the same reasons. `gen` is
    the key's _MARKET_CACHE_GEN captured before the rows were computed; the
    insert is skipped if an invalidation bumped it since."""
    with _MARKET_CACHE_LOCK:
        if _MARKET_CACHE_GEN.get(key, 0) != gen:
            return
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

    # Trait listings are only transactable when the trait economy is enabled on
    # this chain (ECONOMY_NETWORK == XRPL_NETWORK). With it off, surfacing trait
    # rows on this public browse would advertise listings no one can actually
    # buy (buy would 403 economy_disabled), so serve an empty page instead of a
    # hard 403 — the character surface is unaffected. See CLAUDE.md's seam note.
    # Runs AFTER param validation (#130): a malformed query is a caller error
    # (400) whichever way the flag is set — a 200-empty would mask broken
    # clients while the economy is off, then flip to 400 the day it turns on.
    if kind == "trait" and not config.ECONOMY_ENABLED:
        return web.json_response({"rows": [], "total": 0})

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
    with _MARKET_CACHE_LOCK:
        cached = _MARKET_CACHE.get(cache_key)
        gen = _MARKET_CACHE_GEN.get(cache_key, 0)
    if cached is not None and now_mono - cached[0] < _MARKET_CACHE_TTL:
        rows = cached[1]
    else:
        rows = await asyncio.get_event_loop().run_in_executor(
            None, _compute_market_rows, network, kind
        )
        _market_cache_put(cache_key, rows, now_mono, gen)

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


def _invalidate_market_cache(network: str, kind: str | None = None) -> None:
    """Drop the browse cache for a (network, kind) whose listing set just
    changed, so the caller's own List/Cancel/Buy is visible immediately
    instead of after _MARKET_CACHE_TTL. kind=None drops both kinds (the
    close path doesn't know the listing's kind)."""
    with _MARKET_CACHE_LOCK:
        for k in ("character", "trait") if kind is None else (kind,):
            _MARKET_CACHE.pop((network, k), None)
            _MARKET_CACHE_GEN[(network, k)] = _MARKET_CACHE_GEN.get((network, k), 0) + 1


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
    _invalidate_market_cache(network, row.get("kind"))


def _close_listing_sync(
    network: str, offer_index: str, reason: str, buyer: str | None = None
) -> None:
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        market_store.init_db(conn)
        market_store.close_listing(conn, offer_index, reason, buyer=buyer)
    finally:
        conn.close()
    _invalidate_market_cache(network)


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
        platform=memos.platform_for_surface(_platform(user)),
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
    session.push = payload.get("push")
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
        platform=memos.platform_for_surface(_platform(user)),
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
    session.push = payload.get("push")
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
        platform=memos.platform_for_surface(_platform(user)),
        action=memos.ACTION_BUY,
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
    session.push = payload.get("push")
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
        try:
            await _advance_market_session(prefix, session, loop)
        finally:
            # Persist a token the advance captured even when a downstream
            # DB write/settlement raised — the capture is already real.
            await _persist_issued_user_token(request["user"], session)
        return web.json_response(session.to_dict())

    return require_market(handler)


async def _advance_market_session(prefix: str, session: Any, loop: Any) -> None:
    """One poll step for a list/cancel/buy session: advance the state machine
    and apply its DB effects (split out of the status handler so the handler
    can persist a captured push token in a finally regardless of outcome)."""
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


handle_market_list_status = _make_market_status_handler("list")
handle_market_cancel_status = _make_market_status_handler("cancel")
handle_market_buy_status = _make_market_status_handler("buy")


# --- Trait Shop (#217) Task 8: catalog + buy service endpoints ---
# GET /api/shop/catalog is a public, cached, derived-price browse (lfg_core.shop
# .catalog). POST/GET /api/shop/buy drive lfg_core.shop_flow's ShopBuySession
# state machine (mint -> BRIX sell offer -> XUMM accept -> settle-to-Closet),
# mirroring the market buy session shape above but keyed by wallet (`.buyer`)
# rather than discord_id, since ShopBuySession has no platform-user identity.

_SHOP_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SHOP_CACHE_LOCK = threading.Lock()
_SHOP_CACHE_GEN: dict[str, int] = {}
_SHOP_CACHE_TTL = 60.0


def _shop_cache_put(network: str, value: list[dict[str, Any]], now_mono: float, gen: int) -> None:
    """Mirrors _market_cache_put's insert-if-generation-unchanged discipline,
    keyed on network only (the catalog has no `kind` dimension)."""
    with _SHOP_CACHE_LOCK:
        if _SHOP_CACHE_GEN.get(network, 0) != gen:
            return
        for k in [k for k, (ts, _) in _SHOP_CACHE.items() if now_mono - ts >= _SHOP_CACHE_TTL]:
            del _SHOP_CACHE[k]
        _SHOP_CACHE[network] = (now_mono, value)


def _invalidate_shop_cache(network: str) -> None:
    with _SHOP_CACHE_LOCK:
        _SHOP_CACHE.pop(network, None)
        _SHOP_CACHE_GEN[network] = _SHOP_CACHE_GEN.get(network, 0) + 1


def _compute_shop_catalog(network: str) -> list[dict[str, Any]]:
    """Sync work for GET /api/shop/catalog, run on an executor thread (same
    posture as _compute_market_rows): the derived catalog from the app DB
    (trait_rarity + shop_overrides), each row annotated with a representative
    trait-art URL via the same helper the marketplace trait listings use."""
    conn = sqlite3.connect(db_path.app_db_path(network))
    try:
        rarity.ensure_schema(conn)
        rows = shop.catalog(conn, network)
    finally:
        conn.close()
    cfg = trait_config.get_config()
    for row in rows:
        row["image_url"] = _trait_image_url(cfg, row["slot"], row["value"])
    return rows


async def handle_shop_catalog(request: web.Request) -> web.Response:
    """Public: GET /api/shop/catalog. Empty list when the trait economy is
    disabled (mirrors handle_market_listings' empty-page-not-403 posture for
    a browse-only, non-transacting surface)."""
    if not config.ECONOMY_ENABLED:
        return web.json_response({"items": []})

    network = config.ECONOMY_NETWORK
    now_mono = time.monotonic()
    with _SHOP_CACHE_LOCK:
        cached = _SHOP_CACHE.get(network)
        gen = _SHOP_CACHE_GEN.get(network, 0)
    if cached is not None and now_mono - cached[0] < _SHOP_CACHE_TTL:
        rows = cached[1]
    else:
        rows = await asyncio.get_event_loop().run_in_executor(None, _compute_shop_catalog, network)
        _shop_cache_put(network, rows, now_mono, gen)
    return web.json_response({"items": rows})


def _quote_shop_trait(network: str, slot: str, value: str) -> tuple[int | None, bool]:
    """(price, exists) for one (slot, value): price is shop.quote's result
    (None if unknown / rarity-disabled / excluded / a price_override of None);
    exists distinguishes "no trait_rarity rows at all" (404 unknown_trait)
    from "rows exist but this value isn't currently purchasable" (403
    not_purchasable) for the caller."""
    conn = sqlite3.connect(db_path.app_db_path(network))
    try:
        rarity.ensure_schema(conn)
        exists = (
            conn.execute(
                "SELECT 1 FROM trait_rarity WHERE network=? AND category=? AND trait=? LIMIT 1",
                (network, slot, value),
            ).fetchone()
            is not None
        )
        price = shop.quote(conn, network, slot, value)
        return price, exists
    finally:
        conn.close()


def _build_shop_deps(network: str, platform: str) -> tuple[shop_flow.ShopDeps, sqlite3.Connection]:
    """The real ShopDeps for a service-triggered buy: mint/offer/burn against
    the live issuer wallet, XUMM payload builders, and an EconomyDeps built
    the same way `economy_api.build_settlement_deps` wires settlement (shop
    buys settle into the buyer's Closet via the same `run_deposit`). Callers
    own the returned connection's lifetime (close it when done)."""
    conn = nft_index.init_db(nft_index.index_db_path(network))
    economy_store.init_economy_schema(conn)
    shop_store.ensure_schema(conn)
    economy_deps = economy_api.build_settlement_deps(conn)

    def _app_conn_factory() -> sqlite3.Connection:
        return sqlite3.connect(db_path.app_db_path(network))

    async def _accept_payload_fn(offer_index: str, user_token: str | None = None) -> dict[str, Any]:
        # ShopDeps.accept_payload_fn is typed non-Optional (shop_flow assigns
        # its result straight to session.accept with no None-check); a Xaman
        # API failure here raises into start_shop_buy's own outer try/except,
        # which fails the session/order the same way an offer-build failure
        # does — never a silent None assignment.
        payload = await xumm_ops.create_accept_offer_payload(
            offer_index,
            user_token=user_token,
            platform=memos.platform_for_surface(platform),
            action=memos.ACTION_SHOP_BUY,
        )
        if payload is None:
            raise RuntimeError("could not reach Xaman")
        return payload

    deps = shop_flow.ShopDeps(
        conn=conn,
        app_conn_factory=_app_conn_factory,
        economy_deps=economy_deps,
        mint_fn=lambda url, taxon, **kw: xrpl_ops.mint_nft(
            url, taxon, config.SWAP_ISSUER_ADDRESS, **kw
        ),
        offer_fn=xrpl_ops.create_nft_offer,
        burn_fn=lambda nft_id, owner: xrpl_ops.burn_nft(nft_id, owner or None),
        payload_status_fn=xumm_ops.get_payload_status,
        accept_payload_fn=_accept_payload_fn,
        network=network,
        # #238: post-settlement AMM buyback on the XRP path — BRIX
        # currency/issuer baked in; shop_flow calls it as (value, max_xrp=...).
        buy_and_burn_fn=lambda value, max_xrp=None: xrpl_ops.buy_and_burn(
            config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER, value, max_xrp=max_xrp
        ),
    )
    return deps, conn


def _prune_shop_sessions() -> None:
    cutoff = time.time() - SESSION_RETENTION
    terminal = {shop_flow.DONE, shop_flow.FAILED}
    for sid, s in list(shop_sessions.items()):
        if s.state in terminal and _shop_session_created.get(sid, 0) < cutoff:
            del shop_sessions[sid]
            _shop_session_created.pop(sid, None)


@require_wallet
async def handle_shop_buy_start(request):
    """POST /api/shop/buy {slot, value}. Fail-closed order: 403
    economy_disabled -> 400 malformed -> 404 unknown_trait / 403
    not_purchasable -> 403 closet_required -> launch. The price is frozen
    from shop.quote at this moment and carried on the session; the mint +
    BRIX sell-offer + XUMM payload build (shop_flow.start_shop_buy) runs as a
    background task (mirrors webapp.economy_api._schedule) so the request
    returns immediately with a `running` session for the client to poll via
    GET /api/shop/buy/{session_id} — the same shape returned once the
    background task fills in `accept`."""
    if not config.ECONOMY_ENABLED:
        return _economy_disabled_response()

    user = request["user"]
    wallet = request["wallet"]
    body = await request.json()
    slot = body.get("slot")
    value = body.get("value")
    if not isinstance(slot, str) or not slot or not isinstance(value, str) or not value:
        return web.json_response({"error": "slot and value are required"}, status=400)

    network = config.ECONOMY_NETWORK
    loop = asyncio.get_event_loop()
    price, exists = await loop.run_in_executor(None, _quote_shop_trait, network, slot, value)
    if price is None:
        if not exists:
            return web.json_response({"error": "unknown_trait"}, status=404)
        return web.json_response({"error": "not_purchasable"}, status=403)

    active = await loop.run_in_executor(None, _closet_active, network, wallet)
    if not active:
        return web.json_response({"error": "closet_required"}, status=403)

    _prune_shop_sessions()
    for s in shop_sessions.values():
        if s.buyer == wallet and s.state not in shop_flow.TERMINAL_STATES:
            return web.json_response(
                {
                    "error": "a shop purchase is already in progress",
                    "code": "session_active",
                    "session_id": s.id,
                    "session": s.to_dict(),
                },
                status=409,
            )

    # #238 XRP fallback: silent payment-path detection against the frozen BRIX
    # price, BEFORE the session exists (nothing is minted for an unquotable
    # price). BRIX holders keep today's flow byte-identically; everyone else
    # gets an XRP-denominated offer at the buffered AMM quote.
    try:
        pay_with, pay_amount = await brix_payment.detect_payment_path(wallet, str(price))
    except RuntimeError:
        return web.json_response(
            {"error": "pricing unavailable", "code": "pricing_unavailable"}, status=503
        )
    price_xrp = pay_amount if pay_with == "XRP" else None

    session = shop_flow.ShopBuySession(
        buyer=wallet,
        slot=slot,
        value=value,
        price_brix=price,
        pay_with=pay_with,
        price_xrp=price_xrp,
        platform=_platform(user),
        push_user_token=await _push_token(user),
    )
    shop_sessions[session.id] = session
    _shop_session_created[session.id] = time.time()

    deps, conn = _build_shop_deps(network, _platform(user))

    async def _run_and_close() -> None:
        try:
            await shop_flow.start_shop_buy(session, deps)
        except Exception as e:  # unexpected crash: ensure the session reaches a terminal state
            if session.state != shop_flow.FAILED:
                session.fail(f"internal error: {e}")
        finally:
            conn.close()

    asyncio.get_event_loop().create_task(_run_and_close())
    return web.json_response(session.to_dict())


@require_wallet
async def handle_shop_buy_status(request):
    """GET /api/shop/buy/{session_id}: 404 if unknown or the session belongs
    to a different wallet (ShopBuySession has no platform-user identity to
    compare, so ownership is checked by `.buyer` wallet — the same identity
    require_wallet resolved for the POST that created it). Drives
    shop_flow.advance_shop_buy while awaiting the buyer's XUMM signature or
    mid-settlement; a session that hasn't reached AWAITING_ACCEPT yet (the
    background mint/offer build from the POST is still running) is returned
    as-is with nothing to advance."""
    session = shop_sessions.get(request.match_info["session_id"])
    if session is None or session.buyer != request["wallet"]:
        return web.json_response({"error": "not found"}, status=404)

    if session.state in (shop_flow.AWAITING_ACCEPT, shop_flow.SETTLING):
        network = config.ECONOMY_NETWORK
        deps, conn = _build_shop_deps(network, session.platform)
        try:
            await shop_flow.advance_shop_buy(session, deps)
        finally:
            conn.close()

    return web.json_response(session.to_dict())


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


# Trait Shop sweep (#217): backstop for the buy flow in shop_flow.py, mirrors
# the trait-sale settlement sweep above (same giveup-journal convention).
_SHOP_SWEEP_MAX_ATTEMPTS = 5
# session_id -> consecutive failed settlement sweep attempts. In-memory only,
# same rationale as _sweep_attempts: a restart just costs a stuck order a few
# retries rather than falsely reading as "already exhausted".
_shop_settle_attempts: dict[str, int] = {}


def _write_shop_sweep_giveup_record(session_id: str, nft_id: str, buyer: str) -> None:
    """Journal (ECONOMY_RECORDS_DIR) that the shop settlement sweep is no
    longer retrying this order. The trait token is NOT lost: in the normal
    case it is an ordinary trait token sitting in `buyer`'s wallet and they
    can Deposit it into their Closet manually later; if settlement has been
    failing because a prior expiry attempt hit a transient burn error, the
    token may instead still be issuer-held (never delivered) — either way it
    requires manual reconciliation, not a re-burn."""
    try:
        os.makedirs(config.ECONOMY_RECORDS_DIR, exist_ok=True)
        path = os.path.join(config.ECONOMY_RECORDS_DIR, f"shop-settlement-giveup-{session_id}.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "session_id": session_id,
                    "nft_id": nft_id,
                    "buyer": buyer,
                    "attempts": _SHOP_SWEEP_MAX_ATTEMPTS,
                    "status": "abandoned",
                },
                f,
                indent=2,
            )
    except Exception:
        logging.error(
            f"failed to write shop sweep giveup record for {session_id}: {traceback.format_exc()}"
        )


def _write_shop_expiry_reversal_giveup_record(
    session_id: str, slot: str, value: str, delta: int, reason: str
) -> None:
    """Journal (ECONOMY_RECORDS_DIR) an intended `supply_changes` reversal row
    that failed to write after a successful expiry burn. The burn is real and
    irreversible — the order is still closed `expired` so the sweep never
    re-touches it — but the -1 supply row itself never landed, so an admin
    must re-apply it manually from this record to keep the conservation
    ledger accurate."""
    try:
        os.makedirs(config.ECONOMY_RECORDS_DIR, exist_ok=True)
        path = os.path.join(
            config.ECONOMY_RECORDS_DIR, f"shop-expiry-reversal-giveup-{session_id}.json"
        )
        with open(path, "w") as f:
            json.dump(
                {
                    "session_id": session_id,
                    "kind": "burn",
                    "slot": slot,
                    "value": value,
                    "delta": delta,
                    "reason": reason,
                    "status": "needs_admin_reapply",
                },
                f,
                indent=2,
            )
    except Exception:
        logging.error(
            f"failed to write shop expiry reversal giveup record for {session_id}: "
            f"{traceback.format_exc()}"
        )


async def _expire_shop_order(order: dict[str, Any], network: str) -> None:
    """Expire one stale `pending_accept` Trait Shop order: cancel the orphaned
    sell offer (best-effort — an already-gone offer is not an error, the
    expiration alone already made it unacceptable), then issuer-burn the
    unclaimed trait token and record the matching `supply_changes` reversal.

    Fail-closed rescue: if the burn does not definitively succeed — most
    likely because the buyer's accept actually landed moments before the
    sweep ran, moving the token out of the issuer wallet — the order is
    marked `accepted` instead of `expired` so the settlement pass picks it up
    next. A token the buyer paid for must never be burned."""
    session_id = order["session_id"]
    offer_index = order.get("offer_index")
    nft_id = order.get("nft_id")
    now_ts = int(time.time())

    if offer_index:
        try:
            await xrpl_ops.cancel_nft_offer(offer_index)
        except Exception:
            logging.warning(
                f"shop expiry: offer cancel failed for {session_id}: {traceback.format_exc()}"
            )

    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        economy_store.init_economy_schema(conn)
        if not nft_id:
            # Nothing was ever minted for this order (e.g. it stalled before
            # the mint completed) — nothing to burn, just close it out.
            shop_store.update_order(conn, session_id, now_ts=now_ts, status="expired")
            return

        try:
            burn_hash = await xrpl_ops.burn_nft(nft_id)
        except Exception:
            logging.error(f"shop expiry burn crashed for {session_id}: {traceback.format_exc()}")
            burn_hash = None

        if not burn_hash:
            # Could not confirm the issuer still holds the token (most likely
            # a landed accept) — rescue rather than risk burning a sold
            # token. The settlement sweep retries it as an accepted order.
            shop_store.update_order(conn, session_id, now_ts=now_ts, status="accepted")
            return

        reason = f"shop expiry {session_id}"
        try:
            economy_store.record_supply_change(
                conn,
                kind="burn",
                edition=None,
                body_value="",
                body_class="",
                trait_deltas={f"{order['slot']}|{order['value']}": -1},
                actor="shop",
                reason=reason,
            )
        except Exception:
            logging.exception(
                f"shop expiry {session_id}: burn succeeded (nft_id={nft_id}) but the "
                f"supply reversal row failed to write for slot={order.get('slot')} "
                f"value={order.get('value')} — ledger and supply mirror are now out of "
                "sync; journaling for admin re-apply"
            )
            _write_shop_expiry_reversal_giveup_record(
                session_id, order.get("slot", ""), order.get("value", ""), -1, reason
            )
        # The token is burned regardless of whether the reversal row landed —
        # the order must never be re-swept (a second burn attempt would find
        # nothing and the rescue rule would misroute it to `accepted`).
        shop_store.update_order(conn, session_id, now_ts=now_ts, status="expired")
    finally:
        conn.close()


async def _settle_shop_order(order: dict[str, Any], network: str) -> None:
    """Retry settlement (run_deposit into the buyer's Closet + the shop_count
    pricing bump) for one `accepted` Trait Shop order. Mirrors
    `_settle_trait_sale`: on success -> `settled`; after
    `_SHOP_SWEEP_MAX_ATTEMPTS` consecutive failures -> journal + `failed`
    (the token is never lost — it stays wherever it last landed, in the
    buyer's wallet for a manual Deposit or issuer-held if a prior expiry
    attempt hit a transient burn error)."""
    session_id = order["session_id"]
    if _shop_settle_attempts.get(session_id, 0) >= _SHOP_SWEEP_MAX_ATTEMPTS:
        return  # already given up + journaled on a previous pass
    nft_id = order.get("nft_id")
    buyer = order["buyer"]
    now_ts = int(time.time())
    if not nft_id:
        return  # nothing minted; not a settleable order

    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        economy_store.init_economy_schema(conn)
        deps = economy_api.build_settlement_deps(conn)
        dep_session = economy_flow.DepositSession(owner=buyer, nft_id=nft_id)
        try:
            await economy_flow.run_deposit(dep_session, deps)
            settled = dep_session.state == economy_flow.DONE
        except Exception:
            logging.error(
                f"shop settlement sweep crashed for {session_id}: {traceback.format_exc()}"
            )
            settled = False
    finally:
        conn.close()

    if settled:
        conn = nft_index.init_db(nft_index.index_db_path(network))
        try:
            shop_store.update_order(conn, session_id, now_ts=now_ts, status="settled")
            # #238: the sweep's settlement-retry path owes the same one-shot
            # post-settlement AMM buyback the poll path fires — the shared
            # run_buyback_if_due is best-effort, exception-swallowing (incl.
            # the buyback_done flag write, so a failed write can never
            # propagate and leave the burn re-armed), and guarded by the
            # durable buyback_done flag so poll + sweep can never double-fire.
            await shop_flow.run_buyback_if_due(
                conn,
                session_id=session_id,
                pay_with=order.get("pay_with"),
                price_brix=order["price_brix"],
                price_xrp=order.get("price_xrp"),
                buyback_done=order.get("buyback_done"),
                buy_and_burn_fn=lambda value, max_xrp=None: xrpl_ops.buy_and_burn(
                    config.SWAP_OFFER_CURRENCY_HEX,
                    config.SWAP_OFFER_ISSUER,
                    value,
                    max_xrp=max_xrp,
                ),
                now_ts_fn=lambda: now_ts,
            )
        finally:
            conn.close()
        try:
            app_conn = sqlite3.connect(db_path.app_db_path(network))
            try:
                rarity.increment_shop_count(app_conn, network, order["slot"], order["value"])
            finally:
                app_conn.close()
        except Exception:
            logging.warning(
                f"shop settlement sweep: shop_count increment failed for {session_id} "
                f"(order settled; pricing feedback skipped): {traceback.format_exc()}"
            )
        _shop_settle_attempts.pop(session_id, None)
        return

    _shop_settle_attempts[session_id] = _shop_settle_attempts.get(session_id, 0) + 1
    if _shop_settle_attempts[session_id] >= _SHOP_SWEEP_MAX_ATTEMPTS:
        logging.warning(
            f"shop settlement sweep giving up on {session_id} (nft {nft_id}, buyer "
            f"{buyer}) after {_SHOP_SWEEP_MAX_ATTEMPTS} attempts"
        )
        _write_shop_sweep_giveup_record(session_id, nft_id, buyer)
        conn = nft_index.init_db(nft_index.index_db_path(network))
        try:
            shop_store.update_order(conn, session_id, now_ts=now_ts, status="failed")
        finally:
            conn.close()


async def sweep_shop_orders() -> None:
    """Backstop for the Trait Shop buy flow (shop_flow.py): expire stale
    unaccepted offers (burn back + supply reversal) and retry settlement for
    accepted orders that stalled after the buyer signed. Runs on the trait
    economy network (config.ECONOMY_NETWORK), same as the trait-sale
    settlement sweep."""
    network = config.ECONOMY_NETWORK
    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        shop_store.ensure_schema(conn)
        cutoff = int(time.time()) - config.SHOP_OFFER_TTL_SECONDS
        expiring = shop_store.orders_pending_expiry(conn, cutoff)
    finally:
        conn.close()

    for order in expiring:
        try:
            await _expire_shop_order(order, network)
        except Exception:
            logging.error(
                f"shop expiry sweep crashed for {order['session_id']}: {traceback.format_exc()}"
            )

    conn = nft_index.init_db(nft_index.index_db_path(network))
    try:
        shop_store.ensure_schema(conn)
        unsettled = shop_store.orders_unsettled(conn)
    finally:
        conn.close()

    for order in unsettled:
        try:
            await _settle_shop_order(order, network)
        except Exception:
            logging.error(
                f"shop settlement sweep crashed for {order['session_id']}: {traceback.format_exc()}"
            )


async def _settlement_sweep_loop() -> None:
    while True:
        try:
            await settle_pending_trait_sales()
        except Exception:
            logging.error(f"settlement sweep loop crashed: {traceback.format_exc()}")
        try:
            await sweep_shop_orders()
        except Exception:
            logging.error(f"shop sweep loop crashed: {traceback.format_exc()}")
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

    push_user_token = await _push_token(user)
    try:
        extract_ws = await economy_api.start_extract(
            user["id"], wallet, {"slot": slot, "value": value}, user_token=push_user_token
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
        push_user_token=push_user_token,
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

    try:
        row = await market_flow.advance_trait_sell_session(session)
        if row is not None:
            network = _market_network("trait")
            await asyncio.get_event_loop().run_in_executor(None, _write_listing_row, network, row)
    finally:
        # Persist a token the advance captured even when the listing write
        # raised — the capture is already real.
        await _persist_issued_user_token(request["user"], session)
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
        result = await economy_api.start_closet(
            user["id"], request["wallet"], user_token=await _push_token(user)
        )
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
            closet_result = await economy_api.start_closet(
                user["id"], wallet, user_token=await _push_token(user)
            )
        except Exception as e:
            logging.warning(f"post-register ensure_closet failed for {wallet}: {e}")
    resp: dict[str, Any] = {"ok": True, "wallet": wallet}
    if closet_result is not None:
        resp["closet_accept"] = closet_result.get("accept")
        resp["closet_accept_push"] = closet_result.get("accept_push")
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
    # Keep the task handle so /cancel can stop the payment wait (#141)
    session.task = asyncio.create_task(mint_flow.run_mint_session(session))
    return web.json_response(session.to_dict())


@require_wallet
async def handle_bulk_mint_start(request):
    """Start a bulk mint job (#215): one K x payment, then a background task
    mints K units in sequence. Mirrors handle_mint_start's ordering — every
    suspending value (request body parse, push token, return URL) is resolved
    BEFORE the one-active-job check so no await sits between the check and
    the insert below (the guard is only race-free while that window stays
    await-free). prepare_payment() is deliberately awaited AFTER the insert —
    a concurrent request already sees this job as active by then."""
    user = request["user"]
    _prune_sessions(bulk_sessions, bulk_mint_flow.TERMINAL_STATES)

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_qty = body.get("quantity")
    # Reject bool/float/string quantities: int(True) == 1, int(1.5) == 1, and
    # int("3") == 3 would all silently coerce into a "valid" request. Only a
    # real (non-bool) int is accepted.
    if not isinstance(raw_qty, int) or isinstance(raw_qty, bool):
        return web.json_response({"error": "invalid_quantity"}, status=400)
    qty = raw_qty

    return_url = await _request_return_url(request)
    push_user_token = await _push_token(user)

    if qty < 1:
        return web.json_response({"error": "invalid_quantity"}, status=400)

    job = bulk_mint_flow.BulkMintJob(
        discord_id=user["id"],
        wallet_address=request["wallet"],
        requested_qty=qty,
        platform=_platform(user),
        push_user_token=push_user_token,
        return_url=return_url,
    )
    try:
        job.clamp_to_headroom()
    except bulk_mint_flow.CollectionFull:
        return web.json_response({"error": "collection_full"}, status=409)

    # One active job per user (no awaits between this check and the insert
    # below, so it cannot race)
    active = _active_session(
        bulk_sessions, bulk_mint_flow.TERMINAL_STATES, user["id"], _platform(user)
    )
    if active:
        return web.json_response(
            {"error": "bulk mint already in progress", "session": active.to_dict()}, status=409
        )
    bulk_sessions[job.id] = job

    try:
        await asyncio.wait_for(job.prepare_payment(), timeout=8)
    except Exception as e:
        # Never leave a non-terminal job with no task in bulk_sessions — that
        # would permanently wedge this user's bulk slot (every future POST
        # would 409 "already in progress" until a service restart). Mark it
        # terminal so _prune_sessions evicts it and _active_session skips it.
        # Covers both a hung XUMM call (asyncio.TimeoutError) and any other
        # failure -- bulk has no ensure_payment_fallback like single-mint, so
        # marking FAILED and letting the user retry is the correct behavior.
        logging.error(f"bulk job {job.id} prepare_payment failed: {e}")
        job.state = bulk_mint_flow.FAILED
        job.error = str(e)
        return web.json_response({"error": "payment_setup_failed"}, status=500)

    job.task = asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))
    return web.json_response(job.to_dict())


@require_auth
async def handle_bulk_mint_status(request):
    job = bulk_sessions.get(request.match_info["session_id"])
    if (
        not job
        or job.discord_id != request["user"]["id"]
        or getattr(job, "platform", "discord") != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(job.to_dict())


@require_auth
async def handle_bulk_mint_active(request):
    """The caller's live (non-terminal) bulk job, or null. Kept SEPARATE from
    /api/mint/active: that endpoint serves the existing single-mint
    resumeMint() client, which has no knowledge of the bulk job shape
    (units[] etc.) — returning a bulk job there would break it."""
    user = request["user"]
    job = _active_session(
        bulk_sessions, bulk_mint_flow.TERMINAL_STATES, user["id"], _platform(user)
    )
    return web.json_response({"session": job.to_dict() if job else None})


@require_auth
async def handle_bulk_mint_cancel(request):
    """Back out of the bulk pay screen (mirrors handle_mint_cancel/#141): only
    legal while AWAITING_PAYMENT — once paid, fulfillment must run to
    completion. Cancelling frees the per-user bulk slot immediately."""
    job = bulk_sessions.get(request.match_info["session_id"])
    if (
        not job
        or job.discord_id != request["user"]["id"]
        or getattr(job, "platform", "discord") != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    if job.state in bulk_mint_flow.TERMINAL_STATES:
        return web.json_response(job.to_dict())  # already over — no-op
    if not job.cancel():
        return web.json_response({"error": "job is past payment"}, status=409)
    job.mark_published()
    return web.json_response(job.to_dict())


async def resume_bulk_jobs() -> None:
    """On startup, re-attach and resume any paid/fulfilling bulk jobs so a
    service restart mid-fulfillment doesn't strand paid units."""
    for job in bulk_mint_flow.load_all_resumable():
        bulk_sessions[job.id] = job
        job.task = asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))


async def _start_bulk_resume(app: web.Application) -> None:
    """aiohttp on_startup hook: schedule resume_bulk_jobs as a background task
    (mirrors _start_settlement_sweep) so app startup doesn't block on it."""
    app["bulk_resume_task"] = asyncio.get_event_loop().create_task(resume_bulk_jobs())


async def _stop_bulk_resume(app: web.Application) -> None:
    """aiohttp on_cleanup hook: cancel the startup bulk-resume task on
    shutdown (mirrors _stop_settlement_sweep)."""
    task = app.get("bulk_resume_task")
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _index_roster(conn: sqlite3.Connection, wallet: str) -> list[dict[str, Any]] | None:
    """Normalized roster records built ENTIRELY from local data: the wallet's
    live index rows plus the uri metadata cache. No network, ever — an inline
    gateway fetch for a cache miss used to stall the whole roster for the
    full 20s fetch_metadata timeout per permanently-unreadable token (one
    such token held a 225-NFT wallet hostage on every load).

    Returns None when the index holds NO rows for this wallet — only then
    may the caller fall back to the live ledger (the #162 partial-index
    guarantee). A wallet whose rows all get skipped below returns [] and is
    trusted: those tokens are unreadable everywhere (the multi-gateway
    backfill failed too), so the ledger path could only re-enter the slow
    remote fetch to show nothing (Greptile P1 on #165).

    Cache hit → the token's real metadata (most faithful; carries burnCount
    for swap outputs — the listener warms the cache at index time). Miss on a
    readable row → synthesize the metadata from the row itself: the listener/
    backfill already parsed name→edition, attributes and image into it, and
    the collection name pattern is deterministic. Miss on an UNREADABLE row
    (no edition number — the multi-gateway backfill couldn't fetch it either)
    → skip: normalize_nft would reject it regardless."""
    recs = nft_index.owner_live_nfts(conn, wallet)
    if not recs:
        return None
    cached = nft_index.meta_cache_get_many(conn, [r.uri_hex for r in recs if r.uri_hex])
    nfts = []
    for rec in recs:
        meta = cached.get(rec.uri_hex)
        if meta is None:
            if rec.nft_number is None:
                continue
            meta = {
                "name": f"{config.NFT_COLLECTION_NAME} #{rec.nft_number}",
                "image": rec.image,
                "attributes": rec.attributes,
            }
        flags = nft_index.to_token(rec)["flags"]
        try:
            record = swap_meta.normalize_nft(rec.nft_id, meta, flags=flags, uri_hex=rec.uri_hex)
        except Exception as e:
            logging.warning(f"Skipping NFT {rec.nft_id}: bad metadata ({e})")
            continue
        if record:
            nfts.append(record)
    nfts.sort(key=lambda n: n["number"])
    return nfts


async def _wallet_nfts(wallet: str) -> list[dict[str, Any]]:
    """The swapper roster, from LOCAL data (the listener-fresh on-chain index
    + the uri_hex metadata cache — see _index_roster). #153/#162: the remote
    path used to be the default, and a flaky gateway blanked tiles, silently
    dropped NFTs, and stalled loads.

    The live account_nfts ledger call survives as the fallback whenever the
    index yields NOTHING for this wallet — an unbuilt or partially backfilled
    index must not silently hide holdings (Greptile P1 on #162). Any wallet
    with index rows is served locally; the fallback therefore only costs a
    ledger round-trip for genuinely-empty wallets, and if that fallback
    itself fails the empty local answer stands (an empty roster beats a 502
    when the public node is down). Only this cold path may fetch metadata
    remotely (misses are then cached forever — the URI is content-addressed)."""
    conn = None
    cache = None
    roster: list[dict[str, Any]] | None = None
    index_ok = False
    try:
        conn = nft_index.init_db(nft_index.index_db_path(config.XRPL_NETWORK))
        cache = nft_index.UriMetadataCache(conn)
    except Exception as e:
        logging.warning(f"uri metadata cache unavailable: {e}")
    if conn is not None:
        try:
            roster = _index_roster(conn, wallet)
            index_ok = True
        except Exception as e:
            logging.warning(f"on-chain index roster failed, falling back to ledger: {e!r}")
    try:
        if roster is not None:
            return roster
        try:
            return await swap_meta.load_wallet_nfts(
                wallet, xrpl_ops.get_account_nfts, meta_cache=cache
            )
        except Exception:
            if index_ok:
                logging.warning(f"ledger fallback failed for {wallet}; trusting empty index result")
                return []
            raise
    finally:
        if conn is not None:
            conn.close()


# The fee quote is the roster's one remaining live-ledger touch (BRIX balance
# and the AMM rate exist in no local store). Bound it so a hung public node
# degrades the cost line to "unknown" instead of stalling the whole roster.
_SWAP_FEE_QUOTE_TIMEOUT = 4.0


async def _swap_fee_quote(wallet: str) -> dict[str, Any] | None:
    """Advisory swap-fee quote for the roster's cost line (BRIX holders pay
    BRIX; everyone else the AMM XRP equivalent), or None if it can't be
    quoted in time. The swap session re-detects the path server-side when
    the fee is actually charged."""
    try:
        pay_with, amount = await asyncio.wait_for(
            swap_flow.detect_swap_payment(wallet, swap_flow.swap_fee_total(2)),
            timeout=_SWAP_FEE_QUOTE_TIMEOUT,
        )
        return {"pay_with": pay_with, "amount": amount, "per_nft": swap_flow.swap_fee_total(1)}
    except Exception as e:
        logging.warning(f"Swap fee quote failed: {e!r}")
        return None


@require_wallet
async def handle_nfts(request):
    """List the user's swappable collection NFTs (normalized metadata)."""
    try:
        nfts = await _wallet_nfts(request["wallet"])
    except Exception as e:
        # repr, not str: asyncio.TimeoutError stringifies to "" and left this
        # log line blank during the mainnet-cutover 502s.
        logging.error(f"NFT listing failed: {e!r}")
        return web.json_response({"error": "failed to load wallet NFTs"}, status=502)
    swap_fee = await _swap_fee_quote(request["wallet"])
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
        logging.info(
            "swap rejected (invalid trait selection): user=%s nft1=%s nft2=%s traits=%r",
            user["id"],
            nft1_id,
            nft2_id,
            traits_to_swap,
        )
        return web.json_response({"error": "invalid trait selection"}, status=400)

    _prune_sessions(swap_sessions, swap_flow.TERMINAL_STATES)
    if _active_session(swap_sessions, swap_flow.TERMINAL_STATES, user["id"], _platform(user)):
        return web.json_response({"error": "swap already in progress"}, status=409)

    # Re-verify ownership and metadata server-side (never trust client data)
    try:
        nfts = await _wallet_nfts(request["wallet"])
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
        logging.info(
            "swap rejected (cross-body matrix): user=%s nft1=%s (%s) nft2=%s (%s) blocked=%r",
            user["id"],
            nft1_id,
            nft1["gender"],
            nft2_id,
            nft2["gender"],
            blocked,
        )
        return web.json_response(
            {
                "error": (
                    f"trait(s) {', '.join(blocked)} cannot swap between "
                    f"{nft1['gender']} and {nft2['gender']} bodies"
                )
            },
            status=400,
        )
    # 'None' is a real, expected trait value (shirtless/bald/no-accessory), so a
    # one-sided empty slot IS swappable — moving it onto the partner sends the
    # partner's value back the other way (a legitimate exchange, not a deletion).
    # Only a slot empty on BOTH NFTs is a no-op: drop those, and reject the swap
    # only if nothing with real work to do remains.
    noop = swap_meta.noop_swaps(nft1["attributes"], nft2["attributes"], traits_to_swap)
    if noop:
        logging.info(
            "swap: dropping no-op (empty on both NFTs) trait(s) %r: "
            "user=%s nft1=%s (%s) nft2=%s (%s)",
            noop,
            user["id"],
            nft1_id,
            nft1["gender"],
            nft2_id,
            nft2["gender"],
        )
        traits_to_swap = [t for t in traits_to_swap if t not in noop]
        if not traits_to_swap:
            logging.info(
                "swap rejected (all slots empty on both NFTs): "
                "user=%s nft1=%s (%s) nft2=%s (%s) noop=%r",
                user["id"],
                nft1_id,
                nft1["gender"],
                nft2_id,
                nft2["gender"],
                noop,
            )
            return web.json_response(
                {"error": (f"trait(s) {', '.join(noop)} are empty on both NFTs — nothing to swap")},
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
    # Keep the task handle so /cancel can stop the fee-payment wait
    # (mirror of mint #141).
    session.task = asyncio.get_event_loop().create_task(swap_flow.run_swap_session(session))
    return web.json_response(session.to_dict())


@require_auth
async def handle_mint_active(request):
    """The caller's live (non-terminal) mint session, or null. Discord mobile
    kills/reloads the Activity webview when the user app-switches to Xaman to
    sign the payment; the relaunched client has lost its in-memory session id,
    so it calls this on boot to re-attach to the mint still running here
    instead of dumping the user back to the home screen mid-mint."""
    user = request["user"]
    session = _active_session(mint_sessions, mint_flow.TERMINAL_STATES, user["id"], _platform(user))
    return web.json_response({"session": session.to_dict() if session else None})


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
    await _persist_issued_user_token(request["user"], session)
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


@require_auth
async def handle_mint_cancel(request):
    """Back out of the pay screen (issue #141): mark an awaiting_payment
    session terminal so the per-user mint lock releases immediately, and stop
    its background payment wait. Cancelling an already-terminal session is a
    safe no-op; a session past payment (money taken) returns 409."""
    session = mint_sessions.get(request.match_info["session_id"])
    if (
        not session
        or session.discord_id != request["user"]["id"]
        or getattr(session, "platform", "discord") != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    if session.state in mint_flow.TERMINAL_STATES:
        return web.json_response(session.to_dict())  # already over — no-op
    if not session.cancel():
        return web.json_response({"error": "session is past payment"}, status=409)
    # A deliberate cancel is not a mint outcome: suppress the terminal
    # mint.completed/mint.failed publish a late status poll would fire.
    session.mark_published()
    return web.json_response(session.to_dict())


def _first_result_image(session: Any) -> str | None:
    for r in getattr(session, "results", None) or []:
        img = r.get("image_url")
        if img:
            return str(img)
    return None


def _first_result_video(session: Any) -> str | None:
    for r in getattr(session, "results", None) or []:
        vid = r.get("video_url")
        if vid:
            return str(vid)
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
        video_url=_first_result_video(session),
        success_states={swap_flow.OFFERS_READY, swap_flow.DONE},
        fail_states={swap_flow.FAILED, swap_flow.PAYMENT_TIMEOUT},
    )
    return web.json_response(session.to_dict())


# Bound on the XUMM payload rebuild in handle_swap_regenerate (same 8s the
# mint start/regenerate paths use); module-level so tests can shrink it.
SWAP_REGEN_TIMEOUT = 8.0


def _swap_session_for(request):
    """The caller's swap session for the path's session_id, or None on any
    ownership/platform mismatch (identical guard to handle_swap_status)."""
    session = swap_sessions.get(request.match_info["session_id"])
    if (
        not session
        or session.discord_id != request["user"]["id"]
        or getattr(session, "platform", "discord") != _platform(request["user"])
    ):
        return None
    return session


@require_auth
async def handle_swap_regenerate(request):
    """Issue a fresh fee-payment QR for a swap whose XUMM payload expired
    before the user could scan it (mirror of mint issue #22 — the swap fee
    screen previously offered no way to refresh a stale QR)."""
    session = _swap_session_for(request)
    if not session:
        return web.json_response({"error": "not found"}, status=404)
    if session.state != swap_flow.AWAITING_PAYMENT:
        return web.json_response({"error": "session is past payment"}, status=409)
    # Unlike the mint pay screen (whose static-link fallback keeps its 200
    # honest), a swallowed failure here would echo the STALE link with a 200
    # and the button would appear dead — surface it instead.
    try:
        ok = await asyncio.wait_for(session.regenerate_payment(), timeout=SWAP_REGEN_TIMEOUT)
    except asyncio.TimeoutError:
        return web.json_response({"error": "payment QR regeneration timed out"}, status=504)
    except Exception as e:
        logging.warning(f"swap regenerate_payment failed: {e}")
        ok = False
    if not ok:
        return web.json_response({"error": "could not build a new payment QR"}, status=502)
    return web.json_response(session.to_dict())


@require_auth
async def handle_swap_cancel(request):
    """Back out of the swap fee screen (mirror of mint issue #141): mark an
    awaiting_payment session terminal so the per-user swap lock releases
    immediately, and stop its background payment wait. Cancelling an
    already-terminal session is a safe no-op; a session past payment (fee
    taken) returns 409."""
    session = _swap_session_for(request)
    if not session:
        return web.json_response({"error": "not found"}, status=404)
    if session.state in swap_flow.TERMINAL_STATES:
        return web.json_response(session.to_dict())  # already over — no-op
    if not session.cancel():
        return web.json_response({"error": "session is past payment"}, status=409)
    # A deliberate cancel is not a swap outcome: suppress the terminal
    # swap.completed/swap.failed publish a late status poll would fire.
    session.mark_published()
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


# --- Standalone web surface signin (spec 2026-07-16) -------------------------
# Client-callable (same trust posture as /api/telegram/auth): bootstraps a
# session where the wallet IS the identity — platform="web",
# platform_user_id=<classic address>. The payload uuid (128-bit, single-use,
# short-TTL) is the bearer secret; no pre-auth ownership check is possible,
# which is the same trust model as the XUMM deep link itself.

web_signin_payloads: dict[str, Any] = {}
WEB_SIGNIN_RATE_MAX = 5  # payload creations…
WEB_SIGNIN_RATE_WINDOW = 60.0  # …per IP per window (protects the XUMM API)
_web_signin_hits: dict[str, list[float]] = {}


def _client_ip(request) -> str:
    # The funnel / tailscale serve fronts the service, so the TCP peer is
    # localhost and the proxy APPENDS the real client to X-Forwarded-For.
    # Only the RIGHTMOST entry is trustworthy — leftmost values are caller-
    # controlled and would let a spoofer rotate fake IPs past the rate limit.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.remote or "?"


def _web_rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _web_signin_hits.get(ip, []) if now - t < WEB_SIGNIN_RATE_WINDOW]
    if len(hits) >= WEB_SIGNIN_RATE_MAX:
        _web_signin_hits[ip] = hits
        return True
    hits.append(now)
    _web_signin_hits[ip] = hits
    return False


def _prune_web_signin_payloads():
    now = time.time()
    cutoff = now - SIGNIN_TTL
    for uuid, rec in list(web_signin_payloads.items()):
        if rec["created_at"] < cutoff:
            del web_signin_payloads[uuid]
    # Rate-limit bookkeeping must not grow forever across distinct IPs.
    for ip, hits in list(_web_signin_hits.items()):
        if all(now - t >= WEB_SIGNIN_RATE_WINDOW for t in hits):
            del _web_signin_hits[ip]


async def handle_web_signin_start(request):
    """Create a XUMM SignIn payload for the standalone web surface — no session
    required (this IS how a web session begins)."""
    if _web_rate_limited(_client_ip(request)):
        return web.json_response(
            {"error": "too many sign-in attempts", "code": "rate_limited"}, status=429
        )
    _prune_web_signin_payloads()
    # Only an allowlisted Origin becomes the Xaman post-sign bounce target —
    # never a caller-supplied URL.
    origin = request.headers.get("Origin", "")
    return_url = {"app": origin, "web": origin} if origin in config.WEB_ALLOWED_ORIGINS else None
    payload = await xumm_ops.create_signin_payload(return_url=return_url)
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    web_signin_payloads[payload["uuid"]] = {"created_at": time.time()}
    return web.json_response({"uuid": payload["uuid"], "signin_link": payload["xumm_url"]})


async def handle_web_signin_status(request):
    uuid = request.match_info["payload_uuid"]
    if uuid not in web_signin_payloads:
        return web.json_response({"error": "not found"}, status=404)
    s = await xumm_ops.get_payload_status(uuid)
    if not s:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    if s["signed"] and s["account"] and is_valid_classic_address(s["account"]):
        wallet = s["account"]
        # A wallet already known from another surface keeps its display handle;
        # a brand-new one gets a readable shortened address.
        handle = await asyncio.to_thread(identity_store.handle_for_wallet, wallet)
        name = handle or f"{wallet[:6]}…{wallet[-4:]}"
        if not await asyncio.to_thread(identity_store.link, "web", wallet, name, wallet):
            return web.json_response({"error": "identity link failed"}, status=500)
        # #135: capture the push token so later sign requests push to Xaman.
        if s.get("user_token"):
            await asyncio.to_thread(identity_store.set_user_token, "web", wallet, s["user_token"])
        del web_signin_payloads[uuid]
        token = make_session_token({"id": wallet, "name": name, "platform": "web"})
        return web.json_response(
            {
                "state": "signed",
                "wallet": wallet,
                "session_token": token,
                "user": {"id": wallet, "username": name},
            }
        )
    if s["expired"]:
        del web_signin_payloads[uuid]
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


def _count_active(sessions: dict[str, Any], terminal_states: set[str]) -> int:
    """Number of in-flight (non-terminal) sessions in one dict."""
    return sum(1 for s in sessions.values() if getattr(s, "state", None) not in terminal_states)


async def handle_health(request):
    """Liveness + in-flight session counts, so a deploy/restart can DRAIN first
    instead of killing users mid-mint (in-memory sessions are lost on restart).
    Public + unauthenticated: exposes only integer counts, no PII."""
    detail = {
        "mint": _count_active(mint_sessions, mint_flow.TERMINAL_STATES),
        "swap": _count_active(swap_sessions, swap_flow.TERMINAL_STATES),
        "economy": _count_active(economy_sessions, economy_api.TERMINAL_STATES),
        "market": _count_active(market_sessions, market_flow.TERMINAL_STATES),
    }
    return web.json_response(
        {"ok": True, "active_sessions": sum(detail.values()), "detail": detail}
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


def _img_url_allowed(url: str) -> bool:
    """URL-prefix match against the Bunny bases, or an https hostname-suffix
    match against IMG_PROXY_ALLOWED_HOST_SUFFIXES (the per-CID IPFS gateway
    subdomains legacy mainnet image URIs resolve to, #153). The suffix check
    parses the URL so the gateway string appearing in a path or mid-hostname
    (cid.ipfs.dweb.link.evil.example) can never match."""
    if url.startswith(tuple(base + "/" for base in config.IMG_PROXY_ALLOWED_BASES)):
        return True
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.endswith(config.IMG_PROXY_ALLOWED_HOST_SUFFIXES)
    )


def _range_response(request: web.Request, body: bytes, ctype: str) -> web.Response:
    """Byte-range-aware response for /api/img bodies (already fully buffered).

    iOS WebKit's media loader probes with `Range: bytes=0-1` and refuses to
    play progressive mp4 unless the server answers 206 with a Content-Range —
    a 200-full-body answer leaves an animated NFT's <video> frozen on its
    poster (#250). Only the single-range `bytes=` forms browsers actually send
    are honored; a malformed Range header is ignored (200, per RFC 9110)."""
    headers = {"Cache-Control": "public, max-age=86400", "Accept-Ranges": "bytes"}
    total = len(body)
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("Range", "").strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
        else:  # suffix form (bytes=-N): the last N bytes
            start = max(total - int(m.group(2)), 0)
            end = total - 1
        if start >= total or start > end:
            headers["Content-Range"] = f"bytes */{total}"
            return web.Response(status=416, headers=headers)
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return web.Response(
            status=206, body=body[start : end + 1], content_type=ctype, headers=headers
        )
    return web.Response(body=body, content_type=ctype, headers=headers)


async def handle_img(request):
    """Same-origin proxy for CDN images: the Activity's CSP blocks cross-origin
    <img> loads, so the client routes image URLs through here (allowed: the
    Bunny CDN bases plus the IPFS gateway host suffixes — see _img_url_allowed).
    Raw ipfs:// URIs (the on-chain index stores them verbatim, and the
    leaderboard serves them as-is) are resolved to the gateway first.

    Local archive first (#153): if the requested URL maps back to a live
    edition in the on-chain index and that edition's still is in the
    images_<network>/ archive (scripts/rebuild_cdn_images.py), serve it
    straight from disk — no CDN, no IPFS. The proxy below is the fallback
    for editions the archive doesn't hold yet; any archive/index failure
    degrades to that fallback, never to an error.

    `w=<px>` asks for a pre-built thumbnail (scripts/generate_thumbnails.py):
    when the request fits in the thumb size, the ~10 KB WebP is served instead
    of the ~634 KB still. Missing thumb (or any w outside (0, THUMB_SIZE]) just
    means the full still — never an error, so the client can always pass it."""
    url = request.query.get("u", "")
    if len(url) > 2048:
        return web.json_response({"error": "bad image url"}, status=400)
    try:
        want_w = int(request.query.get("w", ""))
    except ValueError:
        want_w = 0
    want_thumb = 0 < want_w <= image_archive.THUMB_SIZE
    try:
        # SQLite lookup + disk read are synchronous — keep them off the
        # event loop (a leaderboard page bursts ~50 concurrent requests).
        def _archive_read() -> tuple[bytes, str] | None:
            conn = nft_index.init_db(nft_index.index_db_path(config.XRPL_NETWORK))
            try:
                edition = image_archive.edition_for_url(conn, url)
            finally:
                conn.close()
            if edition is None:
                return None
            local = None
            if want_thumb:
                local = image_archive.local_thumb(config.XRPL_NETWORK, edition)
            local = local or image_archive.local_image(config.XRPL_NETWORK, edition)
            if not local:
                return None
            path, ctype = local
            with open(path, "rb") as f:
                return f.read(), ctype

        archived = await asyncio.to_thread(_archive_read)
        if archived:
            body, ctype = archived
            return _range_response(request, body, ctype)
    except Exception as e:
        logging.warning(f"image archive lookup failed for {url}: {e!r}")
    url = swap_meta.resolve_ipfs(url)
    if not _img_url_allowed(url):
        return web.json_response({"error": "bad image url"}, status=400)
    try:
        body, ctype = await _fetch_cdn(url)
    except Exception as e:
        logging.error(f"Image proxy fetch failed for {url}: {e}")
        return web.json_response({"error": "image fetch failed"}, status=502)
    # Mint/swap outputs get unique CDN basenames, so they are safe to cache.
    return _range_response(request, body, ctype)


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
            ws = await start_coro(user["id"], request["wallet"], body, await _push_token(user))
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
    lambda uid, w, b, tok: economy_api.start_equip(
        uid, w, b["nft_id"], b["slot"], b["value"], user_token=tok
    ),
    lambda w, b: mock_economy.INSTANCE.equip(w, b["nft_id"], b["slot"], b["value"]),
)
handle_harvest_start = _economy_post(
    "harvest",
    lambda uid, w, b, tok: economy_api.start_harvest(uid, w, b["nft_id"], user_token=tok),
    lambda w, b: mock_economy.INSTANCE.harvest(w, b["nft_id"]),
)
handle_assemble_start = _economy_post(
    "assemble",
    lambda uid, w, b, tok: economy_api.start_assemble(
        uid, w, int(b["edition"]), b["chosen"], user_token=tok
    ),
    lambda w, b: mock_economy.INSTANCE.assemble(w, int(b["edition"]), b["chosen"]),
)
handle_extract_start = _economy_post(
    "extract",
    lambda uid, w, b, tok: economy_api.start_extract(uid, w, b, user_token=tok),
    lambda w, b: mock_economy.INSTANCE.extract(w, b),
)
handle_deposit_start = _economy_post(
    "deposit",
    lambda uid, w, b, tok: economy_api.start_deposit(uid, w, b, user_token=tok),
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
async def cors_mw(request, handler):
    # Standalone web surface (spec 2026-07-16): the GitHub-Pages-hosted client
    # calls this API cross-origin. Dark by default — with WEB_ALLOWED_ORIGINS
    # unset (or the Origin not allowlisted) responses are byte-identical to
    # today, so Discord/Telegram/dev surfaces are untouched. Auth rides the
    # Authorization header, never cookies, so no Allow-Credentials.
    origin = request.headers.get("Origin", "")
    allowed = bool(origin) and origin in config.WEB_ALLOWED_ORIGINS
    if allowed and request.method == "OPTIONS":
        # Preflight: answer here — no handler owns OPTIONS routes.
        resp = web.Response(status=204)
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Max-Age"] = "3600"
    else:
        resp = await handler(request)
    if allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers.add("Vary", "Origin")
    return resp


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
    app = web.Application(middlewares=[cors_mw, no_cache_mw])
    identity_store.ensure_identities_table()
    identity_store.migrate_users_to_identities()
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/api/token", handle_token)
    app.router.add_post("/api/session", handle_session)
    app.router.add_post("/api/telegram/auth", handle_telegram_auth)
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/account", handle_account)
    app.router.add_post("/api/register", handle_register)
    app.router.add_post("/api/mint", handle_mint_start)
    # /active must register BEFORE /{session_id}: aiohttp dispatches in
    # registration order, and the dynamic route would swallow it as an id.
    app.router.add_get("/api/mint/active", handle_mint_active)
    # Bulk mint (#215) routes must also register BEFORE /{session_id} for the
    # same reason — "bulk" would otherwise be swallowed as a session id.
    app.router.add_post("/api/mint/bulk", handle_bulk_mint_start)
    app.router.add_get("/api/mint/bulk/active", handle_bulk_mint_active)
    app.router.add_post("/api/mint/bulk/{session_id}/cancel", handle_bulk_mint_cancel)
    app.router.add_get("/api/mint/bulk/{session_id}", handle_bulk_mint_status)
    app.router.add_get("/api/mint/{session_id}", handle_mint_status)
    app.router.add_post("/api/mint/{session_id}/regenerate", handle_mint_regenerate)
    app.router.add_post("/api/mint/{session_id}/cancel", handle_mint_cancel)
    app.router.add_post("/api/signin", handle_signin_start)
    app.router.add_get("/api/signin/{payload_uuid}", handle_signin_status)
    # Standalone web surface (spec 2026-07-16): client-callable wallet signin.
    app.router.add_post("/api/web/signin", handle_web_signin_start)
    app.router.add_get("/api/web/signin/{payload_uuid}", handle_web_signin_status)
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
    app.router.add_get("/api/shop/catalog", handle_shop_catalog)
    app.router.add_post("/api/shop/buy", handle_shop_buy_start)
    app.router.add_get("/api/shop/buy/{session_id}", handle_shop_buy_status)
    app.router.add_post("/api/swap", handle_swap_start)
    app.router.add_get("/api/swap/{session_id}", handle_swap_status)
    app.router.add_post("/api/swap/{session_id}/regenerate", handle_swap_regenerate)
    app.router.add_post("/api/swap/{session_id}/cancel", handle_swap_cancel)
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
    app.on_startup.append(_start_bulk_resume)
    app.on_cleanup.append(_stop_bulk_resume)
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
