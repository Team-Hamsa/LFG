# lfg_service/app.py
# Discord Activity backend: aiohttp app serving the embedded-app frontend,
# OAuth token exchange, and the mint/swap/register API.
#
# Run with:  python -m lfg_service.app   (from the repo root)
# (`python -m webapp.server` also works via the webapp/server.py launch shim.)

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web
from xrpl.core.addresscodec import is_valid_classic_address

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import (
    config,
    economy_flow,
    layer_store,
    mint_flow,
    swap_flow,
    swap_meta,
    xrpl_ops,
    xumm_ops,
)
from lfg_service import identity as identity_store
from lfg_service.auth import require_service_token, surface_for_token
from lfg_service.events import Event, InMemoryEventBus
from lfg_service.telegram_auth import validate_init_data
from user_db import create_users_table, get_user, register_user
from webapp import economy_api, mock_economy

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
    return web.json_response({"ok": True, "wallet": wallet})


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
        return_url=await _request_return_url(request),
        platform=_platform(user),
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
    return web.json_response(
        {"nfts": nfts, "swappable_traits": swap_meta.SWAPPABLE_TRAITS, "swap_fee": swap_fee}
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
    if nft1["gender"] != nft2["gender"]:
        return web.json_response(
            {"error": "NFTs must share the same body type to swap traits"}, status=400
        )

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
        {"client_id": config.DISCORD_CLIENT_ID, "dev_mode": config.WEBAPP_DEV_MODE}
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
    if config.WEBAPP_DEV_MODE:
        return web.json_response(mock_economy.INSTANCE.read_state(request["wallet"]))
    conn = economy_api.open_conn()
    try:
        return web.json_response(economy_api.read_economy_state(conn, request["wallet"]))
    finally:
        conn.close()


def _economy_post(kind, start_coro, mock_call):
    async def handler(request):
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
    app.router.add_post("/api/swap", handle_swap_start)
    app.router.add_get("/api/swap/{session_id}", handle_swap_status)
    app.router.add_get("/api/qr.png", handle_qr)
    app.router.add_get("/api/img", handle_img)
    app.router.add_get("/api/layer", handle_layer)
    app.router.add_get("/api/economy", require_wallet(handle_economy))
    app.router.add_post("/api/equip", require_wallet(handle_equip_start))
    app.router.add_get("/api/equip/{session_id}", handle_equip_status)
    app.router.add_post("/api/harvest", require_wallet(handle_harvest_start))
    app.router.add_get("/api/harvest/{session_id}", handle_harvest_status)
    app.router.add_post("/api/assemble", require_wallet(handle_assemble_start))
    app.router.add_get("/api/assemble/{session_id}", handle_assemble_status)
    app.router.add_get("/events", handle_events)
    app.router.add_get("/events/me", handle_events_me)
    app.router.add_get("/__dev/reload", handle_dev_reload)
    app.router.add_get("/", handle_index)
    app.router.add_static("/", CLIENT_DIR)
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
