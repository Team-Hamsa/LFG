# tests/test_telegram_swap.py
# Chat-style trait swapper for the Telegram surface (#88). Drives the whole
# inline-keyboard callback chain with fakes (mirrors test_telegram_buttons.py +
# test_telegram_mint.py). Two layers under test:
#   - surfaces.telegram_bot.swap_render — pure InlineKeyboardMarkup / caption
#     builders (no SDK), unit-tested directly.
#   - surfaces.telegram_bot.swap_view — the conversation handlers; state lives in
#     context.user_data["swap_session"]. Fakes mirror the CallbackQuery shape.
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from surfaces._client.errors import BadRequest
from surfaces.telegram_bot import swap_render, swap_view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- fakes ----------------------------------------------------------------


class _Bot:
    def __init__(self):
        self.photos = []
        self.messages = []

    async def send_photo(self, chat_id, photo, caption=None):
        self.photos.append((chat_id, photo, caption))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class _Query:
    """A fake CallbackQuery: records edit_message_text / edit_reply_markup and
    answer() toasts. data is the callback_data being dispatched."""

    def __init__(self, data):
        self.data = data
        self.answer = AsyncMock()
        self.edits = []  # (text, markup)
        self.markup_edits = []
        self.message = SimpleNamespace(chat_id=999)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


def _command_update(bot):
    sent = {}

    async def reply_text(text, reply_markup=None):
        sent["text"] = text
        sent["markup"] = reply_markup

    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=reply_text),
        callback_query=None,
        effective_user=SimpleNamespace(id=55, username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=999),
    )
    ctx = SimpleNamespace(bot=bot, user_data={})
    return update, ctx, sent


def _callback_update(bot, data, user_data):
    query = _Query(data)
    update = SimpleNamespace(
        message=None,
        callback_query=query,
        effective_user=SimpleNamespace(id=55, username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=999),
    )
    ctx = SimpleNamespace(bot=bot, user_data=user_data)
    return update, ctx, query


def _nft(nft_id, number, gender, **traits):
    attrs = [{"trait_type": k, "value": v} for k, v in traits.items()]
    return {
        "nft_id": nft_id,
        "name": f"LFGO #{number}",
        "number": number,
        "image": f"https://cdn/{number}.png",
        "gender": gender,
        "attributes": attrs,
        "mutable": True,
    }


SWAPPABLE = ["Background", "Back", "Clothing", "Mouth", "Eyebrows", "Eyes", "Head", "Accessory"]


class _Svc:
    def __init__(self, *, nfts=None, nfts_exc=None, start=None, final=None, qr=b"PNG"):
        self._nfts = nfts or {}
        self._nfts_exc = nfts_exc
        self._start = start
        self._final = final
        self._qr = qr
        self.start_calls = []

    async def nfts(self, user_id):
        if self._nfts_exc is not None:
            raise self._nfts_exc
        return self._nfts

    async def qr_png(self, data):
        return self._qr

    async def start_swap(self, user_id, nft1_id, nft2_id, traits, *, username=""):
        self.start_calls.append((nft1_id, nft2_id, list(traits)))
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def wait_for_swap(self, user_id, session_id):
        return self._final


def _roster(*nfts, swap_fee=None):
    return {"nfts": list(nfts), "swappable_traits": SWAPPABLE, "swap_fee": swap_fee}


# ---- pure render builders -------------------------------------------------


def test_nft_grid_keyboard_pairs_two_per_row_with_pick_callbacks():
    nfts = [_nft(f"id{i}", 100 + i, "male") for i in range(3)]
    markup = swap_render.nft_grid_keyboard(nfts)
    rows = markup.inline_keyboard
    # 3 nfts -> 2 per row -> first row has 2, second has 1 (ignoring any nav row)
    pick_rows = [r for r in rows if any(b.callback_data.startswith("swap_pick_") for b in r)]
    assert [len(r) for r in pick_rows] == [2, 1]
    callbacks = {b.callback_data for r in pick_rows for b in r}
    assert callbacks == {"swap_pick_id0", "swap_pick_id1", "swap_pick_id2"}


