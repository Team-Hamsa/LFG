# New mints must be burnable (so the trait economy can harvest them) while
# remaining transferable + mutable (so trait swaps modify in place).
import asyncio

import lfg_core.xrpl_ops as xrpl_ops
from lfg_core import config, swap_meta


def test_flag_bit_constants():
    assert config.NFT_FLAG_BURNABLE == 0x0001
    assert config.NFT_FLAG_TRANSFERABLE == 0x0008
    assert config.NFT_FLAG_MUTABLE == 0x0010


def test_default_nft_flags_compose_to_25():
    expected = config.NFT_FLAG_BURNABLE | config.NFT_FLAG_TRANSFERABLE | config.NFT_FLAG_MUTABLE
    assert expected == 25
    # Tie the live value to the composed bits, not just the arithmetic: this
    # fails on a regression to 24 (no burnable) or any stray extra bit, which
    # the bit-presence checks below would miss.
    assert config.NFT_FLAGS == expected


def test_live_nft_flags_are_burnable_and_mutable():
    assert config.NFT_FLAGS & config.NFT_FLAG_BURNABLE, "mints must be burnable"
    assert config.NFT_FLAGS & config.NFT_FLAG_MUTABLE, "mints must stay mutable"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, result):
        self.result = result


def _capture_mint(monkeypatch, captured):
    def fake_submit(tx, client, wallet):
        captured["tx"] = tx
        return _Resp(
            {"hash": "HASH", "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "NFTID"}}
        )

    def fake_request(self, req):
        return _Resp({"meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "NFTID"}})

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request)


def test_default_mint_is_burnable(monkeypatch):
    captured = {}
    _capture_mint(monkeypatch, captured)
    _run(xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS))
    assert captured["tx"].flags & config.NFT_FLAG_BURNABLE, "mint tx must be burnable"
    assert captured["tx"].flags & config.NFT_FLAG_MUTABLE, "mint tx must stay mutable"


def test_flag25_token_is_mutable_so_swap_modifies_in_place():
    # A burnable+transferable+mutable (25) token must still report mutable, so
    # swap_flow routes it to modify_items (NFTokenModify), never burn-and-remint.
    rec = swap_meta.normalize_nft(
        "NFTID",
        {"name": "Let's Effing Go! #3534", "attributes": []},
        flags=25,
    )
    assert rec is not None
    assert rec["mutable"] is True


# NOTE: the former test_main_letsgo_mint_default_is_burnable greps main.py for an
# inline `NFT_FLAGS = int(os.getenv("NFT_FLAGS", "25"))`. Spine Plan 3 (Task 4)
# inverted the /letsgo mint onto lfg_service, deleting that inline pipeline —
# the mint now reads lfg_core.config.NFT_FLAGS, whose burnable+mutable=25
# guarantee is covered by test_default_nft_flags_compose_to_25,
# test_live_nft_flags_are_burnable_and_mutable, and test_default_mint_is_burnable
# above. The source-grep test was removed as obsolete.
