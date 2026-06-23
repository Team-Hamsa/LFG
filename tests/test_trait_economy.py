# Tests for lfg_core/trait_economy.py (pure trait-economy accounting).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import nft_index, trait_economy  # noqa: E402


def _attrs(body="Straight", **slots):
    out = [{"trait_type": "Body", "value": body}]
    for slot, value in slots.items():
        out.append({"trait_type": slot, "value": value})
    return out


def _nft(nft_id, number, *, mutable=True, ledger=1, body_class="male", attrs=None):
    return nft_index.OnchainNft(
        nft_id=nft_id,
        nft_number=number,
        owner="rOwner",
        is_burned=False,
        mutable=mutable,
        uri_hex="6868",
        body=body_class,
        attributes=attrs if attrs is not None else _attrs(),
        image="",
        ledger_index=ledger,
    )


def test_non_body_slots_excludes_body():
    assert "Body" not in trait_economy.NON_BODY_SLOTS
    assert len(trait_economy.NON_BODY_SLOTS) == 8
    assert "Background" in trait_economy.NON_BODY_SLOTS


def test_slot_value_defaults_to_none():
    rec = _nft("A", 1, attrs=_attrs(Background="Sky"))
    assert trait_economy.slot_value(rec, "Background") == "Sky"
    assert trait_economy.slot_value(rec, "Head") == "None"


def test_dedupe_prefers_mutable_then_newest_ledger():
    a = _nft("imm-old", 5, mutable=False, ledger=10)
    b = _nft("mut-old", 5, mutable=True, ledger=20)
    c = _nft("mut-new", 5, mutable=True, ledger=99)
    canonical, recon = trait_economy.dedupe_editions([a, b, c], max_edition=10)
    assert canonical[5].nft_id == "mut-new"
    assert recon["duplicates"][5] == ["mut-old", "imm-old"]


def test_dedupe_classifies_missing_unparsed_out_of_range():
    good = _nft("g", 2)
    unparsed = _nft("u", None)
    oor = _nft("o", 9999)
    canonical, recon = trait_economy.dedupe_editions([good, unparsed, oor], max_edition=3)
    assert set(canonical) == {2}
    assert recon["missing"] == [1, 3]
    assert recon["unparsed"] == ["u"]
    assert recon["out_of_range"] == ["o"]
