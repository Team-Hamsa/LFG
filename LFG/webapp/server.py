# webapp/server.py
# Discord Activity backend: aiohttp app serving the embedded-app frontend,
# OAuth token exchange, and the mint/swap/register API.
#
# Run with:  python -m webapp.server   (from the repo root)

import os
import sys
import hmac
import json
import time
import base64
import hashlib
import logging
import asyncio

import aiohttp
from aiohttp import web
from xrpl.core.addresscodec import is_valid_classic_address

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import config, mint_flow, xumm_ops, xrpl_ops, swap_meta, swap_flow
from user_db import create_users_table, register_user, get_user

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

CLIENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client")
DISCORD_API = "https://discord.com/api"
SESSION_TTL = 6 * 3600

# In-memory sessions: session_id -> MintSession / SwapSession
mint_sessions = {}
swap_sessions = {}
SESSION_RETENTION = 3600  # keep terminal sessions briefly for late polls


def _prune_sessions(sessions: dict, terminal_states: set) -> None:
    cutoff = time.time() - SESSION_RETENTION
    for sid, s in list(sessions.items()):
        if s.state in terminal_states and s.created_at < cutoff:
            del sessions[sid]


def _active_session(sessions: dict, terminal_states: set, discord_id: str):
    for s in sessions.values():
        if s.discord_id == discord_id and s.state not in terminal_states:
            return s
    return None


def _session_secret() -> bytes:
    if config.WEBAPP_SESSION_SECRET:
        return config.WEBAPP_SESSION_SECRET.encode()
    # Fall back to a derivation of the XUMM secret so single-process setups
    # work without extra config; set WEBAPP_SESSION_SECRET in production.
    return hashlib.sha256(b"lfg-webapp:" + config.XUMM_API_SECRET.encode()).digest()


def make_session_token(user: dict) -> str:
    payload = {"id": user["id"], "name": user["name"], "exp": int(time.time()) + SESSION_TTL}
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


def require_auth(handler):
    async def wrapper(request):
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
        record = await asyncio.to_thread(get_user, request["user"]["id"])
        if not record or not record.get("address"):
            return web.json_response({"error": "no wallet registered"}, status=400)
        request["wallet"] = record["address"]
        return await handler(request)
    return wrapper


