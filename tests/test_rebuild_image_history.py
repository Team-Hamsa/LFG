# tests/test_rebuild_image_history.py
# Historical NFT versions: on mainnet every legacy trait swap was a
# burn+remint, so an edition's visual history is the ordered succession of
# its tokens (attributes preserved for burned ones via the Bithomp import).
# scripts/rebuild_image_history.py recomposes each prior version into
# images_<network>/history/<edition>/ for the future evolution-slideshow
# feature; these tests cover its pure ordering/dedupe helpers.
#
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them.
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

import sys  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import rebuild_image_history as rih  # noqa: E402

_A = [{"trait_type": "Body", "value": "White Skeleton"}]
_B = [{"trait_type": "Body", "value": "Buck"}]


def _v(nft_id, attrs, *, ts=None, ledger_index=None, live=False):
    return {
        "nft_id": nft_id,
        "attrs": attrs,
        "body": "skeleton",
        "ts": ts,
        "ledger_index": ledger_index,
        "live": live,
    }


# ------------------------------------------------------------- ordering


def test_order_versions_by_mint_ts_live_last():
    vs = [
        _v("NEW", _B, ts=200, live=True),
        _v("OLD", _A, ts=100),
    ]
    out = rih.order_versions(vs)
    assert [v["nft_id"] for v in out] == ["OLD", "NEW"]


def test_order_versions_missing_ts_falls_back_to_ledger_index():
    vs = [
        _v("C", _B, ts=None, ledger_index=300, live=True),
        _v("B", _B, ts=None, ledger_index=200),
        _v("A", _A, ts=50, ledger_index=999),  # a real ts sorts before fallbacks
    ]
    out = rih.order_versions(vs)
    assert [v["nft_id"] for v in out] == ["A", "B", "C"]


def test_order_versions_ties_put_live_token_last():
    vs = [
        _v("LIVE", _B, ts=None, ledger_index=None, live=True),
        _v("DEAD", _A, ts=None, ledger_index=None),
    ]
    out = rih.order_versions(vs)
    assert [v["nft_id"] for v in out] == ["DEAD", "LIVE"]


# --------------------------------------------------------------- dedupe


def test_mark_duplicates_consecutive_identical_attrs():
    # A remint that changed nothing visually shouldn't cost a compose — the
    # slideshow would show two identical frames.
    out = rih.mark_duplicates([_v("A", _A), _v("B", _A), _v("C", _B)])
    assert [v.get("same_as_prev", False) for v in out] == [False, True, False]


def test_mark_duplicates_attribute_order_is_canonicalized():
    a1 = [
        {"trait_type": "Body", "value": "Buck"},
        {"trait_type": "Head", "value": "Cap"},
    ]
    a2 = [
        {"trait_type": "Head", "value": "Cap"},
        {"trait_type": "Body", "value": "Buck"},
    ]
    out = rih.mark_duplicates([_v("A", a1), _v("B", a2)])
    assert out[1]["same_as_prev"] is True


def test_mark_duplicates_non_consecutive_repeat_is_not_marked():
    # A -> B -> A again: the return to a previous look IS a distinct frame.
    out = rih.mark_duplicates([_v("A", _A), _v("B", _B), _v("C", _A)])
    assert [v.get("same_as_prev", False) for v in out] == [False, False, False]
