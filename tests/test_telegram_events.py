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


def test_make_announcement_unknown_event_uses_generic_fallback(caplog):
    """An unhandled event type must NOT render the 'equip failed' copy; it
    hits the generic fallback and logs a warning."""
    e = Event(
        type="frobnicate.completed",
        ts=0,
        identity=_tg_identity("55"),
        wallet=None,
        data={},
    )
    import logging

    with caplog.at_level(logging.WARNING):
        msg = ev_mod.make_announcement(e)
    assert "equip" not in msg.lower()
    assert "Unknown event" in msg
    assert any("unhandled event type" in r.message for r in caplog.records)


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
    assert "mint.completed" in svc.types and "mint.failed" in svc.types
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


def test_subscribes_to_all_announce_types():
    agen = _FakeAgen([])
    svc = _FakeSvc(agen)
    _run(ev_mod.run_event_loop(svc, lambda m, i: _noop(), None))
    assert set(svc.types) == {
        "mint.completed",
        "mint.failed",
        "swap.completed",
        "swap.failed",
        "harvest.completed",
        "harvest.failed",
        "assemble.completed",
        "assemble.failed",
        "equip.completed",
        "equip.failed",
    }


async def _noop():
    return None


def _ev(type_, **data):
    return Event(
        type=type_,
        ts=0,
        identity={"platform": "telegram", "platform_user_id": "55", "display_handle": "alice"},
        wallet=None,
        data=data,
    )


def test_swap_announcements():
    assert "alice" in ev_mod.make_announcement(_ev("swap.completed"))
    assert "swapped" in ev_mod.make_announcement(_ev("swap.completed"))
    assert "ailed" in ev_mod.make_announcement(_ev("swap.failed"))


def test_assemble_announcements():
    msg = ev_mod.make_announcement(_ev("assemble.completed", edition=3537))
    assert "dressed a blank" in msg and "#3537" in msg
    assert "ailed" in ev_mod.make_announcement(_ev("assemble.failed"))


def test_harvest_announcements():
    assert "blank" in ev_mod.make_announcement(_ev("harvest.completed"))
    assert "ailed" in ev_mod.make_announcement(_ev("harvest.failed"))


def test_equip_announcements():
    assert "equipped" in ev_mod.make_announcement(_ev("equip.completed"))
    assert "ailed" in ev_mod.make_announcement(_ev("equip.failed"))


def test_image_on_new_completed_types():
    e = _ev("swap.completed", image_url="https://cdn/s.png")
    assert ev_mod.announcement_image(e) == "https://cdn/s.png"
    e = _ev("assemble.completed", image_url="https://cdn/a.png")
    assert ev_mod.announcement_image(e) == "https://cdn/a.png"
    assert ev_mod.announcement_image(_ev("swap.failed", image_url="https://cdn/s.png")) is None


def test_no_dm_on_swap_completed():
    agen = _FakeAgen([_ev("swap.completed", image_url="https://cdn/s.png")])
    sent, dmed = [], []

    async def announce(m, image):
        sent.append((m, image))

    async def dm(uid, m, image):
        dmed.append((uid, m, image))

    _run(ev_mod.run_event_loop(_FakeSvc(agen), announce, dm))
    assert sent and "swapped" in sent[0][0]
    assert dmed == []  # only mint.completed DMs


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


def test_announcement_image_prefers_video_url():
    # Animated mints carry a video_url (MP4) next to the PNG poster; the
    # announcement should attach the animation, not the still.
    e = Event(
        type="mint.completed",
        ts=0,
        identity=_tg_identity("55"),
        wallet=None,
        data={
            "nft_number": 7,
            "image_url": "https://cdn/a.png",
            "video_url": "https://cdn/a.mp4",
        },
    )
    assert ev_mod.announcement_image(e) == "https://cdn/a.mp4"


def test_announcement_image_falls_back_to_image_url():
    e = Event(
        type="mint.completed",
        ts=0,
        identity=_tg_identity("55"),
        wallet=None,
        data={"nft_number": 7, "image_url": "https://cdn/a.png"},
    )
    assert ev_mod.announcement_image(e) == "https://cdn/a.png"
