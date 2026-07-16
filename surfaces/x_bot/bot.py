"""X (Twitter) poster surface entrypoint (#41).

Mirrors the Telegram announce consumer (`surfaces/telegram_bot/events.py` +
`bot.py`): a reconnecting `/events` firehose subscription feeding a small,
independently-testable per-event pipeline. One deliberate improvement over
that template: `stream_events` raises `AuthError` immediately on a rejected
(401/403) `/events` handshake, and the Telegram loop does not catch it (the
events task dies silently). Here it propagates out of `run_event_loop` (after
releasing the WebSocket) so `main()` can fail loudly with `sys.exit(1)`.

Pipeline (`handle_event`): dedup -> global 429 backoff -> pause check ->
budget check -> compose -> image download -> media upload -> post -> record.
Dedup is checked, and only checked, before anything is ever recorded for a
given event key — `state.record()` upserts, so recording a second status
(e.g. a later `failed`) for an already-`posted` key would downgrade it; the
pipeline order here guarantees that never happens (state.py's T4 caveat).

Fire-and-forget by construction: this is a separate process reading the
firehose. Nothing here can block, slow, or fail a mint — a bad event, a dead
X API, or a rate limit only affects this surface's own `x_posts` bookkeeping.

No XRPL transactions anywhere in this feature -> no SourceTag surface.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

import aiohttp

from lfg_core import config, rarity
from lfg_service.events import Event  # shared event dataclass (allowed cross-import)
from surfaces._client.errors import AuthError
from surfaces._client.events import stream_events
from surfaces.x_bot import config as x_config
from surfaces.x_bot import poster, state
from surfaces.x_bot.poster import RankTraits
from surfaces.x_bot.x_api import XApi, XApiError

T = TypeVar("T")

# Only `mint.completed` is ever tweet-worthy today (poster.should_post()) —
# expanding to assemble.completed/etc. later (spec §9) is a one-line change
# to this list plus should_post(), nothing structural.
_EVENT_TYPES = ["mint.completed"]

_RETRY_MAX_ATTEMPTS = 3
# Exponential backoff schedule (spec §5.5: "1s/4s/16s"). Only the first
# _RETRY_MAX_ATTEMPTS - 1 entries are ever slept on — with the current
# 3-attempt cap that's 1s then 4s; the trailing 16s slots in unchanged if the
# cap is ever raised to 4 attempts.
_RETRY_DELAYS: tuple[float, ...] = (1.0, 4.0, 16.0)

# 429 with no reset header present (defensive default; the header is normally
# always there per x_api.py's `_earliest_reset`): back off 15 minutes.
_DEFAULT_BACKOFF_SECONDS = 900.0


@dataclass
class Deps:
    """Everything `handle_event` needs, injected so tests drive the pipeline
    without a live stream, a real X account, or real sleeps.

    `backoff_until` is mutated in place by `handle_event` on a 429 — callers
    share one `Deps` instance across the whole event loop so the global
    backoff actually applies to subsequent events.
    """

    x_api: XApi
    http: aiohttp.ClientSession
    db_path: str
    budget: int
    rank_traits: RankTraits | None = None
    now: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    backoff_until: float = 0.0  # epoch seconds


def _utc(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _is_retryable_x_error(exc: Exception) -> bool:
    if isinstance(exc, XApiError):
        return exc.status >= 500
    return isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError))


async def _retry(
    op: Callable[[], Awaitable[T]],
    *,
    should_retry: Callable[[Exception], bool],
    sleep: Callable[[float], Awaitable[None]],
) -> T:
    """Call `op`, retrying up to `_RETRY_MAX_ATTEMPTS` times total while
    `should_retry(exc)` is true, sleeping `_RETRY_DELAYS` between attempts.
    The last exception (retryable-but-exhausted, or the first non-retryable
    one) always propagates — callers decide what a failure means."""
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return await op()
        except Exception as exc:
            if attempt == _RETRY_MAX_ATTEMPTS - 1 or not should_retry(exc):
                raise
            await sleep(_RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")  # pragma: no cover


class _DownloadError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"image download failed: HTTP {status}")


def _is_retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, _DownloadError):
        return exc.status >= 500
    return isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError))


async def _fetch_image(url: str, http: aiohttp.ClientSession) -> bytes:
    async with http.get(url) as resp:
        if resp.status >= 400:
            raise _DownloadError(resp.status)
        body: bytes = await resp.read()
        return body


async def _download_image(url: str, deps: Deps) -> bytes | None:
    """Best-effort image download; any failure (after retries) degrades to a
    text-only tweet rather than dropping the post (spec §5.4)."""
    try:
        return await _retry(
            lambda: _fetch_image(url, deps.http),
            should_retry=_is_retryable_download_error,
            sleep=deps.sleep,
        )
    except Exception as exc:
        logging.warning(f"x_bot: image download failed ({exc}); degrading to text-only tweet")
        return None


async def _upload_media(image_bytes: bytes, deps: Deps) -> str | None:
    """Uploads the mint image; returns the media_id, or None to degrade to a
    text-only tweet on any non-429 failure. A 429 is NOT swallowed here — it
    propagates so the caller applies the global backoff and fails the whole
    post (posting anyway after a 429 would keep burning API quota)."""
    try:
        return await _retry(
            lambda: deps.x_api.upload_media(image_bytes),
            should_retry=_is_retryable_x_error,
            sleep=deps.sleep,
        )
    except XApiError as exc:
        if exc.status == 429:
            raise
        logging.warning(f"x_bot: media upload failed ({exc}); degrading to text-only tweet")
        return None


def _apply_429_backoff(exc: XApiError, deps: Deps) -> None:
    deps.backoff_until = (
        exc.reset_at if exc.reset_at is not None else deps.now() + _DEFAULT_BACKOFF_SECONDS
    )
    logging.error(
        f"x_bot: rate limited (429); pausing all posting until epoch {deps.backoff_until:.0f}"
    )


# Reverse of rarity.LFG_COLUMN_FOR_CATEGORY: LFG traits-dict keys (e.g. "Hat")
# -> trait_rarity.category names (e.g. "Head"). Built once at import time.
_CATEGORY_FOR_LFG_COLUMN: dict[str, str] = {
    column: category for category, column in rarity.LFG_COLUMN_FOR_CATEGORY.items()
}


def rank_traits_by_rarity(traits: dict[str, str], body_type: str | None) -> list[tuple[str, str]]:
    """The real `poster.compose()` ranker: rarest-first ordering of a minted
    NFT's trait slots via `lfg_core.rarity.get_odds`.

    `traits` uses LFG column naming (e.g. "Hat", not the layer-store/rarity
    table's "Head" — see `rarity.LFG_COLUMN_FOR_CATEGORY`); each slot is
    translated back to its rarity-table category before the lookup.

    With no `body_type` (rarity weights are scoped per body class), ranking
    is impossible, so slots come back in insertion order unchanged — the
    same no-op shape `compose()` itself falls back to.

    A slot/value with no `trait_rarity` row (a historical NFT predating
    variable rarity, or a not-yet-synced trait — see recon-rarity.md §8) is
    not silently assigned a made-up weight: it is placed after every ranked
    slot, in its original relative order, so the tweet's displayed trait
    count never silently drops a slot.

    `rarity.connect()` (no explicit db_path) resolves `config.DB_PATH`, the
    same network-aware app DB the mint process itself writes rarity updates
    to — x_bot runs with the full repo .env, same as the Telegram surface.
    """
    if not body_type:
        return list(traits.items())
    conn = rarity.connect()
    try:
        rarity.ensure_schema(conn)
        weighted: list[tuple[str, str, float]] = []
        unranked: list[tuple[str, str]] = []
        for slot, value in traits.items():
            category = _CATEGORY_FOR_LFG_COLUMN.get(slot, slot)
            rows = rarity.get_odds(conn, body_type, category)
            match = next((r for r in rows if r[0] == value), None)
            if match is None:
                unranked.append((slot, value))
            else:
                weighted.append((slot, value, match[3]))
        weighted.sort(key=lambda item: item[2])  # ascending weight = rarest first
        return [(slot, value) for slot, value, _weight in weighted] + unranked
    finally:
        conn.close()


async def handle_event(event: Mapping[str, Any], deps: Deps) -> str | None:
    """The per-event pipeline. Returns the status recorded for this event
    (`"posted"` / `"skipped_paused"` / `"skipped_budget"` / `"failed"`), or
    `None` when nothing was recorded at all — not tweet-worthy, or already
    posted (dedup never re-records a key that's already `posted`: state.py's
    `record()` upserts, so recording anything else afterward would downgrade
    it — the dedup check below runs before any `state.record()` call, for
    every code path, to guarantee that can't happen).
    """
    event_key = poster.should_post(event)
    if event_key is None:
        return None
    if state.already_posted(deps.db_path, event_key):
        logging.info(f"x_bot: {event_key} already posted, skipping (dedup)")
        return None

    now = deps.now()
    if now < deps.backoff_until:
        logging.warning(
            f"x_bot: in 429 backoff until epoch {deps.backoff_until:.0f} "
            f"(now={now:.0f}); recording {event_key} failed without calling the API"
        )
        state.record(deps.db_path, event_key, "failed")
        return "failed"

    if state.posting_paused(deps.db_path):
        logging.info(f"x_bot: posting paused; skipping {event_key}")
        state.record(deps.db_path, event_key, "skipped_paused")
        return "skipped_paused"

    if state.month_count(deps.db_path, now=_utc(now)) >= deps.budget:
        logging.info(f"x_bot: monthly budget ({deps.budget}) reached; skipping {event_key}")
        state.record(deps.db_path, event_key, "skipped_budget")
        return "skipped_budget"

    text = poster.compose(event, rank_traits=deps.rank_traits)
    media_id: str | None = None
    image_url = (event.get("data") or {}).get("image_url")
    if image_url:
        image_bytes = await _download_image(image_url, deps)
        if image_bytes is not None:
            try:
                media_id = await _upload_media(image_bytes, deps)
            except XApiError as exc:
                _apply_429_backoff(exc, deps)
                logging.error(f"x_bot: media upload rate-limited; failing {event_key} ({exc})")
                state.record(deps.db_path, event_key, "failed")
                return "failed"

    try:
        tweet_id = await _retry(
            lambda: deps.x_api.post_tweet(text, media_id),
            should_retry=_is_retryable_x_error,
            sleep=deps.sleep,
        )
    except XApiError as exc:
        if exc.status == 429:
            _apply_429_backoff(exc, deps)
        logging.error(f"x_bot: post_tweet failed for {event_key} ({exc})")
        state.record(deps.db_path, event_key, "failed")
        return "failed"

    state.record(deps.db_path, event_key, "posted", tweet_id=tweet_id)
    logging.info(f"x_bot: posted {event_key} as tweet {tweet_id}")
    return "posted"


async def run_event_loop(http: aiohttp.ClientSession, deps: Deps) -> None:
    """Consume the `/events` firehose forever, mirroring the Telegram announce
    consumer's shape (`surfaces/telegram_bot/events.py`) with one deliberate
    improvement: `AuthError` from a rejected `/events` handshake is allowed to
    propagate (after `aclose()`) so `main()` can fail loudly, rather than
    dying silently the way the Telegram template does (recon-events.md #7)."""
    # stream_events() is implemented as an async generator (it aclose()s fine
    # at runtime) but is annotated to return the narrower AsyncIterator[Event]
    # — cast so the aclose() call below type-checks under strict mypy.
    agen = cast(
        "AsyncGenerator[Event, None]",
        stream_events(
            http,
            x_config.LFG_SERVICE_URL,
            x_config.SERVICE_TOKEN_X,
            _EVENT_TYPES,
            base_delay=x_config.RETRY_BASE_DELAY,
        ),
    )
    try:
        async for event in agen:
            try:
                await handle_event(event.to_dict(), deps)
            except Exception as exc:  # never let one bad event kill the loop
                logging.error(f"x_bot: event handler error: {exc}")
    finally:
        await agen.aclose()


async def _verify_and_build_deps(http: aiohttp.ClientSession) -> Deps:
    x_api = XApi(
        config.X_CONSUMER_KEY,
        config.X_CONSUMER_SECRET,
        config.X_ACCESS_TOKEN,
        config.X_ACCESS_SECRET,
        http,
    )
    handle = await x_api.verify_credentials()  # raises XApiError on bad creds
    logging.info(f"x_bot: X credentials verified for @{handle}")
    return Deps(
        x_api=x_api,
        http=http,
        db_path=config.X_STATE_DB_PATH,
        budget=config.X_MONTHLY_POST_BUDGET,
        rank_traits=rank_traits_by_rarity,
    )


async def _async_main() -> None:
    async with aiohttp.ClientSession() as http:
        deps = await _verify_and_build_deps(http)
        await run_event_loop(http, deps)


def main() -> None:
    if not config.X_ENABLED:
        logging.info(
            "x_bot: X_ENABLED is false (or OAuth credentials incomplete) — "
            "the X poster is off; exiting cleanly (pm2: exit 0, no restart loop)."
        )
        sys.exit(0)
    if not x_config.SERVICE_TOKEN_X:
        logging.error("x_bot: SERVICE_TOKEN_X is not set; cannot subscribe to /events. Exiting.")
        sys.exit(1)
    try:
        asyncio.run(_async_main())
    except XApiError as exc:
        logging.error(f"x_bot: X credential verification failed ({exc}); exiting.")
        sys.exit(1)
    except AuthError as exc:
        logging.error(f"x_bot: /events subscription rejected ({exc}); exiting.")
        sys.exit(1)


if __name__ == "__main__":
    # Guard against the double-import footgun (see run_x.py / run_telegram.py):
    # running this file as `-m` would execute it twice under two different
    # module names, creating two independent module-level states.
    from surfaces.x_bot.bot import main as _canonical_main

    _canonical_main()
