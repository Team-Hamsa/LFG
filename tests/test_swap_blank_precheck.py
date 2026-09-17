# tests/test_swap_blank_precheck.py
# #523: a harvested (blank) character counts as 0 assets in the census. A
# trait swap that lands a real value on ONE side of a blank turns it into a
# partly-dressed character (9 assets: 8 None + the item) with no
# supply_changes row to cover the difference -- the same "phantom None set"
# equip produces (see lfg_core/trait_economy.py's can_equip). Assemble is the
# only supported way to dress a blank, so run_swap_session refuses a blank
# side outright, before any compose/upload/payment/on-chain work.
#
# lfg_service.app.handle_swap_start already refuses this before a SwapSession
# even exists (tests/test_swap_cross_body_api.py); this file is defense in
# depth for run_swap_session itself, mirroring
# tests/test_swap_trustline_precheck.py's style: the guard runs so early that
# NOTHING else needs stubbing -- if it weren't first, this test would crash
# reaching the network/layer store instead of failing cleanly.
#
# Env-guard preamble: importing lfg_core.config freezes its constants at import
# time; set the same defaults test_smoke.py uses so collection order can't
# strand them. (Copy the block verbatim from tests/test_swap_offer_recovery.py.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import sys  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from lfg_core import swap_flow, swap_meta, trait_economy  # noqa: E402


def _run(coro):
    # Repo convention (see tests/test_signing_account.py): a fresh loop that is
    # never set as the thread's current loop, so it doesn't strand loop state
    # for later tests the way asyncio.run() does.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _nft(*, blank: bool = False) -> dict:
    attrs = trait_economy.blank_attributes() if blank else []
    return {
        "name": "Let's Effing Go! #1",
        "number": 1,
        "nft_id": "00" * 32,
        "gender": "male",
        "mutable": True,
        "attributes": attrs,
        # Mirrors swap_meta.normalize_nft's output shape (#523 P5): "blank"
        # is derived from the RAW pre-normalization attributes, separately
        # from "attributes" (which normalize_attributes always pads to
        # all-"None" for ANY missing data, blank or not).
        "blank": blank,
    }


def _make_session(*, blank1: bool, blank2: bool) -> swap_flow.SwapSession:
    return swap_flow.SwapSession(
        discord_id="d",
        wallet_address="rUSERxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        nft1=_nft(blank=blank1),
        nft2=_nft(blank=blank2),
        traits_to_swap=["Accessory"],
    )


def test_run_swap_session_refuses_blank_first_side_before_any_work():
    s = _make_session(blank1=True, blank2=False)
    _run(swap_flow.run_swap_session(s))

    assert s.state == swap_flow.FAILED
    assert s.error == trait_economy.BLANK_CHARACTER_ERROR


def test_run_swap_session_refuses_blank_second_side_before_any_work():
    s = _make_session(blank1=False, blank2=True)
    _run(swap_flow.run_swap_session(s))

    assert s.state == swap_flow.FAILED
    assert s.error == trait_economy.BLANK_CHARACTER_ERROR


# --- normalize_nft's "blank" field (#523 review, Greptile P2) -------------
# swap_meta.normalize_attributes() unconditionally pads EVERY missing slot to
# "None", so a dressed character with unreadable/absent metadata attributes
# and a genuine harvested blank end up with byte-identical "attributes"
# fields. normalize_nft must derive "blank" from the RAW pre-normalization
# attributes, where that distinction still exists, or the swap/equip blank
# guard above would misfire on a data hiccup, not just a real blank.


def test_normalize_nft_blank_field_true_for_genuine_blank():
    rec = swap_meta.normalize_nft(
        "00" * 32,
        {"name": "Let's Effing Go! #1", "attributes": trait_economy.blank_attributes()},
        flags=16,
    )
    assert rec is not None
    assert rec["blank"] is True


def test_normalize_nft_blank_field_false_for_unreadable_metadata():
    """A valid name but no "attributes" key at all -- e.g. malformed or
    partially-unreadable metadata -- must NOT be flagged blank, even though
    the padded "attributes" field this same call produces is indistinguishable
    from a genuine blank's (every slot explicitly "None")."""
    rec = swap_meta.normalize_nft("00" * 32, {"name": "Let's Effing Go! #1"}, flags=16)
    assert rec is not None
    assert rec["blank"] is False
    assert all(a["value"] == "None" for a in rec["attributes"])


def test_normalize_nft_blank_field_false_for_dressed():
    rec = swap_meta.normalize_nft(
        "00" * 32,
        {
            "name": "Let's Effing Go! #1",
            "attributes": [{"trait_type": "Body", "value": "Straight Blue"}],
        },
        flags=16,
    )
    assert rec is not None
    assert rec["blank"] is False
