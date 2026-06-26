# tests/mock_service.py
# A minimal in-process aiohttp app mimicking the lfg_service contract, with
# knobs for flaky responses, one-shot 401s, mint poll progression, and a
# scripted /events stream. No lfg_core import — keeps SDK tests fast/isolated.

import json
from typing import Any

from aiohttp import web

SERVICE_TOKEN = "svc-test"


def _bearer(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else None


def build_mock_service(
    *,
    flaky: dict[str, int] | None = None,  # path -> number of leading 503s to emit
    expire_session_once: bool = False,  # first user-scoped call 401s, forcing a refresh
    events_script: dict[int, list[dict]] | None = None,  # connection# -> events to emit then close
) -> web.Application:
    app = web.Application()
    state: dict[str, Any] = {
        "hits": {},  # path -> count
        "session_hits": 0,  # number of /api/session mints
        "fail_left": dict(flaky or {}),
        "live_sessions": set(),  # minted session tokens currently valid
        "expired_once": expire_session_once,
        "events_script": events_script or {},
        "events_conns": 0,
        "last_event_types": None,  # ?types= seen on the last /events connect
        "mint_polls": {},  # session_id -> times polled
    }
    app["state"] = state

    def _count(path: str) -> None:
        state["hits"][path] = state["hits"].get(path, 0) + 1

    def _maybe_flaky(path: str) -> web.Response | None:
        left = state["fail_left"].get(path, 0)
        if left > 0:
            state["fail_left"][path] = left - 1
            return web.json_response({"error": "overloaded", "code": "busy"}, status=503)
        return None

    def _require_session(request: web.Request) -> web.Response | None:
        tok = _bearer(request)
        if state["expired_once"]:
            state["expired_once"] = False
            return web.json_response({"error": "expired", "code": "bad_session"}, status=401)
        if not tok or tok not in state["live_sessions"]:
            return web.json_response({"error": "unauthorized", "code": "bad_session"}, status=401)
        return None

    async def handle_config(request: web.Request) -> web.StreamResponse:
        _count("/api/config")
        flak = _maybe_flaky("/api/config")
        if flak is not None:
            return flak
        return web.json_response({"ok": True, "network": "testnet"})

    async def handle_qr(request: web.Request) -> web.StreamResponse:
        _count("/api/qr.png")
        if not request.query.get("d"):
            return web.json_response({"error": "bad data", "code": "bad_request"}, status=400)
        return web.Response(body=b"\x89PNG\r\n", content_type="image/png")

    async def handle_img(request: web.Request) -> web.StreamResponse:
        _count("/api/img")
        return web.Response(body=b"IMGDATA", content_type="image/jpeg")

    async def handle_session(request: web.Request) -> web.StreamResponse:
        if _bearer(request) != SERVICE_TOKEN:
            return web.json_response(
                {"error": "unauthorized", "code": "bad_service_token"}, status=401
            )
        body = await request.json()
        state["session_hits"] += 1
        pid = body.get("platform_user_id", "")
        tok = f"sess-{pid}-{state['session_hits']}"
        state["live_sessions"].add(tok)
        return web.json_response(
            {
                "session_token": tok,
                "user": {"id": pid, "username": body.get("platform_username", "")},
            }
        )

    async def handle_me(request: web.Request) -> web.StreamResponse:
        _count("/api/me")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"id": "u", "username": "u", "wallet": "rMOCK"})

    async def handle_register(request: web.Request) -> web.StreamResponse:
        _count("/api/register")
        bad = _require_session(request)
        if bad is not None:
            return bad
        body = await request.json()
        return web.json_response({"ok": True, "wallet": body.get("wallet")})

    async def handle_mint_start(request: web.Request) -> web.StreamResponse:
        _count("/api/mint")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": "m1", "state": "awaiting_payment"})

    async def handle_mint_status(request: web.Request) -> web.StreamResponse:
        bad = _require_session(request)
        if bad is not None:
            return bad
        sid = request.match_info["session_id"]
        state["mint_polls"][sid] = state["mint_polls"].get(sid, 0) + 1
        ready = state["mint_polls"][sid] >= 2
        return web.json_response(
            {"session_id": sid, "state": "offer_ready" if ready else "minting"}
        )

    async def handle_swap_start(request: web.Request) -> web.StreamResponse:
        _count("/api/swap")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": "s1", "state": "awaiting_payment"})

    async def handle_swap_status(request: web.Request) -> web.StreamResponse:
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": request.match_info["session_id"], "state": "done"})

    async def handle_nfts(request: web.Request) -> web.StreamResponse:
        _count("/api/nfts")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"nfts": []})

    async def handle_account(request: web.Request) -> web.StreamResponse:
        _count("/api/account")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response(
            {
                "wallet": "rMOCK",
                "identities": [
                    {"platform": "test", "platform_user_id": "u", "display_handle": "u"}
                ],
            }
        )

    async def handle_signin_start(request: web.Request) -> web.StreamResponse:
        _count("/api/signin")
        bad = _require_session(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
        except Exception:
            body = {}
        state["last_signin_link_flag"] = bool(body.get("link"))
        return web.json_response({"uuid": "sg1", "signin_link": "https://xumm.app/sign/mock"})

    async def handle_signin_status(request: web.Request) -> web.StreamResponse:
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"uuid": request.match_info["payload_uuid"], "signed": True})

    async def handle_generic_session_get(request: web.Request) -> web.StreamResponse:
        # economy / equip-status / harvest-status / assemble-status
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"ok": True, "path": request.path})

    async def handle_generic_session_post(request: web.Request) -> web.StreamResponse:
        # equip / harvest / assemble start
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": "x1", "state": "started"})

    async def handle_events(request: web.Request) -> web.WebSocketResponse:
        # FIX 2: accept token via ?token= (legacy) OR Authorization: Bearer header
        token = request.query.get("token") or request.headers.get("Authorization", "").removeprefix(
            "Bearer "
        )
        if token != SERVICE_TOKEN:
            # aiohttp WS handshake can't carry a JSON 401 cleanly; reject pre-upgrade
            raise web.HTTPUnauthorized()
        state["last_event_types"] = request.query.get("types")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state["events_conns"] += 1
        for evt in state["events_script"].get(state["events_conns"], []):
            await ws.send_str(json.dumps(evt))
        await ws.close()  # ending the connection forces the client to reconnect
        return ws

    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/qr.png", handle_qr)
    app.router.add_get("/api/img", handle_img)
    app.router.add_post("/api/session", handle_session)
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/account", handle_account)
    app.router.add_post("/api/register", handle_register)
    app.router.add_post("/api/mint", handle_mint_start)
    app.router.add_get("/api/mint/{session_id}", handle_mint_status)
    app.router.add_post("/api/swap", handle_swap_start)
    app.router.add_get("/api/swap/{session_id}", handle_swap_status)
    app.router.add_get("/api/nfts", handle_nfts)
    app.router.add_post("/api/signin", handle_signin_start)
    app.router.add_get("/api/signin/{payload_uuid}", handle_signin_status)
    app.router.add_get("/api/economy", handle_generic_session_get)
    app.router.add_post("/api/equip", handle_generic_session_post)
    app.router.add_get("/api/equip/{session_id}", handle_generic_session_get)
    app.router.add_post("/api/harvest", handle_generic_session_post)
    app.router.add_get("/api/harvest/{session_id}", handle_generic_session_get)
    app.router.add_post("/api/assemble", handle_generic_session_post)
    app.router.add_get("/api/assemble/{session_id}", handle_generic_session_get)
    app.router.add_get("/events", handle_events)
    return app