def make_status_handler(sessions: dict):
    @require_auth
    async def handler(request):
        session = sessions.get(request.match_info["session_id"])
        if not session or session.discord_id != request["user"]["id"]:
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
        resp = await http.post(f"{DISCORD_API}/oauth2/token", data={
            "client_id": config.DISCORD_CLIENT_ID,
            "client_secret": config.DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        token_data = await resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logging.error(f"OAuth exchange failed: {token_data}")
            return web.json_response({"error": "oauth exchange failed"}, status=400)

        me = await http.get(f"{DISCORD_API}/users/@me",
                            headers={"Authorization": f"Bearer {access_token}"})
        user = await me.json()
        if me.status != 200 or "id" not in user:
            logging.error(f"Discord /users/@me failed ({me.status}): {user}")
            return web.json_response({"error": "discord identity lookup failed"},
                                     status=502)

    session_token = make_session_token({"id": user["id"], "name": user.get("username", "")})
    return web.json_response({
        "access_token": access_token,  # the SDK needs this for authenticate()
        "session_token": session_token,
        "user": {"id": user["id"], "username": user.get("username", "")},
    })


@require_auth
async def handle_me(request):
    user = request["user"]
    record = await asyncio.to_thread(get_user, user["id"])
    return web.json_response({
        "id": user["id"],
        "username": user["name"],
        "wallet": record["address"] if record else None,
    })


@require_auth
async def handle_register(request):
    user = request["user"]
    body = await request.json()
    wallet = (body.get("wallet") or "").strip()
    if not is_valid_classic_address(wallet):
        return web.json_response({"error": "invalid XRPL address"}, status=400)
    if not await asyncio.to_thread(register_user, user["id"], user["name"], wallet):
        return web.json_response({"error": "registration failed"}, status=500)
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
    active = _active_session(mint_sessions, mint_flow.TERMINAL_STATES, user["id"])
    if active:
        return web.json_response({"error": "mint already in progress",
                                  "session": active.to_dict()}, status=409)

    session = mint_flow.MintSession(discord_id=user["id"], wallet_address=request["wallet"],
                                    return_url=await _request_return_url(request))
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
        nfts = await swap_meta.load_wallet_nfts(request["wallet"],
                                                xrpl_ops.get_account_nfts)
    except Exception as e:
        logging.error(f"NFT listing failed: {e}")
        return web.json_response({"error": "failed to load wallet NFTs"}, status=502)
    # Quote the swap fee for the cost line (BRIX holders pay BRIX; everyone
    # else the AMM XRP equivalent). Advisory only — the swap session
    # re-detects the path server-side when the fee is actually charged.
    swap_fee = None
    try:
        pay_with, amount = await swap_flow.detect_swap_payment(
            request["wallet"], swap_flow.swap_fee_total(2))
        swap_fee = {"pay_with": pay_with, "amount": amount,
                    "per_nft": swap_flow.swap_fee_total(1)}
    except Exception as e:
        logging.warning(f"Swap fee quote failed: {e}")
    return web.json_response({"nfts": nfts,
                              "swappable_traits": swap_meta.SWAPPABLE_TRAITS,
                              "swap_fee": swap_fee})


@require_wallet
async def handle_swap_start(request):
    user = request["user"]
    body = await request.json()
    nft1_id = body.get("nft1_id")
    nft2_id = body.get("nft2_id")
    traits_to_swap = body.get("traits", [])
    if not nft1_id or not nft2_id or nft1_id == nft2_id:
        return web.json_response({"error": "select two different NFTs"}, status=400)
    if not traits_to_swap or any(t not in swap_meta.SWAPPABLE_TRAITS
                                 for t in traits_to_swap):
        return web.json_response({"error": "invalid trait selection"}, status=400)

    _prune_sessions(swap_sessions, swap_flow.TERMINAL_STATES)
    if _active_session(swap_sessions, swap_flow.TERMINAL_STATES, user["id"]):
        return web.json_response({"error": "swap already in progress"}, status=409)

    # Re-verify ownership and metadata server-side (never trust client data)
    try:
        nfts = await swap_meta.load_wallet_nfts(request["wallet"],
                                                xrpl_ops.get_account_nfts)
    except Exception as e:
        logging.error(f"NFT verification failed: {e}")
        return web.json_response({"error": "failed to verify wallet NFTs"}, status=502)
    by_id = {n["nft_id"]: n for n in nfts}
    nft1, nft2 = by_id.get(nft1_id), by_id.get(nft2_id)
    if not nft1 or not nft2:
        return web.json_response({"error": "NFT not found in your wallet"}, status=400)
    if nft1["gender"] != nft2["gender"]:
        return web.json_response(
            {"error": "NFTs must share the same body type to swap traits"}, status=400)

    # The load_wallet_nfts call above awaited, so re-check before inserting
    if _active_session(swap_sessions, swap_flow.TERMINAL_STATES, user["id"]):
        return web.json_response({"error": "swap already in progress"}, status=409)
    session = swap_flow.SwapSession(
        discord_id=user["id"], wallet_address=request["wallet"],
        nft1=nft1, nft2=nft2, traits_to_swap=traits_to_swap,
        return_url=xumm_ops.discord_return_url(body.get("guild_id"),
                                               body.get("channel_id")))
    swap_sessions[session.id] = session
    asyncio.get_event_loop().create_task(swap_flow.run_swap_session(session))
    return web.json_response(session.to_dict())


handle_mint_status = make_status_handler(mint_sessions)
handle_swap_status = make_status_handler(swap_sessions)


async def handle_config(request):
    """Public config the frontend needs before auth (client_id for authorize())."""
    return web.json_response({"client_id": config.DISCORD_CLIENT_ID})


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
    return web.Response(body=body, content_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})


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
    app.router.add_get("/api/config", handle_config)
    app.router.add_post("/api/token", handle_token)
    app.router.add_get("/api/me", handle_me)
    app.router.add_post("/api/register", handle_register)
    app.router.add_post("/api/mint", handle_mint_start)
    app.router.add_get("/api/mint/{session_id}", handle_mint_status)
    app.router.add_get("/api/nfts", handle_nfts)
    app.router.add_post("/api/swap", handle_swap_start)
    app.router.add_get("/api/swap/{session_id}", handle_swap_status)
    app.router.add_get("/api/qr.png", handle_qr)
    app.router.add_get("/api/img", handle_img)
    app.router.add_get("/", handle_index)
    app.router.add_static("/", CLIENT_DIR)
    return app


def main():
    if not config.DISCORD_CLIENT_ID or not config.DISCORD_CLIENT_SECRET:
        raise ValueError("DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET must be set "
                         "for the Activity webapp (see docs/ACTIVITY_SETUP.md)")
    create_users_table()
    logging.info(f"Starting LFG Activity webapp on port {config.WEBAPP_PORT}")
    web.run_app(create_app(), port=config.WEBAPP_PORT)


if __name__ == "__main__":
    main()
