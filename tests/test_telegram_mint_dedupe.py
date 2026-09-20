import asyncio
from types import SimpleNamespace

from lfg_service.events import Event
from surfaces._client.errors import ServiceError
from surfaces.telegram_bot import delivered, mint_view
from surfaces.telegram_bot import events as ev_mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Bot:
    def __init__(self):
        self.photos = []
        self.messages = []

    async def send_photo(self, chat_id, photo, caption=None):
        self.photos.append((chat_id, photo, caption))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def _update_ctx(bot, uid="55"):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=int(uid), username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=999),
    )
    return update, SimpleNamespace(bot=bot, args=[])


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start = start
        self._final = final
        self._qr = qr

    async def start_mint(self, user_id, *, username=""):
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_mint(self, user_id, session_id):
        if isinstance(self._final, Exception):
            raise self._final
        return self._final


class _FakeAgen:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration

    async def aclose(self):
        pass


class _FakeSvc:
    def __init__(self, agen):
        self._agen = agen

    def events(self, types=None):
        return self._agen


def _completed(session_id, uid="55"):
    return Event(
        type="mint.completed",
        ts=0,
        identity={"platform": "telegram", "platform_user_id": uid},
        wallet=None,
        data={"id": session_id, "nft_number": 7, "image_url": "https://cdn/art.png"},
    )


def _drive_events(ev):
    sent, dmed = [], []

    async def announce(message, image):
        sent.append((message, image))

    async def dm(uid, message, image):
        dmed.append((uid, message, image))

    _run(ev_mod.run_event_loop(_FakeSvc(_FakeAgen([ev])), announce, dm))
    return sent, dmed


# --- the registry itself -----------------------------------------------------


def test_registry_marks_and_releases():
    assert not delivered.owns("dedupe-reg-1")
    delivered.mark("dedupe-reg-1")
    assert delivered.owns("dedupe-reg-1")
    # owns() is not consuming: a sub-tick double-publish must stay suppressed.
    assert delivered.owns("dedupe-reg-1")
    delivered.release("dedupe-reg-1")
    assert not delivered.owns("dedupe-reg-1")


def test_registry_ignores_blank_ids():
    delivered.mark("")
    assert not delivered.owns("")


def test_registry_is_bounded():
    for i in range(delivered.MAX_TRACKED + 50):
        delivered.mark(f"dedupe-bound-{i}")
    assert len(delivered._owned) <= delivered.MAX_TRACKED
    # the most recent marks survive; the oldest are evicted
    assert delivered.owns(f"dedupe-bound-{delivered.MAX_TRACKED + 49}")
    assert not delivered.owns("dedupe-bound-0")


# --- handle_mint claims the session ------------------------------------------


def test_chat_mint_claims_its_session_before_waiting():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    seen = {}

    class _WatchSvc(_Svc):
        async def wait_for_mint(self, user_id, session_id):
            # the claim must land BEFORE the terminal event can be published,
            # otherwise the firehose wins the race and DMs a duplicate
            seen["owned_while_waiting"] = delivered.owns(session_id)
            return self._final

    svc = _WatchSvc(
        start={"id": "dedupe-claim-1", "payment_link": "https://pay"},
        final={
            "state": "offer_ready",
            "nft_number": 3600,
            "image_url": "https://cdn/art.png",
            "accept_deeplink": "https://accept",
        },
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    assert seen["owned_while_waiting"] is True
    assert delivered.owns("dedupe-claim-1")


def test_chat_mint_releases_when_it_never_shows_the_artwork():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "dedupe-release-1", "payment_link": "https://pay"},
        final=ServiceError("boom", code="boom", status=500),
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # the handler bailed with an error message, so the DM is the only delivery
    # left — it must not stay suppressed
    assert not delivered.owns("dedupe-release-1")


def test_chat_mint_releases_on_bad_terminal_state():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "dedupe-release-2", "payment_link": "https://pay"},
        final={"state": "failed"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    assert not delivered.owns("dedupe-release-2")


# --- the firehose respects the claim -----------------------------------------


def test_no_dm_when_the_chat_handler_already_showed_the_artwork():
    delivered.mark("dedupe-ev-1")
    sent, dmed = _drive_events(_completed("dedupe-ev-1"))
    assert len(sent) == 1  # the channel announcement still goes out
    assert dmed == []


def test_dm_still_sent_for_an_unclaimed_session():
    # a Mini App (#89) mint is platform="telegram" too, but no chat handler
    # ever claimed it — the DM is that user's only delivery
    sent, dmed = _drive_events(_completed("dedupe-ev-2"))
    assert len(sent) == 1
    assert dmed == [("55", sent[0][0], "https://cdn/art.png")]


def test_claimed_session_stays_suppressed_across_a_double_publish():
    delivered.mark("dedupe-ev-3")
    ev = _completed("dedupe-ev-3")
    sent, dmed = _drive_events(ev)
    assert dmed == []
    sent2, dmed2 = _drive_events(_completed("dedupe-ev-3"))
    assert dmed2 == []
