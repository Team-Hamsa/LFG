# tests/test_sdk_mint_swap.py
from tests.mock_service import build_mock_service
from tests.sdk_helpers import make_client, noop_sleep, run


def test_start_mint_returns_session_id():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            assert (await client.start_mint("42"))["session_id"] == "m1"
        await server.close()

    run(_inner())


def test_wait_for_mint_polls_until_terminal():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            await client.start_mint("42")
            final = await client.wait_for_mint("42", "m1", interval=0.0, sleep=noop_sleep)
            assert final["state"] == "offer_ready"  # mock flips to terminal on the 2nd poll
        await server.close()

    run(_inner())


def test_start_swap_sends_trait_body():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            assert (await client.start_swap("42", "nftA", "nftB", ["Hat"]))["session_id"] == "s1"
        await server.close()

    run(_inner())


def test_swap_status():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            assert (await client.swap_status("42", "s1"))["state"] == "done"
        await server.close()

    run(_inner())
