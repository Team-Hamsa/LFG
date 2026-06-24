# New mints must be burnable (so the trait economy can harvest them) while
# remaining transferable + mutable (so trait swaps modify in place).
from lfg_core import config


def test_flag_bit_constants():
    assert config.NFT_FLAG_BURNABLE == 0x0001
    assert config.NFT_FLAG_TRANSFERABLE == 0x0008
    assert config.NFT_FLAG_MUTABLE == 0x0010


def test_default_nft_flags_compose_to_25():
    expected = (
        config.NFT_FLAG_BURNABLE
        | config.NFT_FLAG_TRANSFERABLE
        | config.NFT_FLAG_MUTABLE
    )
    assert expected == 25


def test_live_nft_flags_are_burnable_and_mutable():
    assert config.NFT_FLAGS & config.NFT_FLAG_BURNABLE, "mints must be burnable"
    assert config.NFT_FLAGS & config.NFT_FLAG_MUTABLE, "mints must stay mutable"
