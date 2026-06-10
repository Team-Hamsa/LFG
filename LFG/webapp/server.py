# webapp/server.py
# Discord Activity backend: aiohttp app serving the embedded-app frontend,
# OAuth token exchange, and the mint/trustline/register API.
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

from lfg_core import config, mint_flow, xumm_ops
from user_db import create_users_table, register_user, get_user

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

CLIENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client")
DISCORD_API = "https://discord.com/api"
SESSION_TTL = 6 * 3600

# In-memory mint sessions: session_id -> MintSession
mint_sessions = {}


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

    session_token = make_session_token({"id": user["id"], "name": user.get("username", "")})
    return web.json_response({
        "access_token": access_token,  # the SDK needs this for authenticate()
        "session_token": session_token,
        "user": {"id": user["id"], "username": user.get("username", "")},
    })


@require_auth
async def handle_me(request):
    user = request["user"]
    record = get_user(user["id"])
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
    if not register_user(user["id"], user["name"], wallet):
        return web.json_response({"error": "registration failed"}, status=500)
    return web.json_response({"ok": True, "wallet": wallet})


@require_auth
async def handle_trustline(request):
    payload = await xumm_ops.create_trustline_payload()
    if not payload:
        return web.json_response({"error": "failed to create trustline request"}, status=502)
    return web.json_response(payload)


@require_auth
async def handle_mint_start(request):
    user = request["user"]
    record = get_user(user["id"])
    if not record or not record.get("address"):
        return web.json_response({"error": "no wallet registered"}, status=400)

    # One active session per user
    for s in mint_sessions.values():
        if s.discord_id == user["id"] and s.state not in mint_flow.TERMINAL_STATES:
            return web.json_response({"error": "mint already in progress",
                                      "session": s.to_dict()}, status=409)

    session = mint_flow.MintSession(discord_id=user["id"], wallet_address=record["address"])
    mint_sessions[session.id] = session
    asyncio.get_event_loop().create_task(mint_flow.run_mint_session(session))
    return web.json_response(session.to_dict())


@require_auth
async def handle_mint_status(request):
    session = mint_sessions.get(request.match_info["session_id"])
    if not session or session.discord_id != request["user"]["id"]:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(session.to_dict())


async def handle_config(request):
    """Public config the frontend needs before auth (client_id for authorize())."""
    return web.json_response({"client_id": config.DISCORD_CLIENT_ID})


async def handle_qr(request):
    """Server-rendered QR PNG (same-origin, satisfies the Activity CSP)."""
    data = request.query.get("d", "")
    if not data or len(data) > 2048:
        return web.json_response({"error": "bad data"}, status=400)
    png = xumm_ops.generate_qr_png(data)
    return web.Response(body=png, content_type="image/png")


async def handle_index(request):
    return web.FileResponse(os.path.join(CLIENT_DIR, "index.html"))


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/config", handle_config)
    app.router.add_post("/api/token", handle_token)
    app.router.add_get("/api/me", handle_me)
    app.router.add_post("/api/register", handle_register)
    app.router.add_post("/api/trustline", handle_trustline)
    app.router.add_post("/api/mint", handle_mint_start)
    app.router.add_get("/api/mint/{session_id}", handle_mint_status)
    app.router.add_get("/api/qr.png", handle_qr)
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