def test_nft_grid_keyboard_gender_filter_omits_mismatches():
    nfts = [
        _nft("m1", 1, "male"),
        _nft("f1", 2, "female"),
        _nft("m2", 3, "male"),
    ]
    markup = swap_render.nft_grid_keyboard(nfts, gender="male")
    callbacks = {b.callback_data for r in markup.inline_keyboard for b in r}
    # only the two males are pickable; the female is dimmed (non-pick) — its
    # callback is never a swap_pick_.
    assert "swap_pick_m1" in callbacks
    assert "swap_pick_m2" in callbacks
    assert "swap_pick_f1" not in callbacks


def test_nft_grid_keyboard_paginates_when_over_eight():
    nfts = [_nft(f"id{i}", i, "male") for i in range(10)]
    page0 = swap_render.nft_grid_keyboard(nfts, page=0)
    picks0 = {
        b.callback_data
        for r in page0.inline_keyboard
        for b in r
        if b.callback_data.startswith("swap_pick_")
    }
    assert len(picks0) == 8  # page size
    nav0 = {
        b.callback_data
        for r in page0.inline_keyboard
        for b in r
        if b.callback_data.startswith("swap_page_")
    }
    assert "swap_page_1" in nav0
    page1 = swap_render.nft_grid_keyboard(nfts, page=1)
    picks1 = {
        b.callback_data
        for r in page1.inline_keyboard
        for b in r
        if b.callback_data.startswith("swap_pick_")
    }
    assert len(picks1) == 2


def test_trait_picker_keyboard_shows_values_and_toggle_state():
    a = _nft("a", 1, "male", Eyes="Blue", Head="Cap")
    b = _nft("b", 2, "male", Eyes="Green", Head="Hat")
    markup = swap_render.trait_picker_keyboard(a, b, ["Eyes", "Head"], {"Eyes"})
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    # Eyes selected (☑), Head unselected (☐); both show the two values
    assert any("☑" in t and "Eyes" in t and "Blue" in t and "Green" in t for t in texts)
    assert any("☐" in t and "Head" in t and "Cap" in t and "Hat" in t for t in texts)
    callbacks = {btn.callback_data for row in markup.inline_keyboard for btn in row}
    assert "swap_trait_Eyes" in callbacks
    assert "swap_confirm" in callbacks
    assert "swap_cancel" in callbacks


def test_swap_payment_caption_names_fee_and_currency():
    cap = swap_render.swap_payment_caption("10", "BRIX")
    assert "10" in cap and "BRIX" in cap


def test_swap_result_caption_modified_vs_offer():
    mod = swap_render.swap_result_caption({"name": "LFGO #1", "modified": True})
    assert "LFGO #1" in mod and "no action" in mod.lower()
    offer = swap_render.swap_result_caption({"name": "LFGO #2", "modified": False})
    assert "LFGO #2" in offer and "xaman" in offer.lower()


# ---- entry: /swap ---------------------------------------------------------


def test_handle_swap_needs_two_avatars():
    bot = _Bot()
    update, ctx, sent = _command_update(bot)
    svc = _Svc(nfts=_roster(_nft("a", 1, "male")))
    _run(swap_view.handle_swap(svc, update, ctx))
    assert "two" in sent["text"].lower()
    assert "swap_session" not in ctx.user_data


def test_handle_swap_renders_grid_and_stores_roster():
    bot = _Bot()
    update, ctx, sent = _command_update(bot)
    nfts = [_nft("a", 1, "male"), _nft("b", 2, "male")]
    svc = _Svc(nfts=_roster(*nfts))
    _run(swap_view.handle_swap(svc, update, ctx))
    assert sent["markup"] is not None
    callbacks = {b.callback_data for r in sent["markup"].inline_keyboard for b in r}
    assert "swap_pick_a" in callbacks and "swap_pick_b" in callbacks
    assert ctx.user_data["swap_session"]["roster"]  # roster cached
    assert "first" in sent["text"].lower()


