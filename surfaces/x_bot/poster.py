"""Pure tweet composition + posting decision (#41).

No DB/network I/O here — `should_post()` decides whether a firehose event is
tweet-worthy and returns its dedup key; `compose()` turns an event into tweet
text. Both take the event dict shape the firehose delivers over `/events`:
`{"type": ..., "ts": ..., "identity": ..., "wallet": ..., "data": {...}}`
where `data` is `MintSession.to_dict()` (recon-events.md) — `traits` uses LFG
column naming (e.g. "Hat", not the layer-store's "Head" — see
`lfg_core/rarity.py`'s `LFG_COLUMN_FOR_CATEGORY`), and `body_type` is the
mint's body class ("male"/"female"/"ape"/...).

Rarity ranking needs a DB read, so it is injected rather than performed here
(keeps this module I/O-free and independently testable): `rank_traits(traits,
body_type) -> [(slot, value), ...]` rarest-first. `bot.py` (T5) supplies the
real implementation wrapping `lfg_core.rarity.get_odds`; when omitted, or
when it raises, `compose()` falls back to the traits dict's insertion order
rather than failing the whole tweet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

MAX_WEIGHTED_CHARS = 280
_TRAITS_SHOWN = 3
_ELLIPSIS = "…"

RankTraits = Callable[[dict[str, str], str | None], list[tuple[str, str]]]

# Codepoint ranges X's weighted-length algorithm counts as 2 (CJK + emoji);
# everything else (Latin, punctuation, digits) counts as 1 (A6).
_DOUBLE_WEIGHT_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),
    (0x2E80, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
    (0x2600, 0x27BF),
    (0x1F300, 0x1F64F),
    (0x1F900, 0x1F9FF),
    (0x20000, 0x3FFFD),
)


def should_post(event: Mapping[str, Any]) -> str | None:
    """Dedup key for a tweet-worthy event, or None to skip it.

    Only successful mints are tweet-worthy today: `mint.completed` with a
    non-empty `nft_id`. Every failure type (`mint.failed`, etc.) and every
    other event type (`swap.*`, `harvest.*`, ...) is never posted.
    """
    if event.get("type") != "mint.completed":
        return None
    data = event.get("data") or {}
    nft_id = data.get("nft_id")
    if not nft_id:
        return None
    return f"mint:{nft_id}"


def compose(event: Mapping[str, Any], rank_traits: RankTraits | None = None) -> str:
    """Compose tweet text for a `mint.completed` event.

    Always 2-3 lines: header, an optional traits summary, and the hashtags.
    The traits line is the only part ever truncated to fit 280 weighted
    characters — the header and hashtags are never cut.

    2026-07-17 user directive (overrides spec §5.3): the bithomp link is
    intentionally omitted from the auto-posted tweet — X bills $0.20 per post
    containing a URL vs $0.015 without, and the brand account doesn't need
    the outbound click.
    """
    data = event.get("data") or {}
    nft_number = data.get("nft_number")
    traits: dict[str, str] = data.get("traits") or {}
    body_type: str | None = data.get("body_type")

    ranked = _rank(traits, body_type, rank_traits)
    traits_line = _traits_line(ranked)

    number_str = str(nft_number) if nft_number is not None else "?"
    header = f"🎨 LFGO #{number_str} just minted!"
    hashtags = "#XRPL #NFT"

    fixed_weighted = _weighted_len(header) + _weighted_len("\n") + _weighted_len(hashtags)
    if traits_line:
        fixed_weighted += _weighted_len("\n")
    budget_for_traits = MAX_WEIGHTED_CHARS - fixed_weighted
    traits_line = _truncate_to_weight(traits_line, budget_for_traits)

    lines = [header]
    if traits_line:
        lines.append(traits_line)
    lines.append(hashtags)
    return "\n".join(lines)


def _rank(
    traits: dict[str, str], body_type: str | None, rank_traits: RankTraits | None
) -> list[tuple[str, str]]:
    if rank_traits is None:
        return list(traits.items())
    try:
        return list(rank_traits(traits, body_type))
    except Exception:
        return list(traits.items())


def _is_placeholder(value: str | None) -> bool:
    return value is None or value.strip().lower() in ("", "none")


def _traits_line(ranked: list[tuple[str, str]]) -> str:
    valid = [(slot, value) for slot, value in ranked if not _is_placeholder(value)]
    if not valid:
        return ""
    shown = valid[:_TRAITS_SHOWN]
    remainder = len(valid) - len(shown)
    line = " · ".join(f"{slot}: {value}" for slot, value in shown)
    if remainder > 0:
        line += f" (+{remainder} more)"
    return line


def _char_weight(ch: str) -> int:
    cp = ord(ch)
    for lo, hi in _DOUBLE_WEIGHT_RANGES:
        if lo <= cp <= hi:
            return 2
    return 1


def _weighted_len(text: str) -> int:
    return sum(_char_weight(c) for c in text)


def weighted_tweet_length(text: str) -> int:
    """X's weighted length of an assembled tweet: the plain per-char weighted
    sum (`_char_weight` — CJK/emoji count 2, everything else 1). Exposed (not
    underscored) because it's the correct way to assert a composed tweet fits
    the budget. The URL special case (X counts any t.co-wrapped URL as exactly
    23 regardless of literal length, A5/A6) was removed together with the
    2026-07-17 link-free directive — no composed tweet can contain a URL
    today; reinstate URL≡23 handling here if links are ever reintroduced."""
    return _weighted_len(text)


def _truncate_to_weight(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if _weighted_len(text) <= budget:
        return text
    target = max(budget - _weighted_len(_ELLIPSIS), 0)
    out: list[str] = []
    total = 0
    for ch in text:
        w = _char_weight(ch)
        if total + w > target:
            break
        out.append(ch)
        total += w
    return "".join(out).rstrip() + _ELLIPSIS
