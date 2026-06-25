# tests/test_sdk_remaining.py
from tests.mock_service import build_mock_service
from tests.sdk_helpers import make_client, run


def test_signin_and_nfts_and_economy():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            assert (await client.signin_start("42"))["uuid"] == "sg1"
            assert (await client.signin_status("42", "sg1"))["signed"] is True
            assert "nfts" in await client.nfts("42")
            assert (await client.economy("42"))["ok"] is True
        await server.close()

    run(_inner())


def test_equip_harvest_assemble_start_and_status():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            assert (await client.equip_start("42", {"asset": "x"}))["session_id"] == "x1"
            assert (await client.equip_status("42", "x1"))["ok"] is True
            assert (await client.harvest_start("42", {"nft": "y"}))["session_id"] == "x1"
            assert (await client.harvest_status("42", "x1"))["ok"] is True
            assert (await client.assemble_start("42", {"body": "z"}))["session_id"] == "x1"
            assert (await client.assemble_status("42", "x1"))["ok"] is True
        await server.close()

    run(_inner())
