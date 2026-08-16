import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import testnet_amm_setup as amm  # noqa: E402


def test_default_ripple_enabled_true_when_bit_set() -> None:
    assert amm.default_ripple_enabled(0x00800000) is True
    assert amm.default_ripple_enabled(0x00800000 | 0x00010000) is True


def test_default_ripple_enabled_false_when_unset() -> None:
    assert amm.default_ripple_enabled(0) is False
    assert amm.default_ripple_enabled(0x00010000) is False


def test_amm_create_fee_drops_converts_reserve_increment() -> None:
    # 0.2 XRP increment -> 200000 drops
    assert amm.amm_create_fee_drops(0.2) == "200000"
    # 2 XRP increment -> 2000000 drops
    assert amm.amm_create_fee_drops(2) == "2000000"


def test_format_pool_summary_contains_key_facts() -> None:
    out = amm.format_pool_summary("rAMMxxxxxxxxxxxxxxxxxxxxxxxxxx", "50000000", "5000", 500)
    assert "rAMMxxxxxxxxxxxxxxxxxxxxxxxxxx" in out
    assert "5000" in out
    assert "0.5%" in out  # trading_fee 500 -> 0.5%
