# Drives the background firehose consumer (surfaces/discord_bot/events). Events
# mirror the REAL service publish shape (lfg_service/app.py:419): identity is
# {"platform": "discord", "platform_user_id": <id>}, data is the mint session
# to_dict (carries nft_number). The /events firehose is cross-surface, so the
# discord-specific DM/mention is gated on platform == "discord".
import asyncio

import pytest

from lfg_service.events import Event


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _discord_identity(uid: str) -> dict:
    return {"platform": "discord", "platform_user_id": uid}


@pytest.fixture
def ev_mod(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
        "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rIssuer",
        "TOKEN_CURRENCY_HEX": "ABC",
    }.items():
        monkeypatch.setenv(k, v)
    import importlib

    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.discord_bot.events as ev

    importlib.reload(ev)
    return ev


class _FakeAgen:
    def __init__(self, items):
        self._items = list(items)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class _FakeSvc:
    def __init__(self, agen):
        self._agen = agen

    def events(self, types=None):
        self.types = types
        return self._agen


def test_make_announcement_completed(ev_mod):
    e = Event(
        type="mint.completed",
        ts=0,
        identity=_discord_identity("42"),
        wallet=None,
        data={"nft_number": 3600},
    )
    msg = ev_mod.make_announcement(e)
    assert "3600" in msg and "<@42>" in msg


def test_make_announcement_failed(ev_mod):
    e = Event(
        type="mint.failed",
        ts=0,
        identity=_discord_identity("42"),
        wallet=None,
        data={"nft_number": 3600},
    )
    msg = ev_mod.make_announcement(e)
    assert "3600" in msg and "<@42>" in msg and "ailed" in msg


def test_make_announcement_non_discord_has_no_mention(ev_mod):
    e = Event(
        type="mint.completed",
        ts=0,
        identity={"platform": "webapp", "platform_user_id": "wallet123"},
        wallet=None,
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "<@" not in msg and "#7" in msg


def test_make_announcement_pings_linked_discord_identity(ev_mod):
    # Cross-surface mint (webapp) by someone who ALSO linked Discord -> ping them.
    e = Event(
        type="mint.completed",
        ts=0,
        identity={
            "platform": "webapp",
            "platform_user_id": "w",
            "display_handle": "alice",
            "linked": [
                {"platform": "discord", "platform_user_id": "999", "display_handle": "alice"}
            ],
        },
        wallet=None,
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "<@999>" in msg


def test_make_announcement_uses_display_handle_when_no_discord(ev_mod):
    e = Event(
        type="mint.completed",
        ts=0,
        identity={"platform": "telegram", "platform_user_id": "55", "display_handle": "alice"},
        wallet=None,
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "<@" not in msg and "alice" in msg and "a user" not in msg


def test_make_announcement_falls_back_to_wallet(ev_mod):
    e = Event(
        type="mint.completed",
        ts=0,
        identity={"platform": "telegram", "platform_user_id": "55", "display_handle": None},
        wallet="rWALLET123",
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "rWALLET123" in msg and "a user" not in msg


def test_make_announcement_falls_back_to_a_user(ev_mod):
    e = Event(
        type="mint.completed",
        ts=0,
        identity=None,
        wallet=None,
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "a user" in msg


def test_run_event_loop_announces_dms_and_closes(ev_mod):
    agen = _FakeAgen(
        [
            Event(
                type="mint.completed",
                ts=0,
                identity=_discord_identity("42"),
                wallet=None,
                data={"nft_number": 3600},
            )
        ]
    )
    svc = _FakeSvc(agen)
    sent, dmed = [], []

    async def announce(m, image):
        sent.append((m, image))

    async def dm(uid, m, image):
        dmed.append((uid, m, image))

    _run(ev_mod.run_event_loop(svc, announce, dm))
    assert svc.types == ["mint.completed", "mint.failed"]
    assert sent and "3600" in sent[0][0]
    # no image_url in data -> image arg is None
    assert sent[0][1] is None
    assert dmed == [("42", sent[0][0], None)]
    assert agen.closed is True


def test_run_event_loop_passes_image_url_through(ev_mod):
    agen = _FakeAgen(
        [
            Event(
                type="mint.completed",
                ts=0,
                identity=_discord_identity("42"),
                wallet=None,
                data={"nft_number": 3600, "image_url": "https://cdn/x.png"},
            )
        ]
    )
    svc = _FakeSvc(agen)
    sent, dmed = [], []

    async def announce(m, image):
        sent.append((m, image))

    async def dm(uid, m, image):
        dmed.append((uid, m, image))

    _run(ev_mod.run_event_loop(svc, announce, dm))
    assert sent[0][1] == "https://cdn/x.png"
    assert dmed == [("42", sent[0][0], "https://cdn/x.png")]


def test_run_event_loop_no_image_on_failed(ev_mod):
    agen = _FakeAgen(
        [
            Event(
                type="mint.failed",
                ts=0,
                identity=_discord_identity("42"),
                wallet=None,
                data={"nft_number": 1, "image_url": "https://cdn/x.png"},
            )
        ]
    )
    sent = []

    async def announce(m, image):
        sent.append((m, image))

    _run(ev_mod.run_event_loop(_FakeSvc(agen), announce, None))
    # mint.failed -> announcement_image is None even if data carries a url
    assert sent[0][1] is None


def test_run_event_loop_no_dm_for_failed_or_non_discord(ev_mod):
    agen = _FakeAgen(
        [
            Event(  # failed -> announced but NOT DMed
                type="mint.failed",
                ts=0,
                identity=_discord_identity("42"),
                wallet=None,
                data={"nft_number": 1},
            ),
            Event(  # non-discord completed -> announced but NOT DMed
                type="mint.completed",
                ts=0,
                identity={"platform": "webapp", "platform_user_id": "w"},
                wallet=None,
                data={"nft_number": 2},
            ),
        ]
    )
    sent, dmed = [], []

    async def announce(m, image):
        sent.append(m)

    async def dm(uid, m, image):
        dmed.append((uid, m))

    _run(ev_mod.run_event_loop(_FakeSvc(agen), announce, dm))
    assert len(sent) == 2
    assert dmed == []
    assert agen.closed is True


def test_run_event_loop_survives_handler_error(ev_mod):
    agen = _FakeAgen(
        [
            Event(
                type="mint.completed",
                ts=0,
                identity=_discord_identity("1"),
                wallet=None,
                data={"nft_number": 1},
            ),
            Event(
                type="mint.completed",
                ts=0,
                identity=_discord_identity("2"),
                wallet=None,
                data={"nft_number": 2},
            ),
        ]
    )
    calls = {"n": 0}

    async def announce(m, image):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # first event blows up; loop must continue

    _run(ev_mod.run_event_loop(_FakeSvc(agen), announce, None))
    assert calls["n"] == 2  # second event still processed
    assert agen.closed is True
