# Provenance Memos (#54): a single source of truth for the who/what/where memo
# schema stamped onto every XRPL transaction alongside the Make Waves SourceTag
# (#61). Values are a closed enum; the two builders emit the same schema in the
# two wire shapes the app needs — XUMM txjson JSON and xrpl-py Memo models.

import pytest
from xrpl.models.transactions import Memo
from xrpl.utils import hex_to_str

from lfg_core import memos


def _decode_json_entry(entry: dict) -> tuple[str, str, str]:
    m = entry["Memo"]
    return (
        hex_to_str(m["MemoType"]),
        hex_to_str(m["MemoData"]),
        hex_to_str(m["MemoFormat"]),
    )


def test_build_memos_json_has_three_required_entries():
    arr = memos.build_memos_json(
        memos.INITIATOR_USER, memos.PLATFORM_DISCORD_ACTIVITY, memos.ACTION_MINT
    )
    decoded = [_decode_json_entry(e) for e in arr]
    assert decoded == [
        ("initiator", "user", "text/plain"),
        ("platform", "discord-activity", "text/plain"),
        ("action", "mint", "text/plain"),
    ]


def test_build_memos_json_includes_optional_campaign_only_when_present():
    without = memos.build_memos_json(
        memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_BURN
    )
    assert len(without) == 3
    with_campaign = memos.build_memos_json(
        memos.INITIATOR_USER,
        memos.PLATFORM_TELEGRAM,
        memos.ACTION_BUY,
        campaign="comeback-2026",
    )
    assert len(with_campaign) == 4
    assert _decode_json_entry(with_campaign[-1]) == ("campaign", "comeback-2026", "text/plain")


def test_build_memo_models_returns_hex_encoded_xrpl_memos():
    models = memos.build_memo_models(
        memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_MODIFY
    )
    assert all(isinstance(m, Memo) for m in models)
    decoded = [
        (hex_to_str(m.memo_type), hex_to_str(m.memo_data), hex_to_str(m.memo_format))
        for m in models
    ]
    assert decoded == [
        ("initiator", "backend", "text/plain"),
        ("platform", "backend", "text/plain"),
        ("action", "modify", "text/plain"),
    ]


@pytest.mark.parametrize(
    "initiator,platform,action",
    [
        ("nobody", memos.PLATFORM_WEBAPP, memos.ACTION_MINT),
        (memos.INITIATOR_USER, "myspace", memos.ACTION_MINT),
        (memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, "frobnicate"),
    ],
)
def test_rejects_values_outside_the_closed_enum(initiator, platform, action):
    with pytest.raises(ValueError):
        memos.build_memos_json(initiator, platform, action)
    with pytest.raises(ValueError):
        memos.build_memo_models(initiator, platform, action)


@pytest.mark.parametrize(
    "bad_campaign",
    [
        "has space",  # whitespace
        "UPPER",  # uppercase
        "josh@example.com",  # PII/email-like
        "a" * 33,  # too long
        "emoji😀",  # non-ascii
    ],
)
def test_rejects_unsafe_campaign_tags(bad_campaign):
    # campaign is written permanently + publicly on-ledger, so it must be a
    # constrained admin/config tag ([a-z0-9-], bounded) — never free/user text.
    with pytest.raises(ValueError):
        memos.build_memos_json(
            memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_MINT, campaign=bad_campaign
        )
    with pytest.raises(ValueError):
        memos.build_memo_models(
            memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_MINT, campaign=bad_campaign
        )


def test_accepts_well_formed_campaign_tag():
    arr = memos.build_memos_json(
        memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_MINT, campaign="comeback-2026"
    )
    assert len(arr) == 4


def test_payload_stays_under_the_1kb_memo_limit():
    # Longest realistic combination must still fit the XRPL per-tx memo budget.
    arr = memos.build_memos_json(
        memos.INITIATOR_USER,
        memos.PLATFORM_DISCORD_ACTIVITY,
        memos.ACTION_TRAIT_SWAP_FEE,
        campaign="comeback-2026",
    )
    total_bytes = sum(
        len(e["Memo"][k]) // 2 for e in arr for k in ("MemoType", "MemoData", "MemoFormat")
    )
    assert total_bytes < 1024
