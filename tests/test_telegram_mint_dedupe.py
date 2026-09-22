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


def _update_ctx(bot, uid="55", chat_id=999):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=int(uid), username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    return update, SimpleNamespace(bot=bot, args=[])


class _MediaFailsBot(_Bot):
    """Rejects the artwork URL for `fail_chat_id`, like Telegram refusing a
    media send. Everything else (QRs, text) still goes through."""

    def __init__(self, artwork_url, fail_chat_id):
        super().__init__()
        self._artwork_url = artwork_url
        self._fail_chat_id = fail_chat_id

    async def send_photo(self, chat_id, photo, caption=None):
        if photo == self._artwork_url and chat_id == self._fail_chat_id:
            raise RuntimeError("Bad Request: wrong file identifier")
        await super().send_photo(chat_id, photo, caption)


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


# --- the artwork has to reach the minter somehow ----------------------------
#
# Claiming the session means the firehose already skipped its DM, and the bus
# does not replay a consumed event — so a failed in-chat send must not leave
# the minter with no artwork at all, and must not take the claim QR with it.


def _final_with_art():
    return {
        "state": "offer_ready",
        "nft_number": 3600,
        "image_url": "https://cdn/art.png",
        "accept_deeplink": "https://accept",
    }


def test_group_mint_falls_back_to_a_dm_when_the_chat_send_fails():
    bot = _MediaFailsBot("https://cdn/art.png", fail_chat_id=999)
    update, ctx = _update_ctx(bot, uid="55", chat_id=999)
    svc = _Svc(
        start={"id": "dedupe-art-1", "payment_link": "https://pay"},
        final=_final_with_art(),
    )
    _run(mint_view.handle_mint(svc, update, ctx))

    # the artwork landed in the minter's DM instead of the group
    art = [p for p in bot.photos if p[1] == "https://cdn/art.png"]
    assert len(art) == 1
    assert art[0][0] == 55
    # it arrived, so the claim stands and the firehose stays quiet
    assert delivered.owns("dedupe-art-1")
    # and the mint still finishes: the claim QR is not collateral damage
    assert any(p[1] != "https://cdn/art.png" for p in bot.photos[1:])


def test_dm_mint_releases_the_claim_when_the_artwork_send_fails():
    # chat_id == the user's id: retrying the same media in the same chat is
    # pointless, so the claim goes back and the firehose DM is unsuppressed
    bot = _MediaFailsBot("https://cdn/art.png", fail_chat_id=55)
    update, ctx = _update_ctx(bot, uid="55", chat_id=55)
    svc = _Svc(
        start={"id": "dedupe-art-2", "payment_link": "https://pay"},
        final=_final_with_art(),
    )
    _run(mint_view.handle_mint(svc, update, ctx))

    assert [p for p in bot.photos if p[1] == "https://cdn/art.png"] == []
    assert not delivered.owns("dedupe-art-2")
    assert any(p[1] != "https://cdn/art.png" for p in bot.photos[1:])


def test_claimed_session_stays_suppressed_across_a_double_publish():
    delivered.mark("dedupe-ev-3")
    ev = _completed("dedupe-ev-3")
    sent, dmed = _drive_events(ev)
    assert dmed == []
    sent2, dmed2 = _drive_events(_completed("dedupe-ev-3"))
    assert dmed2 == []
