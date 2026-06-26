import asyncio

from lfg_service.events import Event
from surfaces.telegram_bot import events as ev_mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tg_identity(uid):
    return {"platform": "telegram", "platform_user_id": uid}


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


def test_announcement_uses_minter_display_handle():
    e = Event(
        type="mint.completed",
        ts=0,
        identity={"platform": "telegram", "platform_user_id": "55", "display_handle": "alice"},
        wallet=None,
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "alice" in msg and "a user" not in msg and "#7" in msg


def test_announcement_falls_back_to_linked_handle():
    e = Event(
        type="mint.completed",
        ts=0,
        identity={
            "platform": "webapp",
            "platform_user_id": "w",
            "display_handle": None,
            "linked": [
                {"platform": "telegram", "platform_user_id": "55", "display_handle": "bob_tg"}
            ],
        },
        wallet=None,
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "bob_tg" in msg and "a user" not in msg


def test_announcement_falls_back_to_wallet():
    e = Event(
        type="mint.completed",
        ts=0,
        identity={"platform": "webapp", "platform_user_id": "w", "display_handle": None},
        wallet="rWALLET123",
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "rWALLET123" in msg and "a user" not in msg


def test_announcement_falls_back_to_a_user():
    e = Event(
        type="mint.completed",
        ts=0,
        identity=None,
        wallet=None,
        data={"nft_number": 7},
    )
    msg = ev_mod.make_announcement(e)
    assert "a user" in msg


def test_announce_and_dm_on_telegram_completed():
    agen = _FakeAgen(
        [
            Event(
                type="mint.completed",
                ts=0,
                identity=_tg_identity("55"),
                wallet=None,
                data={"nft_number": 3600},
            ),
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
    assert dmed == [("55", sent[0][0], None)]
    assert agen.closed is True


def test_image_url_passed_through_on_completed():
    agen = _FakeAgen(
        [
            Event(
                type="mint.completed",
                ts=0,
                identity=_tg_identity("55"),
                wallet=None,
                data={"nft_number": 3600, "image_url": "https://cdn/x.png"},
            ),
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
    assert dmed == [("55", sent[0][0], "https://cdn/x.png")]


def test_no_image_on_failed():
    agen = _FakeAgen(
        [
            Event(
                type="mint.failed",
                ts=0,
                identity=_tg_identity("55"),
                wallet=None,
                data={"nft_number": 1, "image_url": "https://cdn/x.png"},
            ),
        ]
    )
    svc = _FakeSvc(agen)
    sent = []

    async def announce(m, image):
        sent.append((m, image))

    _run(ev_mod.run_event_loop(svc, announce, None))
    # mint.failed -> announcement_image returns None even if data has a url
    assert sent[0][1] is None


def test_no_dm_for_failed_or_non_telegram():
    agen = _FakeAgen(
        [
            Event(
                type="mint.failed",
                ts=0,
                identity=_tg_identity("55"),
                wallet=None,
                data={"nft_number": 1},
            ),
            Event(
                type="mint.completed",
                ts=0,
                identity={"platform": "discord", "platform_user_id": "9"},
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


def test_loop_survives_handler_error():
    agen = _FakeAgen(
        [
            Event(
                type="mint.completed",
                ts=0,
                identity=_tg_identity("1"),
                wallet=None,
                data={"nft_number": 1},
            ),
            Event(
                type="mint.completed",
                ts=0,
                identity=_tg_identity("2"),
                wallet=None,
                data={"nft_number": 2},
            ),
        ]
    )
    calls = {"n": 0}

    async def announce(m, image):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    _run(ev_mod.run_event_loop(_FakeSvc(agen), announce, None))
    assert calls["n"] == 2
    assert agen.closed is True