def test_handle_swap_service_error_is_friendly():
    bot = _Bot()
    update, ctx, sent = _command_update(bot)
    svc = _Svc(nfts_exc=BadRequest("no wallet registered", status=400))
    _run(swap_view.handle_swap(svc, update, ctx))
    assert "register" in sent["text"].lower()


# ---- first pick locks gender ----------------------------------------------


def _session(roster, **over):
    s = {"roster": roster, "nft1_id": None, "nft2_id": None, "traits": {}, "page": 0}
    s.update(over)
    return s


def test_first_pick_locks_gender_and_filters_grid():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male"), _nft("f1", 2, "female"), _nft("m2", 3, "male")]
    user_data = {"swap_session": _session(nfts)}
    update, ctx, query = _callback_update(bot, "swap_pick_m1", user_data)
    _run(swap_view.handle_swap_pick(None, update, ctx))
    # nft1 stored, gender locked
    assert ctx.user_data["swap_session"]["nft1_id"] == "m1"
    # grid re-rendered in place, only males pickable
    assert query.edits
    _text, markup = query.edits[-1]
    callbacks = {b.callback_data for r in markup.inline_keyboard for b in r}
    assert "swap_pick_m2" in callbacks
    assert "swap_pick_f1" not in callbacks


def test_second_pick_shows_trait_picker():
    bot = _Bot()
    nfts = [
        _nft("m1", 1, "male", Eyes="Blue"),
        _nft("m2", 3, "male", Eyes="Green"),
    ]
    user_data = {"swap_session": _session(nfts, nft1_id="m1")}
    update, ctx, query = _callback_update(bot, "swap_pick_m2", user_data)
    _run(swap_view.handle_swap_pick(None, update, ctx))
    assert ctx.user_data["swap_session"]["nft2_id"] == "m2"
    _text, markup = query.edits[-1]
    callbacks = {b.callback_data for r in markup.inline_keyboard for b in r}
    assert "swap_confirm" in callbacks
    assert any(c.startswith("swap_trait_") for c in callbacks)


def test_gender_mismatch_cannot_be_picked():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male"), _nft("f1", 2, "female")]
    user_data = {"swap_session": _session(nfts, nft1_id="m1")}
    update, ctx, query = _callback_update(bot, "swap_pick_f1", user_data)
    _run(swap_view.handle_swap_pick(None, update, ctx))
    # nft2 NOT set; a toast explains why
    assert ctx.user_data["swap_session"]["nft2_id"] is None
    query.answer.assert_awaited()


# ---- trait toggle ---------------------------------------------------------


def test_trait_toggle_mutates_selected_set():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male", Eyes="Blue"), _nft("m2", 3, "male", Eyes="Green")]
    user_data = {"swap_session": _session(nfts, nft1_id="m1", nft2_id="m2")}
    update, ctx, query = _callback_update(bot, "swap_trait_Eyes", user_data)
    _run(swap_view.handle_swap_trait(None, update, ctx))
    assert ctx.user_data["swap_session"]["traits"].get("Eyes") is True
    # toggle off again
    update2, ctx2, query2 = _callback_update(bot, "swap_trait_Eyes", user_data)
    _run(swap_view.handle_swap_trait(None, update2, ctx2))
    assert not ctx2.user_data["swap_session"]["traits"].get("Eyes")


# ---- confirm guard + flow -------------------------------------------------


def test_confirm_with_zero_traits_is_noop_toast():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male"), _nft("m2", 3, "male")]
    user_data = {"swap_session": _session(nfts, nft1_id="m1", nft2_id="m2", traits={})}
    svc = _Svc()
    update, ctx, query = _callback_update(bot, "swap_confirm", user_data)
    _run(swap_view.handle_swap_confirm(svc, update, ctx))
    # no swap fired, session still present, a toast was shown
    assert svc.start_calls == []
    assert "swap_session" in ctx.user_data
    query.answer.assert_awaited()


