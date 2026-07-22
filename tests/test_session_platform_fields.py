# tests/test_session_platform_fields.py
# BV1: verify that SwapSession (directly-used ownership object) and
# EconomyWebSession (outer economy wrapper) accept a `platform` keyword arg
# (default "discord"), store it on self, and emit it in to_dict().
#
# HarvestSession / AssembleSession / EquipSession had a dead `platform` field
# that was never set from the user's actual platform nor read for ownership
# checks (ownership goes through the EconomyWebSession wrapper). That field has
# been removed; tests for it have been dropped accordingly.
#
# SwapSession has to_dict() so its round-trip is tested.
# EconomyWebSession (webapp/economy_api.py) is the OUTER wrapper actually stored
# in the economy_sessions dict (has discord_id/id/state/to_dict) — the one BV2's
# ownership check sees — so it gets the field + to_dict round-trip too.

from types import SimpleNamespace

from lfg_core.swap_flow import SwapSession
from webapp.economy_api import EconomyWebSession


def _nft() -> dict:
    """Minimal SwapSession nft arg — needs 'name' and 'image' for to_dict()."""
    return {"NFTokenID": "x", "attributes": [], "name": "NFT #1", "image": "https://cdn/1.png"}


def test_swap_session_platform_default_and_explicit():
    s = SwapSession("9", "rA", _nft(), _nft(), ["Hat"])
    assert s.platform == "discord"
    assert s.to_dict()["platform"] == "discord"

    t = SwapSession("55", "rA", _nft(), _nft(), ["Hat"], platform="telegram")
    assert t.platform == "telegram"
    assert t.to_dict()["platform"] == "telegram"


def test_economy_web_session_platform_default_and_explicit():
    # Minimal fake inner: economy_session_dict needs .id/.state/.error, and the
    # "equip" branch reads .displaced (a dict of slot -> displaced value).
    inner = SimpleNamespace(id="x", state="running", error=None, displaced={})
    s = EconomyWebSession(discord_id="9", kind="equip", inner=inner)
    assert s.platform == "discord"
    assert s.to_dict()["platform"] == "discord"

    t = EconomyWebSession(discord_id="55", kind="equip", inner=inner, platform="telegram")
    assert t.platform == "telegram"
    assert t.to_dict()["platform"] == "telegram"
