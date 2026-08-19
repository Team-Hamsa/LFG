"""LFGServiceClient methods for the BRIX daily drip (#48).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

from aiohttp import web

from tests.mock_service import SERVICE_TOKEN, build_mock_service
from tests.sdk_helpers import make_client, run


def _app_with_brix(seen: list[tuple[str, str]]) -> web.Application:
    app = build_mock_service()

    async def status(request):
        seen.append(("GET", str(request.rel_url)))
        return web.json_response(
            {"wallet": "rAlice", "claimable": 7, "unlisted_last_epoch": 7, "open_claim": None}
        )

    async def claim(request):
        seen.append(("POST", str(request.rel_url)))
        return web.json_response(
            {"claim_id": 5, "state": "confirmed", "amount": 7, "tx_hash": "HASH"}
        )

    async def claim_status(request):
        seen.append(("GET", str(request.rel_url)))
        return web.json_response({"claim_id": 5, "state": "confirmed", "amount": 7})

    app.router.add_get("/api/brix", status)
    app.router.add_post("/api/brix/claim", claim)
    app.router.add_get("/api/brix/claim/{claim_id}", claim_status)
    return app


def test_brix_status_round_trips():
    seen: list[tuple[str, str]] = []

    async def go():
        server, client = await make_client(_app_with_brix(seen))
        try:
            async with client:
                return await client.brix_status("user-1", username="alice")
        finally:
            await server.close()

    data = run(go())
    assert data["claimable"] == 7
    assert ("GET", "/api/brix") in seen


def test_brix_claim_round_trips():
    seen: list[tuple[str, str]] = []

    async def go():
        server, client = await make_client(_app_with_brix(seen))
        try:
            async with client:
                return await client.brix_claim("user-1", username="alice")
        finally:
            await server.close()

    data = run(go())
    assert data["state"] == "confirmed"
    assert data["amount"] == 7
    assert ("POST", "/api/brix/claim") in seen


def test_brix_claim_status_targets_the_claim_id():
    seen: list[tuple[str, str]] = []

    async def go():
        server, client = await make_client(_app_with_brix(seen))
        try:
            async with client:
                return await client.brix_claim_status("user-1", 5)
        finally:
            await server.close()

    assert run(go())["claim_id"] == 5
    assert ("GET", "/api/brix/claim/5") in seen


assert SERVICE_TOKEN  # imported for the shared auth token used by make_client


def test_claim_unconfirmed_is_never_retried():
    """The server has already bound the claim and cannot say whether the XRPL
    payment landed. A retry submits a second request against that same claim,
    gets `claim_in_flight`, and hides the in-progress result the surface must
    show the user."""
    calls: list[int] = []

    def _app() -> web.Application:
        app = build_mock_service()

        async def claim(request):
            calls.append(1)
            return web.json_response(
                {"error": "the payout outcome is unconfirmed", "code": "claim_unconfirmed"},
                status=502,
            )

        app.router.add_post("/api/brix/claim", claim)
        return app

    async def go():
        server, client = await make_client(_app())
        try:
            async with client:
                try:
                    await client.brix_claim("user-1", username="alice")
                except Exception as exc:
                    return exc
        finally:
            await server.close()

    err = run(go())
    assert getattr(err, "code", None) == "claim_unconfirmed"
    assert len(calls) == 1, f"claim was retried {len(calls)} times"
