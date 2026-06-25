# tests/test_session_platform_fields.py
# BV1: verify that SwapSession, HarvestSession, AssembleSession, EquipSession
# all accept a `platform` keyword arg (default "discord"), store it on self,
# and (where to_dict exists) emit it in the serialised dict.
#
# HarvestSession / AssembleSession / EquipSession are @dataclass and have NO
# to_dict() — the test only asserts on the attribute. SwapSession has to_dict(),
# so its round-trip is also tested.
#
# EconomyWebSession (webapp/economy_api.py) is the OUTER wrapper actually stored
# in the economy_sessions dict (has discord_id/id/state/to_dict) — the one BV2's
# ownership check sees — so it gets the field + to_dict round-trip too.

from types import SimpleNamespace

from lfg_core import trait_economy as te
from lfg_core.economy_flow import AssembleSession, EquipSession, HarvestSession
from lfg_core.nft_index import OnchainNft
from lfg_core.swap_flow import SwapSession
from webapp.economy_api import EconomyWebSession


def _nft() -> dict:
    """Minimal SwapSession nft arg — needs 'name' and 'image' for to_dict()."""
    return {"NFTokenID": "x", "attributes": [], "name": "NFT #1", "image": "https://cdn/1.png"}


def _onchain_nft() -> OnchainNft:
    """Minimal OnchainNft for HarvestSession / EquipSession."""
    attrs = [{"trait_type": "Body", "value": "Straight Blue"}]
    attrs += [{"trait_type": s, "value": "None"} for s in te.NON_BODY_SLOTS]
    return OnchainNft(
        nft_id="NFT7",
        nft_number=7,
        owner="rUser",
        is_burned=False,
        mutable=True,
        uri_hex="",
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def test_swap_session_platform_default_and_explicit():
    s = SwapSession("9", "rA", _nft(), _nft(), ["Hat"])
    assert s.platform == "discord"
    assert s.to_dict()["platform"] == "discord"

    t = SwapSession("55", "rA", _nft(), _nft(), ["Hat"], platform="telegram")
    assert t.platform == "telegram"
    assert t.to_dict()["platform"] == "telegram"


def test_harvest_session_platform_default_and_explicit():
    s = HarvestSession(owner="rUser", character=_onchain_nft(), burnable=True)
    assert s.platform == "discord"

    t = HarvestSession(owner="rUser", character=_onchain_nft(), burnable=True, platform="telegram")
    assert t.platform == "telegram"


def test_assemble_session_platform_default_and_explicit():
    chosen = dict.fromkeys(te.NON_BODY_SLOTS, "None")
    s = AssembleSession(
        owner="rUser",
        edition=7,
        chosen=chosen,
        body_value="Straight Blue",
        body_class="male",
    )
    assert s.platform == "discord"

    t = AssembleSession(
        owner="rUser",
        edition=7,
        chosen=chosen,
        body_value="Straight Blue",
        body_class="male",
        platform="telegram",
    )
    assert t.platform == "telegram"


def test_equip_session_platform_default_and_explicit():
    s = EquipSession(owner="rUser", character=_onchain_nft(), slot="Head", incoming_value="Crown")
    assert s.platform == "discord"

    t = EquipSession(
        owner="rUser",
        character=_onchain_nft(),
        slot="Head",
        incoming_value="Crown",
        platform="telegram",
    )
    assert t.platform == "telegram"


def test_economy_web_session_platform_default_and_explicit():
    # Minimal fake inner: economy_session_dict needs .id/.state/.error, and the
    # "equip" branch reads .displaced_value.
    inner = SimpleNamespace(id="x", state="running", error=None, displaced_value="")
    s = EconomyWebSession(discord_id="9", kind="equip", inner=inner)
    assert s.platform == "discord"
    assert s.to_dict()["platform"] == "discord"

    t = EconomyWebSession(discord_id="55", kind="equip", inner=inner, platform="telegram")
    assert t.platform == "telegram"
    assert t.to_dict()["platform"] == "telegram"
