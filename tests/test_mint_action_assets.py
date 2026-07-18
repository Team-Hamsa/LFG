from typing import Any

import pytest

from lfg_core import mint_flow


@pytest.fixture
def asset_mocks(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_select(store):
        return "male", [{"trait_type": "Body", "value": "Straight"}]

    async def fake_compose(attributes, body, store, basename):
        captured["compose_attributes"] = attributes
        return "/tmp/out.png", False

    async def fake_upload_output(path, is_video, upload_fn, basename, keep_still=None):
        captured["keep_still"] = keep_still
        return f"https://cdn.example/{basename}.png", None

    async def fake_upload_bunny(name, data, content_type):
        captured["metadata_bytes"] = data
        return f"https://cdn.example/{name}"

    monkeypatch.setattr(mint_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", fake_select)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow.swap_compose, "upload_output", fake_upload_output)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload_bunny)
    monkeypatch.setattr(
        mint_flow.image_archive,
        "pending_still_path",
        lambda *args: "/tmp/pending.png",
    )
    return captured


@pytest.mark.asyncio
async def test_prepare_mint_assets_never_calls_xrpl(monkeypatch, asset_mocks):
    def ledger_called(*args, **kwargs):
        raise AssertionError("XRPL called during off-ledger preparation")

    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", ledger_called)
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", ledger_called)
    monkeypatch.setattr(
        mint_flow.xumm_ops, "create_accept_offer_payload", ledger_called
    )
    prepared = await mint_flow.prepare_mint_assets(
        nft_number=4001, session_tag="action:s1"
    )
    assert prepared.metadata_url == "https://cdn.example/4001/4001_0.json"
    assert prepared.image_url == "https://cdn.example/4001/4001_0.png"
    assert prepared.video_url is None
    assert prepared.traits["Body"] == "Straight"
    assert prepared.traits["Hat"] == "None"
    assert prepared.body_type == "male"
    assert asset_mocks["keep_still"] == "/tmp/pending.png"


class _FakeRarityConnection:
    def __init__(self, calls):
        self.calls = calls

    def close(self):
        self.calls.append("rarity-close")


@pytest.mark.asyncio
async def test_record_validated_mint_preserves_callback_and_record_order(
    monkeypatch, asset_mocks
):
    prepared = await mint_flow.prepare_mint_assets(
        nft_number=4002, session_tag="action:s2"
    )
    calls = []
    monkeypatch.setattr(
        mint_flow.image_archive,
        "promote_still",
        lambda *args: calls.append("promote"),
    )

    async def on_mint(number, nft_id, image_url):
        calls.append(("on_mint", number, nft_id, image_url))

    def fake_record(**kwargs):
        calls.append(("record", kwargs))
        return True

    monkeypatch.setattr(mint_flow, "record_nft_mint", fake_record)
    monkeypatch.setattr(
        mint_flow.rarity,
        "connect",
        lambda: _FakeRarityConnection(calls),
    )
    monkeypatch.setattr(
        mint_flow.rarity,
        "start_boost_clock",
        lambda *args: calls.append("rarity-clock"),
    )
    monkeypatch.setattr(
        mint_flow.rarity,
        "recalculate_rarity",
        lambda *args: calls.append("rarity-recalculate"),
    )
    mint_flow._reserved_numbers.add(4002)
    saved = await mint_flow.record_validated_mint(
        prepared,
        nft_id="NFT1",
        wallet_address="rBuyer",
        user_id="u1",
        network="testnet",
        on_mint=on_mint,
    )
    assert saved is True
    assert calls[0] == "promote"
    assert calls[1][0] == "on_mint"
    assert calls[2][0] == "record"
    assert calls[2][1]["nft_id"] == "NFT1"
    assert calls[2][1]["owner_address"] == "rBuyer"
    assert 4002 not in mint_flow._reserved_numbers


@pytest.mark.asyncio
async def test_record_failure_writes_recovery_and_keeps_number_reserved(
    monkeypatch, asset_mocks
):
    prepared = await mint_flow.prepare_mint_assets(
        nft_number=4003, session_tag="action:s3"
    )
    monkeypatch.setattr(mint_flow.image_archive, "promote_still", lambda *args: None)
    monkeypatch.setattr(
        mint_flow,
        "record_nft_mint",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db offline")),
    )
    recovered = []
    monkeypatch.setattr(mint_flow, "_save_recovery_record", recovered.append)
    mint_flow._reserved_numbers.add(4003)
    saved = await mint_flow.record_validated_mint(
        prepared,
        nft_id="NFT2",
        wallet_address="rBuyer",
        user_id="u1",
        network="testnet",
    )
    assert saved is False
    assert recovered[0]["nft_id"] == "NFT2"
    assert 4003 in mint_flow._reserved_numbers
    mint_flow._reserved_numbers.discard(4003)