def test_confirm_with_traits_runs_swap_and_renders_results():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male", Eyes="Blue"), _nft("m2", 3, "male", Eyes="Green")]
    user_data = {"swap_session": _session(nfts, nft1_id="m1", nft2_id="m2", traits={"Eyes": True})}
    svc = _Svc(
        start={"id": "sid", "state": "composing", "payment_link": ""},
        final={
            "state": "offers_ready",
            "results": [
                {"name": "LFGO #1", "modified": True},
                {
                    "name": "LFGO #3",
                    "modified": False,
                    "accept_qr_url": "https://cdn/qr.png",
                    "accept_deeplink": "https://accept",
                },
            ],
        },
    )
    update, ctx, query = _callback_update(bot, "swap_confirm", user_data)
    _run(swap_view.handle_swap_confirm(svc, update, ctx))
    # start_swap called with sorted traits
    assert svc.start_calls == [("m1", "m2", ["Eyes"])]
    # one modified message + one offer QR photo
    assert any("no action" in m[1].lower() for m in bot.messages)
    assert any(p[1] == "https://cdn/qr.png" for p in bot.photos)
    # state cleared at the end
    assert "swap_session" not in ctx.user_data


def test_confirm_payment_branch_sends_fee_qr():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male", Eyes="Blue"), _nft("m2", 3, "male", Eyes="Green")]
    user_data = {"swap_session": _session(nfts, nft1_id="m1", nft2_id="m2", traits={"Eyes": True})}
    svc = _Svc(
        start={
            "id": "sid",
            "state": "awaiting_payment",
            "payment_link": "https://pay",
            "fee_amount": "10",
            "pay_with": "BRIX",
        },
        final={"state": "offers_ready", "results": [{"name": "LFGO #1", "modified": True}]},
    )
    update, ctx, query = _callback_update(bot, "swap_confirm", user_data)
    _run(swap_view.handle_swap_confirm(svc, update, ctx))
    # the fee QR photo went out with a caption naming the fee + currency
    fee_caps = [p[2] for p in bot.photos if p[2] and "10" in p[2] and "BRIX" in p[2]]
    assert fee_caps


def test_confirm_failed_state_is_friendly_error_and_clears():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male", Eyes="Blue"), _nft("m2", 3, "male", Eyes="Green")]
    user_data = {"swap_session": _session(nfts, nft1_id="m1", nft2_id="m2", traits={"Eyes": True})}
    svc = _Svc(
        start={"id": "sid", "state": "composing", "payment_link": ""},
        final={"state": "failed", "error": "Reminting failed."},
    )
    update, ctx, query = _callback_update(bot, "swap_confirm", user_data)
    _run(swap_view.handle_swap_confirm(svc, update, ctx))
    assert any("⚠️" in m[1] or "fail" in m[1].lower() for m in bot.messages)
    assert "swap_session" not in ctx.user_data


# ---- cancel ---------------------------------------------------------------


def test_cancel_clears_state():
    bot = _Bot()
    nfts = [_nft("m1", 1, "male"), _nft("m2", 3, "male")]
    user_data = {"swap_session": _session(nfts, nft1_id="m1")}
    update, ctx, query = _callback_update(bot, "swap_cancel", user_data)
    _run(swap_view.handle_swap_cancel(None, update, ctx))
    assert "swap_session" not in ctx.user_data
    assert query.edits
    assert "cancel" in query.edits[-1][0].lower()


# ---- pagination -----------------------------------------------------------


def test_page_navigation_rerenders_grid_for_page():
    bot = _Bot()
    nfts = [_nft(f"id{i}", i, "male") for i in range(10)]
    user_data = {"swap_session": _session(nfts)}
    update, ctx, query = _callback_update(bot, "swap_page_1", user_data)
    _run(swap_view.handle_swap_page(None, update, ctx))
    assert ctx.user_data["swap_session"]["page"] == 1
    _text, markup = query.edits[-1]
    picks = {
        b.callback_data
        for r in markup.inline_keyboard
        for b in r
        if b.callback_data.startswith("swap_pick_")
    }
    assert len(picks) == 2  # page 1 has the remaining 2
